"""
NC DEQ DWM Septage Firm Registry loader — Phase 2 step 4.

Pulls the master Septage Firm list maintained by NC DEQ DWM and upserts
one row per registered septage firm into `raw_facility_record` with
`source='nc_deq_septage_firm_list'`. Direct coverage of v1 category 4
(private/regional septage facilities) — every row in this list is a
private septage hauler / firm.

Access path
-----------
Same edocs document repository as the solid-waste list (docid=2132702
here vs 2132701 there). Same network-layer TCP block on the Playwright
path; same manual-drop fallback at
`local/manual_drops/nc_deq_septage_firm/`. See the Phase 2 step 3
build_log entry for the failure mode write-up.

Source schema (verified from 2026-05-11 manual drop)
----------------------------------------------------
- 1 sheet named `PermittedSeptageForm_<YYYYMMDD>` (e.g.
  `PermittedSeptageForm_20260428`). The content-date is encoded in
  the sheet name itself — no About sheet — so the loader parses
  the date from the sheet name.
- 759 firm rows x 9 columns: County, Waste, Activity, Status, Permit,
  Name, Address, Contact, Phone. 0 nulls anywhere.
- Stable identifier: **`Permit`** (100% populated, 100% unique
  across all 759 rows). Format: `NCS-\\d{5}` (e.g. `NCS-01837`,
  `NCS-00308`).
- `Waste` = "Septage" for all rows. `Activity` = "Hauler" for all
  rows. `Status` = "Open" for all rows. These are uniform classifiers
  and don't need per-row decoding; they confirm this is a pure
  category-4 source.
- No State column. `County` is the address geography. NC DEQ uses
  `'-'` as the County value for **out-of-state firms** that are
  NC-registered to operate in NC. We count those for transparency
  but they belong to NC's regulatory scope and stay in the load.
"""

from __future__ import annotations

import io
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
    "Axiom-Insights-ArchLegacy/0.1 (Phase-2 NC DEQ Septage Firm loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)
SOURCE_SLUG = "nc_deq_septage_firm_list"
SOURCE_RECORD_ID_FIELD = "Permit"
COUNTY_COLUMN = "County"
OUT_OF_STATE_COUNTY_VALUE = "-"  # NC DEQ marker for out-of-state firms
BATCH_SIZE = 500

MANUAL_DROP_DIR = ROOT / "local" / "manual_drops" / "nc_deq_septage_firm"

PW_URL = (
    "https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx"
    "?docid=2132702&dbid=0&repo=WasteManagement"
)
PW_NAV_TIMEOUT_MS = 30_000

# Sheet-name pattern for content-date extraction
_SHEET_DATE_RE = re.compile(r"(\d{8})$")  # trailing YYYYMMDD


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
    out_of_state_rows: int = 0  # County == '-' (NC-permitted but out-of-state physical)
    schema_hash: str | None = None
    columns: list[str] = field(default_factory=list)
    sheet_name: str | None = None
    last_modified: str | None = None
    elapsed_sec: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------
# Fetch — Playwright primary, manual-drop fallback (mirrors solid_waste)
# --------------------------------------------------------------------------
def _try_playwright_fetch(work_dir: Path) -> tuple[bytes, str] | None:
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[NC SF]   playwright not installed; skipping playwright fetch", flush=True)
        return None
    print(f"[NC SF]   playwright fetch attempt: {PW_URL}", flush=True)
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
                        "[NC SF]   playwright: navigation timed out (TCP block expected)",
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
        print(f"[NC SF]   playwright fetch failed: {type(e).__name__}: {e}", flush=True)
        return None


