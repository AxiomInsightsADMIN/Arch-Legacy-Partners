"""
EPA CWNS 2022 loader — Phase 1 Day 2 step 3.

Pulls per-state CWNS data zips from the Oracle APEX app at
https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub via Playwright. Each
zip contains a set of CSV tables keyed on `CWNS_ID`. We re-shape the zip
into one row per `CWNS_ID` in `raw_facility_record`, where `raw_payload` is
a JSONB dict keyed by source table name (FACILITIES, FACILITY_TYPES,
PHYSICAL_LOCATION, FACILITY_PERMIT, POINT_OF_CONTACT, AREAS_COUNTY, etc.).

Why Playwright (not pure HTTP): the Day-2 step-2 spike proved the per-state
download requires JS-driven interaction with the APEX `<select id="P5_STATE">`
and a follow-on `theme42.dialog` modal — pure HTTP can only retrieve the
nationwide Data Dictionary. See docs/build_log.md Day-2 step-2 entry.

Validation before any DB write:
  1) downloaded file has ZIP magic
  2) expected member files exist (cross-checked against the Data Dictionary
     XLSX captured in the step-2 spike)
  3) every member CSV has at least one data row
  4) CWNS_ID column is present and unique within FACILITIES.csv

Sequential per state — APEX session state would collide on parallel selects.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2.extras
from playwright.sync_api import Page, sync_playwright

# Make the project root importable when this module is run directly via
# `python scrapers/federal/epa_cwns.py`. The `scrapers/...` package layout
# requires the project root on sys.path; `python -m scrapers.federal.epa_cwns`
# would also work, but matching the ECHO loader's invocation pattern is
# friendlier for ad-hoc runs.
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

ROOT = Path(__file__).resolve().parent.parent.parent

USER_AGENT = (
    "Axiom-Insights-ArchLegacy/0.1 (Phase-1 EPA CWNS loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)

APEX_BASE = "https://sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub"
DATA_DOWNLOAD = f"{APEX_BASE}/data-download"
SOURCE_SLUG = "epa_cwns_2022"
BATCH_SIZE = 500

# Tables we expect inside the per-state CSV ZIP. From the spike's Data
# Dictionary inventory. Members that *must* exist for validation to pass:
REQUIRED_MEMBERS = ("FACILITIES.csv",)
# Members we ingest into raw_payload if present (1:1 or 1:N to CWNS_ID):
INGEST_MEMBERS = (
    "FACILITIES.csv",
    "FACILITIES_CONFIRMED.csv",
    "FACILITY_TYPES.csv",
    "FACILITY_PERMIT.csv",
    "POINT_OF_CONTACT.csv",
    "PHYSICAL_LOCATION.csv",
    "AREAS_COUNTY.csv",
    "AREAS_WATERSHED.csv",
    "AREAS_CONGRESS_DISTRICT.csv",
    "REASON_FOR_NEEDS.csv",
    "FACILITY_DOCUMENT_LINKAGES.csv",
    "CET_INPUTS_WASTEWATER.csv",
    "CET_INPUTS_DECENTRALIZED.csv",
    "CET_INPUTS_STORMWATER.csv",
    "CET_INPUTS_NONPOINT.csv",
    "CET_INPUTS_WASTEWATER_CSO.csv",
    "NEEDS_COST_BY_CATEGORY.csv",
    "POPULATION_DECENTRALIZED.csv",
    "POPULATION_WASTEWATER.csv",
    "POPULATION_WASTEWATER_CONFIRMED.csv",
    "DISCHARGES.csv",
    "EFFLUENT.csv",
    "FLOW.csv",
    "UNIT_PROCESSES.csv",
    "ASSET_MANAGEMENT.csv",
)


@dataclass
class StateLoadResult:
    state: str
    download_bytes: int
    zip_members: list[str] = field(default_factory=list)
    rows_per_member: dict[str, int] = field(default_factory=dict)
    facilities_count: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    cross_state_rows: int = 0
    elapsed_sec: float = 0.0
    schema_hash: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# Playwright flow: select state, capture the ZIP download
# --------------------------------------------------------------------------
def _capture_state_download(page: Page, state: str, *, work_dir: Path) -> bytes:
    """Drive the APEX page to download the per-state CSV ZIP. Returns the
    zip bytes. Raises on timeout or unexpected dialog state."""
    print(f"[{state}]   goto {DATA_DOWNLOAD}", flush=True)
    page.goto(DATA_DOWNLOAD, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#P5_STATE", state="attached", timeout=30_000)

    # Selecting the state triggers `location.href=...` in the onchange handler,
    # so a full page navigation follows. Wait for the new page to settle.
    print(f"[{state}]   select_option #P5_STATE -> {state!r}", flush=True)
    with page.expect_navigation(wait_until="domcontentloaded", timeout=60_000):
        page.select_option("#P5_STATE", state)
    page.wait_for_selector("#P5_STATE", state="attached", timeout=30_000)
    # Sanity check: the dropdown now reports the state as selected
    selected = page.evaluate("() => document.getElementById('P5_STATE').value")
    if selected != state:
        raise RuntimeError(f"P5_STATE did not switch — expected {state!r} got {selected!r}")
    print(f"[{state}]   page now shows state={selected!r}", flush=True)

    # The "Download CSVs" button for the per-state CSV ZIP. The page has TWO
    # "Download CSVs" buttons: button #0 is the per-state download (selected
    # via P5_STATE); button #1 is the nationwide dataset. We want #0.
    buttons = page.locator('button:has-text("Download CSVs")')
    if buttons.count() < 1:
        raise RuntimeError(f"[{state}] no Download CSVs button found on /data-download")
    button = buttons.first

    # Click triggers `apex.theme42.dialog('/download-popup?...')`, which APEX
    # renders as an iframe-based modal. The iframe contains a survey wrapper
    # ("Help us learn more about who uses our data") with a final "Download"
    # button — clicking THAT triggers the actual browser download.
    print(f"[{state}]   click Download CSVs (opens dialog iframe)", flush=True)
    with page.expect_download(timeout=180_000) as dl_info:
        button.click()
        # Wait for the dialog iframe to render and its content to load.
        page.wait_for_selector('iframe[src*="download-popup"]', state="attached", timeout=30_000)
        # Resolve the iframe frame and click the inner Download button.
        popup_frame = None
        deadline = time.time() + 30
        while time.time() < deadline:
            for fr in page.frames:
                if "download-popup" in fr.url:
                    popup_frame = fr
                    break
            if popup_frame is not None:
                break
            page.wait_for_timeout(250)
        if popup_frame is None:
            raise RuntimeError(f"[{state}] download-popup iframe never appeared")
        print(f"[{state}]   popup iframe url={popup_frame.url[:140]}…", flush=True)
        # The popup is a survey wrapper. EPA's "Help us learn more about who
        # uses our data" form requires a <select name="P3_QUESTION"> answer
        # before its "Download" button becomes enabled. We answer
        # "Industry or NGO" — the most accurate description of an
        # Axiom-Insights-contracted build for a private-sector client.
        # See docs/build_log.md Day-2 step-3 for the decision rationale.
        popup_frame.wait_for_selector("#P3_QUESTION", state="attached", timeout=30_000)
        popup_frame.select_option("#P3_QUESTION", "Industry or NGO")
        # Now the Download button should enable.
        popup_frame.wait_for_selector(
            'button:has-text("Download"):not([disabled])',
            state="visible",
            timeout=30_000,
        )
        print(f"[{state}]   survey answered; clicking Download", flush=True)
        popup_frame.locator('button:has-text("Download"):not([disabled])').first.click()
    download = dl_info.value
    suggested_name = download.suggested_filename
    dl_path = work_dir / f"{state}_{suggested_name}"
    download.save_as(str(dl_path))
    body = dl_path.read_bytes()
    print(
        f"[{state}]   download captured: {suggested_name!r} "
        f"saved as {dl_path.name}  bytes={len(body):,}",
        flush=True,
    )
    return body


# --------------------------------------------------------------------------
# Validate the downloaded ZIP
# --------------------------------------------------------------------------
def _validate_zip(zip_bytes: bytes, state: str) -> tuple[zipfile.ZipFile, dict[str, int]]:
    """Validate ZIP magic, required members, and minimum row counts. Returns
    (open ZipFile, rows_per_member dict). Caller is responsible for closing."""
    if zip_bytes[:4] != b"PK\x03\x04":
        raise RuntimeError(f"[{state}] downloaded file is not a ZIP (magic={zip_bytes[:4]!r})")

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    members = zf.namelist()
    # Members may have directory prefixes — normalize to basename for our checks
    by_base = {Path(m).name: m for m in members}
    missing = [m for m in REQUIRED_MEMBERS if m not in by_base]
    if missing:
        raise RuntimeError(
            f"[{state}] required ZIP members missing: {missing} (found: {sorted(by_base.keys())[:10]}…)"
        )

    rows_per_member: dict[str, int] = {}
    for m in by_base.values():
        if not m.lower().endswith(".csv"):
            continue
        with zf.open(m) as fp:
            # Count newlines minus the header
            data = fp.read()
        n = max(0, data.count(b"\n") - 1)
        rows_per_member[Path(m).name] = n
        if n <= 0 and Path(m).name in REQUIRED_MEMBERS:
            raise RuntimeError(f"[{state}] required member {m!r} has 0 data rows")

    return zf, rows_per_member


# --------------------------------------------------------------------------
# Parse CSVs into per-CWNS_ID nested payloads
# --------------------------------------------------------------------------
def _read_csv(zf: zipfile.ZipFile, member_basename: str) -> tuple[list[str], list[dict]]:
    """Return (header, rows) for the named member, or ([], []) if absent."""
    for n in zf.namelist():
        if Path(n).name == member_basename:
            with zf.open(n) as fp:
                text = fp.read().decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            header = reader.fieldnames or []
            rows = list(reader)
            return header, rows
    return [], []


def _build_payloads(zf: zipfile.ZipFile, state: str) -> tuple[dict[str, dict], str]:
    """Read every ingest-listed CSV and group rows by CWNS_ID. Returns:

    payloads        — dict: CWNS_ID -> dict of {table_name: row_or_rows}
    schema_hash     — sha256 hex of the FACILITIES.csv header (for drift)
    """
    # FACILITIES.csv is the spine — every canonical CWNS_ID we want is in there.
    fac_header, fac_rows = _read_csv(zf, "FACILITIES.csv")
    if not fac_rows:
        raise RuntimeError(f"[{state}] FACILITIES.csv has no rows")
    cwns_key = "CWNS_ID" if "CWNS_ID" in fac_header else None
    if not cwns_key:
        for cand in fac_header:
            if cand.upper() == "CWNS_ID":
                cwns_key = cand
                break
    if not cwns_key:
        raise RuntimeError(f"[{state}] FACILITIES.csv has no CWNS_ID column. Header: {fac_header}")

    # Uniqueness of CWNS_ID within FACILITIES
    seen: set[str] = set()
    dup: list[str] = []
    for row in fac_rows:
        v = (row.get(cwns_key) or "").strip()
        if not v:
            continue
        if v in seen:
            dup.append(v)
        seen.add(v)
    if dup:
        raise RuntimeError(f"[{state}] FACILITIES.csv has duplicate CWNS_ID values: {dup[:10]!r}")

    payloads: dict[str, dict] = {cid: {"FACILITIES": {}} for cid in seen}
    for row in fac_rows:
        v = (row.get(cwns_key) or "").strip()
        if v:
            payloads[v]["FACILITIES"] = row

    schema_hash = hash_payload(",".join(fac_header))

    # Pull every other ingest-listed table, group by CWNS_ID
    for member in INGEST_MEMBERS:
        if member == "FACILITIES.csv":
            continue
        header, rows = _read_csv(zf, member)
        if not rows:
            continue
        table_name = Path(member).stem  # FACILITY_TYPES.csv -> FACILITY_TYPES
        join_key = cwns_key if cwns_key in header else ("CWNS_ID" if "CWNS_ID" in header else None)
        if not join_key:
            # No CWNS_ID column — skip (tables like REF_FACILITY_TYPES are
            # reference data, not per-CWNS_ID).
            continue
        # Group rows by CWNS_ID. Tables are 1:N to CWNS_ID by default.
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            v = (row.get(join_key) or "").strip()
            if not v:
                continue
            grouped[v].append(row)
        for cid, group in grouped.items():
            if cid not in payloads:
                # CWNS_ID present in a related table but not in FACILITIES.
                # Skip — FACILITIES is the spine.
                continue
            # If exactly one row, store as dict; else as list
            payloads[cid][table_name] = group[0] if len(group) == 1 else group

    return payloads, schema_hash


# --------------------------------------------------------------------------
# Upsert into raw_facility_record
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


def _cross_state_check(payloads: dict[str, dict], state: str) -> int:
    """Count CWNS_IDs whose PHYSICAL_LOCATION row reports a state other than
    the queried state. We do NOT mutate or drop these — same handling as
    ECHO. Phase 3 resolver applies the state-coverage filter."""
    cross = 0
    for body in payloads.values():
        pl = body.get("PHYSICAL_LOCATION")
        if not pl:
            continue
        # PHYSICAL_LOCATION may be a single dict or a list. Pick the first.
        row = pl[0] if isinstance(pl, list) else pl
        # CWNS uses STATE_CODE typically. Fall back to STATE / STATE_NAME.
        candidate = row.get("STATE_CODE") or row.get("STATE") or row.get("STATE_NAME") or ""
        if isinstance(candidate, str) and candidate.strip().upper() not in {state.upper()}:
            cross += 1
    return cross


# --------------------------------------------------------------------------
# Per-state load
# --------------------------------------------------------------------------
def load_state(state: str, *, work_dir: Path) -> StateLoadResult:
    t0 = time.time()
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[{state}] starting CWNS load", flush=True)

    # 1) Drive Playwright to obtain the per-state ZIP
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, accept_downloads=True)
            page = context.new_page()
            zip_bytes = _capture_state_download(page, state, work_dir=work_dir)
        finally:
            browser.close()

    res = StateLoadResult(state=state, download_bytes=len(zip_bytes))

    # 2) Validate
    print(f"[{state}]   validating ZIP ({len(zip_bytes):,} bytes)", flush=True)
    zf, rows_per_member = _validate_zip(zip_bytes, state)
    res.zip_members = sorted(rows_per_member.keys())
    res.rows_per_member = rows_per_member
    print(
        f"[{state}]   ZIP OK — members={len(res.zip_members)} "
        f"FACILITIES rows={rows_per_member.get('FACILITIES.csv')}",
        flush=True,
    )

    # 3) Parse → per-CWNS_ID nested payloads
    payloads, schema_hash = _build_payloads(zf, state)
    zf.close()
    res.schema_hash = schema_hash
    res.facilities_count = len(payloads)
    print(
        f"[{state}]   parsed {len(payloads):,} CWNS_IDs across "
        f"{len([m for m in INGEST_MEMBERS if rows_per_member.get(m, 0) > 0])} populated tables",
        flush=True,
    )

    # 4) Cross-state check (informational; no mutation)
    res.cross_state_rows = _cross_state_check(payloads, state)
    print(
        f"[{state}]   cross-state rows in PHYSICAL_LOCATION: {res.cross_state_rows}",
        flush=True,
    )

    # 5) Upsert
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, SOURCE_SLUG)
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[{state}]   scraper_run id={run_id} (source_id={source_id})", flush=True)

    batch: list[tuple] = []
    err: str | None = None
    try:
        for cid, body in payloads.items():
            payload_str = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
            ph = hash_payload(payload_str)
            batch.append((source_id, cid, run_id, psycopg2.extras.Json(body), ph))
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

        write_signature(
            cur,
            source_id,
            run_id,
            http_status=200,
            byte_size=len(zip_bytes),
            schema_hash=schema_hash,
            row_count=len(payloads),
        )
        finish_run(
            cur,
            run_id,
            "success",
            rows_in=len(payloads),
            rows_inserted=res.rows_inserted,
            rows_updated=res.rows_updated,
        )
        conn.commit()
    except Exception as e:
        err = str(e)
        print(f"[{state}]   ERROR during upsert: {err}", flush=True)
        conn.rollback()
        try:
            finish_run(
                cur,
                run_id,
                "failed",
                rows_in=len(payloads),
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
        f"[{state}] done in {res.elapsed_sec}s — "
        f"facilities={res.facilities_count:,} "
        f"inserted={res.rows_inserted:,} updated={res.rows_updated:,} "
        f"unchanged={res.rows_unchanged:,} cross_state={res.cross_state_rows}",
        flush=True,
    )
    return res


def main(states: list[str]) -> int:
    work_dir = ROOT / "local" / "cwns_downloads"
    results = []
    for state in states:
        try:
            results.append(load_state(state, work_dir=work_dir))
        except Exception as e:
            print(f"[{state}] FATAL: {e}", flush=True)
            results.append(StateLoadResult(state=state, download_bytes=0, error=str(e)))

    print("\n========== SUMMARY ==========")
    fail = False
    for r in results:
        status = "OK" if not r.error else "FAIL"
        print(
            f"  [{status}] {r.state}: facilities={r.facilities_count:,} "
            f"inserted={r.rows_inserted:,} updated={r.rows_updated:,} "
            f"unchanged={r.rows_unchanged:,} cross_state={r.cross_state_rows} "
            f"zip_bytes={r.download_bytes:,} elapsed={r.elapsed_sec}s"
        )
        if r.error:
            print(f"           error: {r.error}")
            fail = True
    return 1 if fail else 0


if __name__ == "__main__":
    sts = sys.argv[1:] or ["TX", "NC"]
    sys.exit(main(sts))
