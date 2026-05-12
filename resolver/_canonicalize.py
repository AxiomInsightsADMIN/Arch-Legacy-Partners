"""Canonical writer — INSERT/UPDATE canonical_facility, facility_record_link,
and field_provenance.

Design choice: the resolver maintains in-memory canonical state during the
pass and bulk-flushes to the DB in batches. This keeps the pass fast against
the Tokyo pooler and isolates the SQL surface in one place.

Field merge policy: **first non-null wins** ("fill-in-the-blanks"). A
later-arriving raw cannot overwrite a value already populated by an
earlier raw, even if the later raw is from a more-trusted source. The
processing order is therefore significant: more-trusted sources first
(CWNS -> ECHO -> TCEQ MSW -> NC ND -> NC SW -> NC SF) so their values
anchor the canonical row. Subsequent merges fill remaining NULLs.

(`canonical_facility_history` is not populated by the v1 resolver. The
schema exists for future field-level audit; provenance covers Phase 3.)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg2.extras

from resolver._normalize import NormalizedRaw

# Canonical-facility fields we know how to populate from NormalizedRaw.
# Order matches the canonical_facility schema columns roughly; used only
# for stable iteration in field_provenance.
CANONICAL_FIELDS: tuple[str, ...] = (
    "name",
    "street",
    "city",
    "state",
    "zip",
    "county",
    "latitude",
    "longitude",
    "phone",
    "email",
    "website",
    "frs_id",
    "npdes_id",
    "state_permit_id",
    "facility_type",
)


@dataclass
class CanonicalRowState:
    """In-memory image of a single canonical_facility row. Mutated as raws
    merge in via the 'first non-null wins' policy. Flushed to DB in batches."""

    canonical_id: str
    name: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    county: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    frs_id: str | None = None
    npdes_id: str | None = None
    state_permit_id: str | None = None
    facility_type: str | None = None  # canonical_type slug from category_map
    # tracking
    dirty: bool = True  # True = needs INSERT (new) or UPDATE (changed since last flush)
    persisted: bool = False  # True = at least one INSERT/UPDATE has been written

    def merge_first_non_null(self, raw: NormalizedRaw, *, state_permit: str | None) -> None:
        """Apply the field merge policy: fill blanks only."""
        for f in (
            "name",
            "street",
            "city",
            "state",
            "zip",
            "county",
            "latitude",
            "longitude",
            "phone",
            "email",
            "website",
            "frs_id",
            "npdes_id",
        ):
            if getattr(self, f) is None:
                v = getattr(raw, f, None)
                if v is not None:
                    setattr(self, f, v)
                    self.dirty = True
        if self.state_permit_id is None and state_permit:
            self.state_permit_id = state_permit
            self.dirty = True


@dataclass
class PendingLink:
    """One row to be inserted into facility_record_link."""

    raw_facility_record_id: int
    canonical_facility_id: str
    match_score: float | None
    match_method: str  # 'id_match' | 'rapidfuzz' | ... per the CHECK constraint


@dataclass
class PendingProvenance:
    """One row to be inserted into field_provenance. The resolver writes one
    row per (canonical, field, observed_raw) for fields that were populated
    from the raw."""

    canonical_facility_id: str
    field_name: str
    value: str | None
    source_url: str | None
    source_date: str | None
    extraction_method: str  # 'direct_scrape' (Phase 3 always uses this)
    confidence: str  # 'high' | 'medium' | 'low'


def new_canonical_id() -> str:
    """Mint a UUID for a new canonical. Generated in Python so the in-memory
    index can use it immediately without a round-trip to the DB."""
    return str(uuid.uuid4())


def derive_state_permit_id(raw: NormalizedRaw) -> str | None:
    """Pick the most-specific state-level permit ID available on the raw.
    Resolver writes this into canonical_facility.state_permit_id."""
    for field_name in (
        "tceq_additional_id",
        "nc_permit_number",
        "nc_facility_id",
        "nc_septage_permit",
    ):
        v = getattr(raw, field_name)
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# Bulk-flush SQL
# ---------------------------------------------------------------------------

CANONICAL_INSERT_SQL = """
INSERT INTO canonical_facility
    (id, name, street, city, state, zip, county,
     latitude, longitude, phone, email, website,
     frs_id, npdes_id, state_permit_id, facility_type)
