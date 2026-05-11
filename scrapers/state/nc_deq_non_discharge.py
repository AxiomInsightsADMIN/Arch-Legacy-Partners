"""
NC DEQ DWR Non-Discharge Permits loader — Phase 2 step 2.

Pulls the NPDES_Non_Discharge_Permits FeatureServer layer (NC DEQ's
public ArcGIS Online org via services2.arcgis.com) and upserts one
feature per row into `raw_facility_record` with
`source='nc_deq_non_discharge_facilities'`.

Layer reached via the AGOL item lookup chain documented in the NC DEQ
audit (docs/nc_deq_audit.md): the public DWR Locator Map Experience
references the web map item, which references this FeatureServer.

Schema (verified 2026-05-12):
  - 16 fields including PERMITNUMBER (string, the stable NC state
    permit id, format `WQ\\d{7}`), PERMIT_TYPE, PERMIT_STATUS,
    ORIGINAL_ISSUED_DT, PERMIT_EFFECTIVE_DATE, PERMIT_EXPIRATION_DT,
    FACILITY, FACILITY_STATUS, OWNER, OWNER_TYPE, MAJOR, COUNTY,
    REGION, LAST_INSPECTION_DT, URL, ObjectId
  - geometryType esriGeometryPoint, spatialReference wkid 3857
  - maxRecordCount 10000, supportsPagination true
  - feature count at probe time: 1,259
  - editingInfo.dataLastEditDate is the layer freshness signal we
    capture into source_signature.last_modified (converted to RFC 7231
    so the column shape matches HTTP Last-Modified values from other
    loaders).

Stable identifier: PERMITNUMBER (per Ryan's preference order
state-permit-id → facility-id → OBJECTID; PERMITNUMBER is the state
permit id and is required by the schema).

Cross-state sanity: this is NC DEQ's view; the layer is geographically
NC by source. We count any row whose COUNTY is empty/null (no state
column to assert TX/NC directly; the cross-state shape doesn't apply
the way it did for ECHO).

Geometry handling: the FeatureServer's layer type is
`esriGeometryPoint` but the public `(View)` we hit deliberately
strips geometry from every feature response — 0/1000 of the features
in the 2026-05-12 probe returned a non-null `geometry` object. This
is almost certainly a privacy decision by NC DEQ (≈47% of the 1,259
rows are "Single-Family Residence Wastewater Irrigation" — exact
lat/lng of homes would be PII). We still request `returnGeometry=true`
and `outSR=4326` for forward-compat (if NC DEQ ever exposes geometry,
our requests pick it up); the loader writes `_geometry` into
raw_payload only when present. Phase 3 canonical resolution leaves
`canonical_facility.latitude/longitude` NULL for these rows and uses
COUNTY as the only geographic attribution.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

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
    "Axiom-Insights-ArchLegacy/0.1 (Phase-2 NC DEQ Non-Discharge loader; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
TIMEOUT = 60

# FeatureServer endpoint. The service name has parens — we URL-encode
# them defensively even though `requests` typically passes them through.
FS_BASE = (
    "https://services2.arcgis.com/kCu40SDxsCGcuUWO/arcgis/rest/services/"
    + quote("NPDES_Non_Discharge_Permits_(View)", safe="()")
    + "/FeatureServer"
)
LAYER_URL = f"{FS_BASE}/0"

SOURCE_SLUG = "nc_deq_non_discharge_facilities"
SOURCE_RECORD_ID_FIELD = "PERMITNUMBER"
OBJECT_ID_FIELD = "ObjectId"
PAGE_SIZE = 1000  # well under the layer's maxRecordCount of 10000
BATCH_SIZE = 500  # DB batch size, independent of ArcGIS page size


@dataclass
class LoadResult:
    feature_count_reported: int | None = None
    rows_parsed: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_skipped_no_id: int = 0
    rows_skipped_dupe_id: int = 0
    null_county_rows: int = 0
    schema_hash: str | None = None
    columns: list[str] = field(default_factory=list)
    bytes_received_total: int = 0
    page_count: int = 0
    last_modified: str | None = None
    elapsed_sec: float = 0.0
    error: str | None = None


# --------------------------------------------------------------------------
# ArcGIS REST helpers
# --------------------------------------------------------------------------
def fetch_layer_metadata() -> tuple[dict, int]:
    """Return (layer JSON, bytes_received)."""
    r = requests.get(LAYER_URL, params={"f": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json(), len(r.content)


def fetch_count() -> int:
    r = requests.get(
        f"{LAYER_URL}/query",
        params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return int(r.json().get("count", 0))


def fetch_page(offset: int, count: int) -> tuple[list[dict], int]:
    """Return ([feature dicts], bytes_received). Each feature is the
    `attributes` dict; geometry is requested but stored in the payload
    too for forward compat."""
    r = requests.get(
        f"{LAYER_URL}/query",
        params={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",  # request lat/long in WGS84 for downstream consistency
            "resultOffset": str(offset),
            "resultRecordCount": str(count),
            "orderByFields": OBJECT_ID_FIELD,
            "f": "json",
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("features", []), len(r.content)


# --------------------------------------------------------------------------
# Freshness signal: dataLastEditDate (Unix ms) → RFC 7231 string
# --------------------------------------------------------------------------
def ms_to_http_date(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)
        return format_datetime(dt, usegmt=True)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Upsert
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


def _feature_to_payload(feature: dict) -> dict:
    """Combine attributes + geometry into a single dict for raw_payload."""
    attrs = dict(feature.get("attributes", {}) or {})
    geom = feature.get("geometry")
    if geom is not None:
        attrs["_geometry"] = geom  # prefixed so it never collides with a real column
    return attrs


# --------------------------------------------------------------------------
# Main load
# --------------------------------------------------------------------------
def load() -> LoadResult:
    print("[NC NDP] starting load", flush=True)
    t0 = time.time()
    res = LoadResult()

    # 1) Layer metadata + count
    print(f"[NC NDP]   GET {LAYER_URL}?f=json", flush=True)
    layer, b = fetch_layer_metadata()
    res.bytes_received_total += b
    fields = [f["name"] for f in (layer.get("fields") or [])]
    res.columns = fields
    res.schema_hash = hash_payload(",".join(fields))
    print(
        f"[NC NDP]   layer: {layer.get('name')!r}  fields={len(fields)}  "
        f"schema_hash={res.schema_hash[:12]}…",
        flush=True,
    )
    einfo = layer.get("editingInfo") or {}
    res.last_modified = ms_to_http_date(einfo.get("dataLastEditDate"))
    print(
        f"[NC NDP]   editingInfo.dataLastEditDate -> last_modified={res.last_modified!r}",
        flush=True,
    )

    total = fetch_count()
    res.feature_count_reported = total
    print(f"[NC NDP]   feature count: {total:,}", flush=True)

    # 2) DB setup
    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()
    source_id = get_source_id(cur, SOURCE_SLUG)
    run_id = begin_run(cur, source_id)
    conn.commit()
    print(f"[NC NDP]   scraper_run id={run_id} (source_id={source_id})", flush=True)

    # 3) Paginated query + upsert. In-Python dedupe + batched DB writes.
    by_sid: dict[str, tuple] = {}
    err: str | None = None
    try:
        offset = 0
        while True:
            features, b = fetch_page(offset, PAGE_SIZE)
            res.bytes_received_total += b
            res.page_count += 1
            if not features:
                break
            print(
                f"[NC NDP]   page {res.page_count}: offset={offset} "
                f"got {len(features)} features ({b:,} bytes)",
                flush=True,
            )
            for feat in features:
                res.rows_parsed += 1
                payload = _feature_to_payload(feat)
                sid_raw = payload.get(SOURCE_RECORD_ID_FIELD)
                sid = None if sid_raw in (None, "") else str(sid_raw).strip()
                if not sid:
                    res.rows_skipped_no_id += 1
                    continue
                if (payload.get("COUNTY") or "").strip() == "":
                    res.null_county_rows += 1
                payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                ph = hash_payload(payload_str)
                if sid in by_sid:
                    res.rows_skipped_dupe_id += 1
                by_sid[sid] = (source_id, sid, run_id, psycopg2.extras.Json(payload), ph)
            if len(features) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        # Flush all rows in BATCH_SIZE chunks
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
            http_status=200,
            byte_size=res.bytes_received_total,
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
        print(f"[NC NDP]   ERROR: {err}", flush=True)
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
        f"[NC NDP] done in {res.elapsed_sec}s — "
        f"reported={res.feature_count_reported:,} parsed={res.rows_parsed:,} "
        f"inserted={res.rows_inserted:,} updated={res.rows_updated:,} "
        f"unchanged={res.rows_unchanged:,} skipped_no_id={res.rows_skipped_no_id} "
        f"skipped_dupe_id={res.rows_skipped_dupe_id} null_county={res.null_county_rows} "
        f"pages={res.page_count} bytes={res.bytes_received_total:,} "
        f"last_modified={res.last_modified!r}",
        flush=True,
    )
    return res


def main() -> int:
    res = load()
    print("\n========== SUMMARY ==========")
    status = "OK" if not res.error else "FAIL"
    print(
        f"  [{status}] NC DEQ Non-Discharge: "
        f"reported={res.feature_count_reported:,} parsed={res.rows_parsed:,} "
        f"inserted={res.rows_inserted:,} updated={res.rows_updated:,} "
        f"unchanged={res.rows_unchanged:,} skipped_no_id={res.rows_skipped_no_id} "
        f"skipped_dupe_id={res.rows_skipped_dupe_id} null_county={res.null_county_rows} "
        f"bytes={res.bytes_received_total:,} pages={res.page_count} "
        f"elapsed={res.elapsed_sec}s last_modified={res.last_modified!r}"
    )
    if res.error:
        print(f"           error: {res.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
