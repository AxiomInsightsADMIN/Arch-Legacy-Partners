"""
NC DEQ DWM Solid Waste Permitted Facilities loader — Phase 2 step 3.

Pulls the master "Active Solid Waste Facilities" roster maintained by NC
DEQ DWM and upserts one row per facility into `raw_facility_record` with
`source='nc_deq_solid_waste_facility_list'`.

Access path
-----------
The data lives behind NC DEQ's Laserfiche document repository at
`edocs.deq.nc.gov` (docid=2132701). The repository host TCP-blocks our
egress IP for both vanilla `requests` and headless Playwright — the
block is at the network layer, not the application layer (see
`docs/nc_deq_audit.md` section C and the Phase 2 step 3 entry in
`docs/build_log.md`). Per the locked operational rule, we do NOT
attempt anti-detection escalation, IP rotation, or proxy use.

Two-path fetch model:

  1. Try Playwright fetch first. Forward-compat — if NC DEQ ever
     relaxes the WAF rule we pick the file up automatically.
  2. On Playwright failure, fall back to the manual-drop pickup at
     `local/manual_drops/nc_deq_solid_waste/`. Ryan drops the latest
     XLSX into that directory from a real browser session; the loader
     uses the newest file by mtime.

Source schema (verified from 2026-05-11 manual drop)
----------------------------------------------------
- Two sheets: "About" (40-row metadata) and "Active Solid Waste
  Facilities" (435 facility rows x 13 columns).
- About sheet header carries "Permitted Solid Waste Facilities List -
  Date Created: <Month DD, YYYY>" — parsed into a RFC 7231 string for
  `source_signature.last_modified`.
- Facility sheet columns: County, Facility Id, Facility Name, Waste,
  Activity, Latitude, Longitude, Address, City, State, Zip, Contact,
  Phone.
- Stable identifier: **`Facility Id`** (100% populated, 100% unique
  across all 435 rows of the 2026-05-11 sample). Format encodes
  `<county-prefix>-<facility-type-code>-<year-or-suffix>` (e.g.
  `0104-MSWLF-1994`, `0106-TP-2012`).
- `State` is uniformly `NC` (cross-state count expected to be 0; we
  still assert it).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import pandas as pd
import psycopg2.extras

# Project-root path shim so this runs both via `python scrapers/state/X.py`
# and `python -m scrapers.state.X`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scrapers._loader_utils import (  # noqa: E402
    begin_run,
    db_connect,
    finish_run,
    get_source_id,
    hash_payload,
)

ROOT = _PROJECT_ROOT

USER_AGENT = (
    "Axiom-Insights-ArchLegacy/0.1 (Phase-2 NC DEQ Solid Waste loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)
SOURCE_SLUG = "nc_deq_solid_waste_facility_list"
SOURCE_RECORD_ID_FIELD = "Facility Id"
STATE_COLUMN = "State"
EXPECTED_STATE = "NC"
BATCH_SIZE = 500

# Manual-drop directory the operator (Ryan) populates from a real browser
# session when edocs blocks our automated egress.
MANUAL_DROP_DIR = ROOT / "local" / "manual_drops" / "nc_deq_solid_waste"

# Playwright target (will currently fail — kept for forward-compat).
PW_URL = (
    "https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx"
    "?docid=2132701&dbid=0&repo=WasteManagement"
)
PW_NAV_TIMEOUT_MS = 30_000  # be modest; we expect failure


@dataclass
class LoadResult:
    fetch_path: str | None = None  # "playwright" | "manual_drop"
    fetch_source_uri: str | None = None
    download_bytes: int = 0
    rows_parsed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_skipped_no_id: int = 0
    rows_skipped_dupe_id: int = 0
    cross_state_rows: int = 0
    null_lat_lng_rows: int = 0
    schema_hash: str | None = None
    columns: list[str] = field(default_factory=list)
    last_modified: str | None = None
    elapsed_sec: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------
# Fetch — Playwright primary, manual-drop fallback
# --------------------------------------------------------------------------
def _try_playwright_fetch(work_dir: Path) -> tuple[bytes, str] | None:
    """Attempt to fetch the XLSX via Playwright. Returns (bytes,
    saved_path_or_url) on success, None on failure. NO escalation —
    a connection timeout or any exception returns None and the caller
    falls back to manual-drop pickup."""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[NC SW]   playwright not installed; skipping playwright fetch", flush=True)
        return None
    print(f"[NC SW]   playwright fetch attempt: {PW_URL}", flush=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(accept_downloads=True)
                page = ctx.new_page()
                downloads: list = []
                page.on("download", lambda d: downloads.append(d))
                try:
                    page.goto(PW_URL, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT_MS)
                except PWTimeout:
                    print(
                        "[NC SW]   playwright: navigation timed out (TCP block expected)",
                        flush=True,
                    )
                    return None
                page.wait_for_timeout(1500)
                if not downloads:
                    return None
                d = downloads[0]
                target = (
                    work_dir
                    / f"playwright_{int(time.time())}_{d.suggested_filename or 'download.xlsx'}"
                )
                d.save_as(str(target))
                body = target.read_bytes()
                return body, str(target.relative_to(ROOT))
            finally:
                browser.close()
    except Exception as e:
        # `requests.exceptions.ConnectionError` from the underlying CDP
        # transport sometimes surfaces here too. We treat any exception as
        # "playwright failed; fall back" — never as something to retry.
        print(f"[NC SW]   playwright fetch failed: {type(e).__name__}: {e}", flush=True)
        return None


def _newest_manual_drop() -> Path | None:
    """Return the most recently modified .xlsx in the manual-drop dir,
    or None if the dir is empty / missing."""
    if not MANUAL_DROP_DIR.exists():
        return None
    candidates = sorted(
        MANUAL_DROP_DIR.glob("*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def fetch_source() -> tuple[bytes, str, str]:
    """Return (xlsx_bytes, fetch_path_label, source_uri_or_path).
    Raises if neither the Playwright path nor a manual drop produces
    a file."""
    # 1) Try Playwright (forward-compat; expected to fail today)
    pw = _try_playwright_fetch(ROOT / "local" / "manual_drops" / "nc_deq_solid_waste")
    if pw is not None:
        body, where = pw
        print(f"[NC SW]   playwright succeeded; {len(body):,} bytes via {where}", flush=True)
        return body, "playwright", PW_URL

    # 2) Fall back to manual-drop pickup
    f = _newest_manual_drop()
    if f is None:
        raise RuntimeError(
            f"NC DEQ Solid Waste: no manual drop found at "
            f"{MANUAL_DROP_DIR.relative_to(ROOT)} and Playwright was blocked. "
            "Drop the XLSX from a real browser session and re-run."
        )
    body = f.read_bytes()
    print(f"[NC SW]   manual-drop pickup: {f.relative_to(ROOT)}  ({len(body):,} bytes)", flush=True)
    return body, "manual_drop", str(f.relative_to(ROOT))


# --------------------------------------------------------------------------
# Date extraction from the "About" sheet header
# --------------------------------------------------------------------------
_DATE_RE = re.compile(r"Date Created:\s*([A-Z][a-z]+ \d{1,2},\s*\d{4})", re.I)


def _extract_content_date(xlsx_bytes: bytes) -> str | None:
    """Return RFC 7231 string from the About-sheet header date, or None."""
    import io as _io

    try:
        about = pd.read_excel(
            _io.BytesIO(xlsx_bytes),
            sheet_name="About",
            engine="openpyxl",
            dtype=str,
            header=None,
        )
    except Exception:
        # If the About sheet is missing or the read fails, return None — the
        # signature column stays NULL and the file's mtime is the only
        # cadence signal we have on the manual-drop path.
        return None
    # The About sheet's header carries the "Date Created" text; the column
    # name itself encodes it ("Permitted ... - Date Created: April 28, 2026").
    blob = "\n".join(
        str(v) for v in (list(about.columns) + about.values.flatten().tolist()) if v is not None
    )
    m = _DATE_RE.search(blob)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1).strip(), "%B %d, %Y").replace(tzinfo=UTC)
        return format_datetime(dt, usegmt=True)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Parse + upsert
# --------------------------------------------------------------------------
UPSERT_SQL = """
INSERT INTO raw_facility_record
    (source_id, source_record_id, scraper_run_id, raw_payload, payload_hash)
