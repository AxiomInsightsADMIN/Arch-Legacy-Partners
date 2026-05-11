"""
TCEQ MSW Active Facilities XLS loader — Phase 2 step 1.

Pulls the weekly-refreshed `msw-facilities-texas.xls` from
www.tceq.texas.gov and upserts one row per TCEQ permit/registration into
`raw_facility_record` with `source='tceq_msw_facilities_xls'`.

Source audit: docs/tceq_pdl_audit.md (Phase 2 first action).
Why XLS-direct: TCEQ application subdomains (www2/www3/www6/www15/www18)
are all robots-disallowed; the only allowed TCEQ path for bulk data is
the static `/assets/public/permitting/waste/msw/*.xls` published by the
MSW Data hub. Schema reference: TCEQ publication GI-613.

Refresh cadence: weekly, "each Friday morning" per the source page. We
capture the response's `Last-Modified` header into
`source_signature.last_modified` so the drift detector can distinguish
"source updated on cadence" from "source changed schema". If the header
is missing the column is NULL and we fall back to `response_byte_size`
as the cadence proxy.

Stable identifier: `Additional ID` column (TCEQ permit / registration /
notification number). Near-unique (1494/1496 unique in the 2026-05-11
sample; 1 null, 1 duplicated). Duplicates are tolerated as benign
upserts — Phase 3 canonical resolution dedupes by RN if needed.

Cross-state sanity: every row's `Near Phys Loc State` should be `TX`.
The loader counts non-TX rows and emits an audit warning, but does not
mutate or drop them (same handling as the ECHO 11-row anomaly).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import psycopg2.extras
import requests

# Make the project root importable when this module is run directly.
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
    "Axiom-Insights-ArchLegacy/0.1 (Phase-2 TCEQ MSW XLS loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/vnd.ms-excel,*/*"}

XLS_URL = "https://www.tceq.texas.gov/assets/public/permitting/waste/msw/msw-facilities-texas.xls"
SOURCE_SLUG = "tceq_msw_facilities_xls"
SOURCE_RECORD_ID_COLUMN = "Additional ID"
STATE_COLUMN = "Near Phys Loc State"  # 1496/1497 TX in 2026-05-11 sample
EXPECTED_STATE = "TX"
BATCH_SIZE = 500
TIMEOUT_DOWNLOAD = 120


@dataclass
class LoadResult:
    download_bytes: int = 0
    http_status: int | None = None
    last_modified: str | None = None
    rows_parsed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_skipped_no_id: int = 0
    rows_skipped_dupe_id: int = 0  # in-XLS Additional ID duplicates (last-write-wins)
    cross_state_rows: int = 0
    schema_hash: str | None = None
    columns: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------
# Fetch the XLS
# --------------------------------------------------------------------------
def fetch_xls() -> tuple[bytes, int, str | None]:
    """Returns (xls_bytes, http_status, last_modified_header_or_None)."""
    r = requests.get(XLS_URL, headers=HEADERS, timeout=TIMEOUT_DOWNLOAD)
    r.raise_for_status()
    lm = r.headers.get("Last-Modified")
    return r.content, r.status_code, lm


# --------------------------------------------------------------------------
# Parse with pandas + xlrd (the XLS is BIFF binary, magic D0 CF 11 E0)
# --------------------------------------------------------------------------
def parse_xls(xls_bytes: bytes) -> pd.DataFrame:
    """Parse the MSW XLS into a DataFrame of str values. Raises if the file
    is not a recognized BIFF .xls (catches the case where TCEQ silently
    switches to .xlsx or a different format)."""
    head = xls_bytes[:4]
    if head != b"\xd0\xcf\x11\xe0":
        # Surface this loudly — drift signal worth investigating
        raise RuntimeError(
            f"Downloaded file is not a BIFF .xls (magic={head!r}); "
            "TCEQ may have switched format. Source audit needed before "
            "re-running the loader."
        )
    return pd.read_excel(io.BytesIO(xls_bytes), engine="xlrd", dtype=str)


def schema_hash_for(df: pd.DataFrame) -> str:
    """Hash of the column header list for drift detection."""
    return hashlib.sha256(",".join(df.columns).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Cross-state sanity check
# --------------------------------------------------------------------------
def count_cross_state(df: pd.DataFrame) -> int:
    """Count rows whose state column is non-null AND != 'TX'."""
    if STATE_COLUMN not in df.columns:
        return 0
    col = df[STATE_COLUMN].dropna()
    return int((col.str.upper() != EXPECTED_STATE).sum())


# --------------------------------------------------------------------------
# Upsert (mirrors the federal-loader pattern)
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


# --------------------------------------------------------------------------
# Custom source_signature writer with last_modified support
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Per-state load (TX-only — XLS is TX-scoped at source)
# --------------------------------------------------------------------------
def _row_payload_json(row: pd.Series) -> dict:
    """Return a dict of the row, converting NaN to None and datetime to str."""
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
    print("[TCEQ MSW] starting load", flush=True)
    t0 = time.time()
    res = LoadResult()

    # 1) Fetch
    print(f"[TCEQ MSW]   GET {XLS_URL}", flush=True)
    xls_bytes, http_status, last_modified = fetch_xls()
    res.http_status = http_status
    res.download_bytes = len(xls_bytes)
    res.last_modified = last_modified
    print(
        f"[TCEQ MSW]   HTTP {http_status}  bytes={len(xls_bytes):,}  "
        f"last_modified={last_modified!r}",
        flush=True,
    )

    # 2) Parse
    df = parse_xls(xls_bytes)
    res.rows_parsed = len(df)
    res.columns = list(df.columns)
    res.schema_hash = schema_hash_for(df)
    print(
        f"[TCEQ MSW]   parsed shape={df.shape}  schema_hash={res.schema_hash[:12]}…",
        flush=True,
    )

    # 3) Cross-state sanity (informational; raw rows stay)
    res.cross_state_rows = count_cross_state(df)
    print(
        f"[TCEQ MSW]   cross-state rows (non-TX in {STATE_COLUMN!r}): {res.cross_state_rows}",
        flush=True,
    )

    # 4) DB session, scraper_run begin
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, SOURCE_SLUG)
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[TCEQ MSW]   scraper_run id={run_id} (source_id={source_id})", flush=True)

    # 5) Upsert
    # Postgres rejects `ON CONFLICT DO UPDATE` when a single statement's
    # VALUES contain duplicate conflict keys ("cannot affect row a second
    # time"). The 2026-05-11 XLS has 1 such pair (1494/1496 unique
    # Additional IDs). We dedupe in-Python with last-write-wins semantics
    # and count the drops so the anomaly stays visible. Per-key dedupe is
    # within a single load only; cross-load drift still reaches the DB
    # via the upsert path.
    by_sid: dict[str, tuple] = {}
    for _, row in df.iterrows():
        sid_raw = row.get(SOURCE_RECORD_ID_COLUMN)
        sid = None if pd.isna(sid_raw) else str(sid_raw).strip()
        if not sid:
            res.rows_skipped_no_id += 1
            continue
        payload = _row_payload_json(row)
        payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        ph = hash_payload(payload_str)
        if sid in by_sid:
            res.rows_skipped_dupe_id += 1
        by_sid[sid] = (source_id, sid, run_id, psycopg2.extras.Json(payload), ph)

    batch: list[tuple] = []
    err: str | None = None
    try:
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

        # 6) source_signature with Last-Modified header captured
        write_signature_with_last_modified(
            cur,
            source_id=source_id,
            run_id=run_id,
            http_status=http_status,
            byte_size=res.download_bytes,
            schema_hash=res.schema_hash,
            row_count=res.rows_parsed,
            last_modified=last_modified,
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
        print(f"[TCEQ MSW]   ERROR: {err}", flush=True)
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
        f"[TCEQ MSW] done in {res.elapsed_sec}s — "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"cross_state={res.cross_state_rows} "
        f"last_modified={res.last_modified!r}",
        flush=True,
    )
    return res


def main() -> int:
    res = load()
    print("\n========== SUMMARY ==========")
    status = "OK" if not res.error else "FAIL"
    print(
        f"  [{status}] TX (TCEQ MSW): "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped_no_id={res.rows_skipped_no_id} skipped_dupe_id={res.rows_skipped_dupe_id} "
        f"cross_state={res.cross_state_rows} "
        f"bytes={res.download_bytes:,} elapsed={res.elapsed_sec}s "
        f"last_modified={res.last_modified!r}"
    )
    if res.error:
        print(f"           error: {res.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