def _newest_manual_drop() -> Path | None:
    if not MANUAL_DROP_DIR.exists():
        return None
    candidates = sorted(
        MANUAL_DROP_DIR.glob("*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def fetch_source() -> tuple[bytes, str, str]:
    pw = _try_playwright_fetch(MANUAL_DROP_DIR)
    if pw is not None:
        body, where = pw
        print(f"[NC SF]   playwright succeeded; {len(body):,} bytes via {where}", flush=True)
        return body, "playwright", PW_URL

    f = _newest_manual_drop()
    if f is None:
        raise RuntimeError(
            f"NC DEQ Septage Firm: no manual drop found at "
            f"{MANUAL_DROP_DIR.relative_to(ROOT)} and Playwright was blocked. "
            "Drop the XLSX from a real browser session and re-run."
        )
    body = f.read_bytes()
    print(f"[NC SF]   manual-drop pickup: {f.relative_to(ROOT)}  ({len(body):,} bytes)", flush=True)
    return body, "manual_drop", str(f.relative_to(ROOT))


# --------------------------------------------------------------------------
# Date extraction from the sheet name (PermittedSeptageForm_YYYYMMDD)
# --------------------------------------------------------------------------
def _content_date_from_sheet_name(sheet_name: str) -> str | None:
    m = _SHEET_DATE_RE.search(sheet_name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC)
        return format_datetime(dt, usegmt=True)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Upsert (same shape as the other loaders)
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
    out = {}
    for k, v in row.items():
        if pd.isna(v):
            out[str(k)] = None
        elif isinstance(v, pd.Timestamp):
            out[str(k)] = v.isoformat()
        else:
            out[str(k)] = str(v)
    return out


# --------------------------------------------------------------------------
# Main load
# --------------------------------------------------------------------------
def load() -> LoadResult:
    print("[NC SF] starting load", flush=True)
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
        print(f"[NC SF]   FETCH ERROR: {e}", flush=True)
        res.elapsed_sec = round(time.time() - t0, 1)
        return res

    # 2) Read the single sheet and pull the content-date from its name
    xl = pd.ExcelFile(io.BytesIO(body), engine="openpyxl")
    if len(xl.sheet_names) != 1:
        print(f"[NC SF]   WARNING: expected exactly 1 sheet, got {xl.sheet_names!r}", flush=True)
    sheet_name = xl.sheet_names[0]
    res.sheet_name = sheet_name
    res.last_modified = _content_date_from_sheet_name(sheet_name)
    print(
        f"[NC SF]   sheet={sheet_name!r}  content-date -> last_modified={res.last_modified!r}",
        flush=True,
    )

    df = pd.read_excel(
        io.BytesIO(body),
        sheet_name=sheet_name,
        engine="openpyxl",
        dtype=str,
    )
    res.rows_parsed = len(df)
    res.columns = list(df.columns)
    res.schema_hash = hash_payload(",".join(res.columns))
    print(
        f"[NC SF]   parsed shape={df.shape}  schema_hash={res.schema_hash[:12]}…",
        flush=True,
    )

    # 3) Out-of-state county count (informational; rows stay in the load)
    if COUNTY_COLUMN in df.columns:
        res.out_of_state_rows = int(
            (
                df[COUNTY_COLUMN].fillna("").astype(str).str.strip() == OUT_OF_STATE_COUNTY_VALUE
            ).sum()
        )
        print(
            f"[NC SF]   out-of-state firms (County={OUT_OF_STATE_COUNTY_VALUE!r}): "
            f"{res.out_of_state_rows}",
            flush=True,
        )

    # 4) DB session
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, SOURCE_SLUG)
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[NC SF]   scraper_run id={run_id} (source_id={source_id})", flush=True)

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
            http_status=None,  # manual_drop path: no HTTP status
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
        print(f"[NC SF]   ERROR during upsert: {err}", flush=True)
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
        f"[NC SF] done in {res.elapsed_sec}s via {res.fetch_path!r} — "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"out_of_state={res.out_of_state_rows} bytes={res.download_bytes:,} "
        f"last_modified={res.last_modified!r}",
        flush=True,
    )
    return res


def main() -> int:
    res = load()
    print("\n========== SUMMARY ==========")
    status = "OK" if not res.error else "FAIL"
    print(
        f"  [{status}] NC DEQ Septage Firm (via {res.fetch_path}): "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"out_of_state={res.out_of_state_rows} bytes={res.download_bytes:,} "
        f"elapsed={res.elapsed_sec}s last_modified={res.last_modified!r}"
    )
    if res.error:
        print(f"           error: {res.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