VALUES %s
ON CONFLICT (source_id, source_record_id) DO UPDATE
   SET raw_payload    = EXCLUDED.raw_payload,
       payload_hash   = EXCLUDED.payload_hash,
       scraper_run_id = EXCLUDED.scraper_run_id,
       ingested_at    = NOW()
   WHERE raw_facility_record.payload_hash <> EXCLUDED.payload_hash
RETURNING (xmax = 0) AS inserted, source_record_id;
"""


def _flush_batch(cur, batch: list[tuple]) -> tuple[int, int]:
    if not batch:
        return 0, 0
    result = psycopg2.extras.execute_values(
        cur,
        UPSERT_SQL,
        batch,
        fetch=True,
        page_size=BATCH_SIZE,
    )
    inserted = sum(1 for r in result if r[0])
    updated = sum(1 for r in result if not r[0])
    return inserted, updated


def write_signature_with_last_modified(
    cur,
    *,
    source_id: int,
    run_id: int,
    http_status: int | None,
    byte_size: int,
    schema_hash: str | None,
    row_count: int,
    last_modified: str | None,
) -> None:
    cur.execute(
        """
        INSERT INTO source_signature
            (source_id, scraper_run_id, http_status, response_byte_size,
             schema_hash, row_count, last_modified)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (source_id, run_id, http_status, byte_size, schema_hash, row_count, last_modified),
    )