VALUES %s
ON CONFLICT (id) DO UPDATE
   SET name             = COALESCE(canonical_facility.name,             EXCLUDED.name),
       street           = COALESCE(canonical_facility.street,           EXCLUDED.street),
       city             = COALESCE(canonical_facility.city,             EXCLUDED.city),
       state            = COALESCE(canonical_facility.state,            EXCLUDED.state),
       zip              = COALESCE(canonical_facility.zip,              EXCLUDED.zip),
       county           = COALESCE(canonical_facility.county,           EXCLUDED.county),
       latitude         = COALESCE(canonical_facility.latitude,         EXCLUDED.latitude),
       longitude        = COALESCE(canonical_facility.longitude,        EXCLUDED.longitude),
       phone            = COALESCE(canonical_facility.phone,            EXCLUDED.phone),
       email            = COALESCE(canonical_facility.email,            EXCLUDED.email),
       website          = COALESCE(canonical_facility.website,          EXCLUDED.website),
       frs_id           = COALESCE(canonical_facility.frs_id,           EXCLUDED.frs_id),
       npdes_id         = COALESCE(canonical_facility.npdes_id,         EXCLUDED.npdes_id),
       state_permit_id  = COALESCE(canonical_facility.state_permit_id,  EXCLUDED.state_permit_id),
       facility_type    = COALESCE(canonical_facility.facility_type,    EXCLUDED.facility_type),
       last_seen_at     = NOW()
"""

LINK_INSERT_SQL = """
INSERT INTO facility_record_link
    (raw_facility_record_id, canonical_facility_id, match_score, match_method)
VALUES %s
ON CONFLICT (raw_facility_record_id) DO UPDATE
   SET canonical_facility_id = EXCLUDED.canonical_facility_id,
       match_score           = EXCLUDED.match_score,
       match_method          = EXCLUDED.match_method,
       linked_at             = NOW()
"""

PROVENANCE_INSERT_SQL = """
INSERT INTO field_provenance
    (canonical_facility_id, field_name, value, source_url, source_date,
     extraction_method, confidence)
VALUES %s
"""


def flush_canonicals(cur, rows: list[CanonicalRowState]) -> int:
    """Bulk-upsert a batch of canonical_facility rows. Mutates each row's
    `persisted=True` and `dirty=False`. Returns count flushed."""
    if not rows:
        return 0
    batch = [
        (
            r.canonical_id,
            r.name,
            r.street,
            r.city,
            r.state,
            r.zip,
            r.county,
            r.latitude,
            r.longitude,
            r.phone,
            r.email,
            r.website,
            r.frs_id,
            r.npdes_id,
            r.state_permit_id,
            r.facility_type,
        )
        for r in rows
    ]
    psycopg2.extras.execute_values(
        cur,
        CANONICAL_INSERT_SQL,
        batch,
        page_size=500,
    )
    for r in rows:
        r.persisted = True
        r.dirty = False
    return len(rows)


def flush_links(cur, rows: list[PendingLink]) -> int:
    if not rows:
        return 0
    batch = [
        (r.raw_facility_record_id, r.canonical_facility_id, r.match_score, r.match_method)
        for r in rows
    ]
    psycopg2.extras.execute_values(cur, LINK_INSERT_SQL, batch, page_size=500)
    return len(rows)


def flush_provenance(cur, rows: list[PendingProvenance]) -> int:
    if not rows:
        return 0
    batch = [
        (
            r.canonical_facility_id,
            r.field_name,
            r.value,
            r.source_url,
            r.source_date,
            r.extraction_method,
            r.confidence,
        )
        for r in rows
    ]
    psycopg2.extras.execute_values(cur, PROVENANCE_INSERT_SQL, batch, page_size=500)
    return len(rows)


def make_provenance_rows(
    *,
    canonical_id: str,
    raw: NormalizedRaw,
    raw_source_url: str | None,
    raw_source_date: str | None,
    category_confidence: str,
) -> list[PendingProvenance]:
    """Emit one provenance row per non-null normalized field on the raw.
    Resolver writes these for every raw it links, regardless of whether
    the value 'won' on the canonical row (provenance is multi-source by
    design). confidence='high' for direct_scrape fields; the category map
    passes its own confidence for facility_type."""
    out: list[PendingProvenance] = []
    raw_payload_str = json.dumps(raw.raw_payload, default=str)[:0] if raw.raw_payload else None
    # We don't store raw_payload_str (too large); the field_provenance table
    # records per-field text values. The full raw_payload lives on raw_facility_record.
    del raw_payload_str

    for f in CANONICAL_FIELDS:
        if f == "facility_type":
            v = getattr(raw, "raw_facility_type_string", None)
            confidence = category_confidence
        elif f == "state_permit_id":
            v = derive_state_permit_id(raw)
            confidence = "high"
        else:
            v = getattr(raw, f, None)
            confidence = "high"
        if v is None:
            continue
        out.append(
            PendingProvenance(
                canonical_facility_id=canonical_id,
                field_name=f,
                value=str(v),
                source_url=raw_source_url,
                source_date=raw_source_date,
                extraction_method="direct_scrape",
                confidence=confidence,
            )
        )
    return out
