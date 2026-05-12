"""
EPA ECHO CWA loader — Phase 1 Day 2 step 1.

Pulls active CWA-permitted facilities from the EPA ECHO REST API for each
target state and upserts raw observations into `raw_facility_record` with
source='epa_echo'. Honors the ECHO bulk-data path (NOT the
robots-disallowed search-results pages).

Flow per logical refresh (covering BOTH states in one scraper_run):
  for each state:
    1) get_facilities -> QueryID + total QueryRows
    2) get_download   -> CSV with column codes 1..34 (34 useful columns)
    3) Parse CSV rows into JSON payloads
    4) Upsert into raw_facility_record with no-change detection via payload_hash
    capture per-state stats (bytes downloaded, rows parsed, schema hash) into
    in-memory StateLoadResult.
  After all states done:
    5) Write ONE consolidated source_signature covering all states
    6) Mark scraper_run='success' with the aggregate counts

**Single-signature-per-refresh design (Phase 5 follow-on).** The original
Phase-1 implementation wrote one source_signature per state, which made
the drift detector compare "latest NC slice" vs "prior TX slice" and
trigger a 72% row-count-drop pause every month. Locked decision 8.7's
drift comparison expects same-shape signatures across runs, so we now
consolidate: aggregate bytes + rows across states, assert schema_hash
matches across states (it does for federal sources by construction),
and emit one signature row at the end of the run.

Idempotent: re-running is safe and is a no-op on unchanged rows. The
ON CONFLICT clause skips the row UPDATE when payload_hash matches.

Read-only over the wire; only DB writes touch raw_facility_record,
scraper_run, source_signature — all per the locked architectural decisions.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

# Make the project root importable when this module is run directly. See
# the parallel note in epa_cwns.py for why.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scrapers._loader_utils import (  # noqa: E402
    begin_run,
    db_connect,
    finish_run,
    get_source_id,
    hash_payload,
    write_signature,
)

ROOT = _PROJECT_ROOT

USER_AGENT = (
    "Axiom-Insights-ArchLegacy/0.1 (Phase-1 EPA ECHO loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT_SETUP = 30
TIMEOUT_DOWNLOAD = 180  # large CSV; allow time to stream
BATCH_SIZE = 500

ECHO_BASE = "https://echodata.epa.gov/echo/cwa_rest_services"
# Codes 1..34 cover everything the canonical schema needs:
#   RegistryID (FRS), SourceID (NPDES), CWPName, CWPStreet, CWPCity,
#   CWPState, CWPZip, CWPCounty, FacLat, FacLong,
#   CWPFacilityTypeIndicator (POTW/NON-POTW), design+actual flow, etc.
QCOLUMNS = ",".join(str(i) for i in range(1, 35))


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------
@dataclass
class StateLoadResult:
    state: str
    http_status_setup: int | None
    http_status_download: int | None
    query_rows_reported: int | None
    rows_parsed: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_skipped_no_id: int
    bytes_downloaded: int
    schema_hash: str | None
    elapsed_sec: float
    error: str | None = None


# --------------------------------------------------------------------------
# ECHO REST helpers
# --------------------------------------------------------------------------
def echo_setup(state: str) -> tuple[int, int, str]:
    """Step 1: get_facilities. Returns (http_status, query_rows_total, qid)."""
    r = requests.get(
        f"{ECHO_BASE}.get_facilities",
        params={"output": "JSON", "p_st": state, "p_act": "Y", "responseset": "1"},
        headers=HEADERS,
        timeout=TIMEOUT_SETUP,
    )
    r.raise_for_status()
    results = r.json().get("Results") or {}
    return r.status_code, int(results.get("QueryRows", 0)), str(results.get("QueryID"))


def echo_download(qid: str) -> tuple[int, str, int]:
    """Step 2: get_download CSV. Returns (http_status, csv_text, byte_len)."""
    r = requests.get(
        f"{ECHO_BASE}.get_download",
        params={"qid": qid, "qcolumns": QCOLUMNS, "output": "CSV"},
        headers={**HEADERS, "Accept": "text/csv"},
        timeout=TIMEOUT_DOWNLOAD,
    )
    r.raise_for_status()
    return r.status_code, r.text, len(r.content)


# --------------------------------------------------------------------------
# Upsert helpers
# --------------------------------------------------------------------------
def _source_record_id_for_row(row: dict) -> str | None:
    """Stable per-source key. ECHO rows: prefer SourceID (NPDES) → RegistryID
    (FRS) → bail. We need a stable, unique key per facility."""
    sid = (row.get("SourceID") or "").strip()
    if sid:
        return sid
    rid = (row.get("RegistryID") or "").strip()
    if rid:
        return f"FRS:{rid}"
    return None


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
    """Execute the batch INSERT/UPDATE. Returns (inserted, updated) counts.

    NOTE: rows whose payload_hash matched the existing row are *not* returned
    by RETURNING (the WHERE-clause filter prevents the UPDATE). We compute
    'unchanged' as batch_size - inserted - updated outside this fn.
    """
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
# Per-state load — caller owns scraper_run + source_signature lifecycle
# --------------------------------------------------------------------------
def load_state(state: str, *, conn, source_id: int, run_id: int) -> StateLoadResult:
    """Pull + upsert one state's worth of ECHO data using the caller's
    shared `conn` + `run_id`. Does NOT call begin_run/write_signature/
    finish_run — that's the orchestrator's job (`run_all` below).
    Returns per-state stats so `run_all` can aggregate them into a
    single consolidated source_signature."""
    print(f"\n[{state}] starting ECHO load", flush=True)
    t0 = time.time()
    cur = conn.cursor()

    # 1) setup query
    setup_status, qrows, qid = echo_setup(state)
    print(f"[{state}] setup: HTTP {setup_status}, QueryRows={qrows}, QID={qid}", flush=True)

    # 2) download CSV
    dl_status, csv_text, byte_len = echo_download(qid)
    print(f"[{state}] download: HTTP {dl_status}, bytes={byte_len:,}", flush=True)

    # 3) parse + 4) upsert
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    schema_hash = hash_payload(",".join(headers))
    print(f"[{state}] parsed {len(headers)} columns; schema_hash={schema_hash[:12]}…", flush=True)

    rows_parsed = 0
    rows_inserted = 0
    rows_updated = 0
    rows_unchanged = 0
    rows_skipped = 0
    batch: list[tuple] = []
    err = None

    try:
        for row in reader:
            rows_parsed += 1
            src_rec_id = _source_record_id_for_row(row)
            if not src_rec_id:
                rows_skipped += 1
                continue
            payload_str = json.dumps(row, ensure_ascii=False, sort_keys=True)
            payload_hash = hash_payload(payload_str)
            batch.append((source_id, src_rec_id, run_id, psycopg2.extras.Json(row), payload_hash))
            if len(batch) >= BATCH_SIZE:
                ins, upd = _flush_batch(cur, batch)
                effective = ins + upd
                rows_unchanged += len(batch) - effective
                rows_inserted += ins
                rows_updated += upd
                conn.commit()
                batch = []
                if rows_parsed % 5000 == 0:
                    print(
                        f"[{state}]   processed={rows_parsed:,}  inserted={rows_inserted:,}  "
                        f"updated={rows_updated:,}  unchanged={rows_unchanged:,}",
                        flush=True,
                    )
        if batch:
            ins, upd = _flush_batch(cur, batch)
            effective = ins + upd
            rows_unchanged += len(batch) - effective
            rows_inserted += ins
            rows_updated += upd
            conn.commit()
    except Exception as e:
        err = str(e)
        print(f"[{state}] ERROR: {err}", flush=True)
        conn.rollback()
        # Do NOT mark the run failed here; the orchestrator decides. We
        # bubble the error out by populating res.error and returning.
    finally:
        cur.close()

    elapsed = round(time.time() - t0, 1)
    res = StateLoadResult(
        state=state,
        http_status_setup=setup_status,
        http_status_download=dl_status,
        query_rows_reported=qrows,
        rows_parsed=rows_parsed,
        rows_inserted=rows_inserted,
        rows_updated=rows_updated,
        rows_unchanged=rows_unchanged,
        rows_skipped_no_id=rows_skipped,
        bytes_downloaded=byte_len,
        schema_hash=schema_hash,
        elapsed_sec=elapsed,
        error=err,
    )
    print(
        f"[{state}] done in {elapsed}s — "
        f"parsed={res.rows_parsed:,} inserted={res.rows_inserted:,} "
        f"updated={res.rows_updated:,} unchanged={res.rows_unchanged:,} "
        f"skipped={res.rows_skipped_no_id}",
        flush=True,
    )
    return res


# --------------------------------------------------------------------------
# Orchestrator — one scraper_run + one consolidated source_signature
# --------------------------------------------------------------------------
def run_all(states: list[str]) -> int:
    """Open one connection, begin one scraper_run, walk all states, write
    one consolidated source_signature, finalize the run. Returns shell
    exit code (0 success, 1 any state failed)."""
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, "epa_echo")
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[run] scraper_run id={run_id} (source_id={source_id})", flush=True)

    results: list[StateLoadResult] = []
    consolidated_bytes = 0
    consolidated_rows = 0
    consolidated_hash: str | None = None
    consolidated_http: int | None = None
    fatal_err: str | None = None

    try:
        for state in states:
            res = load_state(state, conn=conn, source_id=source_id, run_id=run_id)
            results.append(res)
            if res.error:
                fatal_err = f"state {state} failed: {res.error}"
                break
            consolidated_bytes += res.bytes_downloaded
            consolidated_rows += res.rows_parsed
            # Schema hash must match across states for a single source.
            # ECHO returns the same CSV column header set per state, so a
            # mismatch indicates real schema drift — fail the whole run.
            if consolidated_hash is None:
                consolidated_hash = res.schema_hash
            elif res.schema_hash and res.schema_hash != consolidated_hash:
                fatal_err = (
                    f"schema_hash mismatch across states: "
                    f"{consolidated_hash[:12]} vs {res.schema_hash[:12]}"
                )
                break
            if consolidated_http is None:
                consolidated_http = res.http_status_download

        if fatal_err:
            raise RuntimeError(fatal_err)

        sig_cur = conn.cursor()
        try:
            write_signature(
                sig_cur,
                source_id,
                run_id,
                http_status=consolidated_http,
                byte_size=consolidated_bytes,
                schema_hash=consolidated_hash,
                row_count=consolidated_rows,
            )
            finish_run(
                sig_cur,
                run_id,
                "success",
                rows_in=consolidated_rows,
                rows_inserted=sum(r.rows_inserted for r in results),
                rows_updated=sum(r.rows_updated for r in results),
            )
            conn.commit()
        finally:
            sig_cur.close()

        print(
            f"\n[run] consolidated signature: bytes={consolidated_bytes:,} "
            f"rows={consolidated_rows:,} schema_hash={consolidated_hash[:12]}…",
            flush=True,
        )
    except Exception as e:
        fatal_err = fatal_err or str(e)
        print(f"[run] FATAL: {fatal_err}", flush=True)
        conn.rollback()
        try:
            err_cur = conn.cursor()
            finish_run(
                err_cur,
                run_id,
                "failed",
                rows_in=consolidated_rows,
                rows_inserted=sum(r.rows_inserted for r in results),
                rows_updated=sum(r.rows_updated for r in results),
                error_message=fatal_err,
            )
            conn.commit()
            err_cur.close()
        except Exception:
            pass
    finally:
        conn.close()

    print("\n========== SUMMARY ==========")
    fail = bool(fatal_err)
    for r in results:
        status = "OK" if not r.error else "FAIL"
        print(
            f"  [{status}] {r.state}: parsed={r.rows_parsed:,} "
            f"inserted={r.rows_inserted:,} updated={r.rows_updated:,} "
            f"unchanged={r.rows_unchanged:,} skipped={r.rows_skipped_no_id} "
            f"in {r.elapsed_sec}s"
        )
        if r.error:
            print(f"         error: {r.error}")
            fail = True
    return 1 if fail else 0


def main(states: list[str]) -> int:
    return run_all(states)


if __name__ == "__main__":
    sts = sys.argv[1:] or ["TX", "NC"]
    sys.exit(main(sts))