def _row_payload(row: pd.Series) -> dict:
    """Convert pandas Series to a plain dict, NaN -> None."""
    out = {}
    for k, v in row.items():
        if pd.isna(v):
            out[str(k)] = None
        elif isinstance(v, pd.Timestamp):
            out[str(k)] = v.isoformat()
        else:
            out[str(k)] = str(v)
    return out


def load() -> LoadResult:
    print("[NC SW] starting load", flush=True)
    t0 = time.time()
    res = LoadResult()

    # 1) Fetch
    try:
        body, path_label, uri = fetch_source()
        res.fetch_path = path_label
        res.fetch_source_uri = uri
        res.download_bytes = len(body)
    except Exception as e:
        res.error = str(e)
        print(f"[NC SW]   FETCH ERROR: {e}", flush=True)
        res.elapsed_sec = round(time.time() - t0, 1)
        return res

    # 2) Parse — content-date from About + facility rows from main sheet
    res.last_modified = _extract_content_date(body)
    print(
        f"[NC SW]   content-date (from About sheet) -> last_modified={res.last_modified!r}",
        flush=True,
    )

    import io

    df = pd.read_excel(
        io.BytesIO(body),
        sheet_name="Active Solid Waste Facilities",
        engine="openpyxl",
        dtype=str,
    )
    res.rows_parsed = len(df)
    res.columns = list(df.columns)
    res.schema_hash = hash_payload(",".join(res.columns))
    print(f"[NC SW]   parsed shape={df.shape}  schema_hash={res.schema_hash[:12]}…", flush=True)

    # 3) Cross-state sanity (count, do not mutate)
    if STATE_COLUMN in df.columns:
        col = df[STATE_COLUMN].dropna()
        res.cross_state_rows = int((col.str.upper() != EXPECTED_STATE).sum())
        print(
            f"[NC SW]   cross-state rows (non-{EXPECTED_STATE} in {STATE_COLUMN!r}): "
            f"{res.cross_state_rows}",
            flush=True,
        )

    # Count rows with NULL coords (NC DEQ DWM publishes lat/lng here unlike
    # the DWR view; informational)
    if "Latitude" in df.columns and "Longitude" in df.columns:
        res.null_lat_lng_rows = int((df["Latitude"].isna() | df["Longitude"].isna()).sum())
        print(f"[NC SW]   rows with null lat or lng: {res.null_lat_lng_rows}", flush=True)

    # 4) DB session
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, SOURCE_SLUG)
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[NC SW]   scraper_run id={run_id} (source_id={source_id})", flush=True)

    # 5) In-Python dedupe + batched upsert
    by_sid: dict[str, tuple] = {}
    for _, row in df.iterrows():
        sid_raw = row.get(SOURCE_RECORD_ID_FIELD)
        sid = None if pd.isna(sid_raw) else str(sid_raw).strip()
        if not sid:
            res.rows_skipped_no_id += 1
            continue
        payload = _row_payload(row)
        payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        ph = hash_payload(payload_str)
        if sid in by_sid:
            res.rows_skipped_dupe_id += 1
        by_sid[sid] = (source_id, sid, run_id, psycopg2.extras.Json(payload), ph)

    err: str | None = None
    try:
        batch: list[tuple] = []
        for tup in by_sid.values():
            batch.append(tup)
            if len(batch) >= BATCH_SIZE:
                ins, upd = _flush_batch(cur, batch)
                res.rows_inserted += ins
                res.rows_updated += upd
                res.rows_unchanged += len(batch) - ins - upd
                conn.commit()
                batch = []
        if batch:
            ins, upd = _flush_batch(cur, batch)
            res.rows_inserted += ins
            res.rows_updated += upd
            res.rows_unchanged += len(batch) - ins - upd
            conn.commit()

        write_signature_with_last_modified(
            cur,
            source_id=source_id,
            run_id=run_id,
            http_status=None,  # we fetched a file from disk on the manual path
            byte_size=res.download_bytes,
            schema_hash=res.schema_hash,
            row_count=res.rows_parsed,
            last_modified=res.last_modified,
        )
        finish_run(
            cur,
            run_id,
            "success",
            rows_in=res.rows_parsed,
            rows_inserted=res.rows_inserted,
            rows_updated=res.rows_updated,
        )
        conn.commit()
    except Exception as e:
        err = str(e)
        print(f"[NC SW]   ERROR during upsert: {err}", flush=True)
        conn.rollback()
        try:
            finish_run(
                cur,
                run_id,
                "failed",
                rows_in=res.rows_parsed,
                rows_inserted=res.rows_inserted,
                rows_updated=res.rows_updated,
                error_message=err,
            )
            conn.commit()
        except Exception:
            pass
        res.error = err
    finally:
        cur.close()
        conn.close()

    res.elapsed_sec = round(time.time() - t0, 1)
    print(
        f"[NC SW] done in {res.elapsed_sec}s via {res.fetch_path!r} — "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"cross_state={res.cross_state_rows} null_lat_lng={res.null_lat_lng_rows} "
        f"bytes={res.download_bytes:,} last_modified={res.last_modified!r}",
        flush=True,
    )
    return res


def main() -> int:
    res = load()
    print("\n========== SUMMARY ==========")
    status = "OK" if not res.error else "FAIL"
    print(
        f"  [{status}] NC DEQ Solid Waste (via {res.fetch_path}): "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"cross_state={res.cross_state_rows} null_lat_lng={res.null_lat_lng_rows} "
        f"bytes={res.download_bytes:,} elapsed={res.elapsed_sec}s "
        f"last_modified={res.last_modified!r}"
    )
    if res.error:
        print(f"           error: {res.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
