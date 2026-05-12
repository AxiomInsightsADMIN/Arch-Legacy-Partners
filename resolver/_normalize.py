"""Per-source raw_payload -> NormalizedRaw facade.

Every source has its own JSONB shape (ECHO is flat with `CWP*` keys, CWNS is
nested by sub-table, TCEQ MSW is flat with `Phys Addr *` keys, NC sources
vary). The resolver only wants to think about a unified facade: name,
address parts, coords, stable IDs, raw facility-type string. This module
does that translation.

Adding a new source: write a `_normalize_<slug>(row)` function and register
it in `NORMALIZERS`. The resolver auto-routes on `row.source_slug`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedRaw:
    """Unified facade over a single `raw_facility_record` row, plus the
    stable identifiers used for ID-first matching."""

    raw_id: int
    source_slug: str
    source_record_id: str

    # Common identity fields
    name: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None  # USPS 2-letter
    zip: str | None = None
    county: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    # Stable identifiers (any may be None)
    frs_id: str | None = None
    npdes_id: str | None = None
    cwns_id: str | None = None
    tceq_additional_id: str | None = None
    tceq_rn: str | None = None
    nc_permit_number: str | None = None
    nc_facility_id: str | None = None
    nc_septage_permit: str | None = None

    # Type string for YAML lookup (Phase 4 enrichment refines)
    raw_facility_type_string: str | None = None

    # Contact info
    phone: str | None = None
    email: str | None = None
    website: str | None = None

    # Original payload (kept for provenance, not for matching)
    raw_payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Field-level helpers
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")


def _clean(v: Any) -> str | None:
    """Convert to stripped str. None / empty / whitespace -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return _WHITESPACE.sub(" ", s)


def _coord(v: Any) -> float | None:
    """Parse a coordinate. Returns None on any failure or out-of-range value."""
    if v is None:
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if f == 0.0:
        # ECHO uses 0/0 as a sentinel for "no geocoder result"; same for some
        # state sources. Treat as missing.
        return None
    if not (-180.0 <= f <= 180.0):
        return None
    return f


def _normalize_zip(v: Any) -> str | None:
    """Keep ZIP+4 if present, strip trailing dashes/whitespace."""
    s = _clean(v)
    if not s:
        return None
    s = s.replace(" ", "")
    if s.endswith("-"):
        s = s[:-1]
    return s


def _upper2(v: Any) -> str | None:
    """USPS state code; uppercased; None if not exactly 2 alpha chars."""
    s = _clean(v)
    if not s:
        return None
    s = s.upper()
    if len(s) != 2 or not s.isalpha():
        return None
    return s


# ---------------------------------------------------------------------------
# Per-source normalizers
# ---------------------------------------------------------------------------


def _normalize_epa_echo(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """EPA ECHO CWA. Flat shape with `CWP*` keys."""
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="epa_echo",
        source_record_id=source_record_id,
        name=_clean(p.get("CWPName")),
        street=_clean(p.get("CWPStreet")),
        city=_clean(p.get("CWPCity")),
        state=_upper2(p.get("CWPState")),
        zip=_normalize_zip(p.get("CWPZip")),
        county=_clean(p.get("CWPCounty")),
        latitude=_coord(p.get("CWPLatitude") or p.get("FacLat") or p.get("Lat")),
        longitude=_coord(p.get("CWPLongitude") or p.get("FacLong") or p.get("Long")),
        npdes_id=_clean(p.get("SourceID")),
        frs_id=_clean(p.get("RegistryID")),
        raw_facility_type_string=_clean(p.get("CWPSICCodes")),
        raw_payload=p,
    )


def _normalize_epa_cwns(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """EPA CWNS 2022. Nested shape; PHYSICAL_LOCATION is a single object,
    FACILITIES is an array but the first element is the primary name."""
    pl = p.get("PHYSICAL_LOCATION") or {}
    if isinstance(pl, list) and pl:
        pl = pl[0]
    elif not isinstance(pl, dict):
        pl = {}

    facilities = p.get("FACILITIES") or []
    if isinstance(facilities, dict):
        facilities = [facilities]
    primary_name = None
    if facilities:
        primary_name = _clean(facilities[0].get("FACILITY_NAME"))

    # NPDES permit may appear under FACILITY_PERMIT (array). The actual key
    # in CWNS 2022 is `PERMIT_NUMBER` with `PERMIT_SOURCE='NPDES'` flagging
    # which entries are NPDES (vs `Non-NPDES` state-issued permits). CWNS
    # data is sparse here: only 1,736 of 3,132 CWNS rows have a
    # FACILITY_PERMIT entry at all, and ~100 of those have PERMIT_SOURCE=NPDES.
    # Cross-source overlap with ECHO is therefore small (~83 unique NPDES);
    # we still extract because every collapse improves canonical quality.
    npdes = None
    fp = p.get("FACILITY_PERMIT") or []
    if isinstance(fp, list):
        for entry in fp:
            if not isinstance(entry, dict):
                continue
            permit_source = (entry.get("PERMIT_SOURCE") or "").strip().upper()
            if permit_source != "NPDES":
                continue
            candidate = _clean(entry.get("PERMIT_NUMBER"))
            if candidate:
                npdes = candidate
                break

    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="epa_cwns_2022",
        source_record_id=source_record_id,
        name=primary_name,
        street=_clean(pl.get("ADDRESS")),
        city=_clean(pl.get("CITY")),
        state=_upper2(pl.get("STATE_CODE")),
        zip=_normalize_zip(pl.get("ZIP_CODE")),
        county=_clean(pl.get("COUNTY_NAME")),
        latitude=_coord(pl.get("LATITUDE")),
        longitude=_coord(pl.get("LONGITUDE")),
        cwns_id=_clean(pl.get("CWNS_ID") or source_record_id),
        npdes_id=npdes,
        raw_facility_type_string=_collect_cwns_types(p),
        raw_payload=p,
    )


def _collect_cwns_types(p: dict) -> str | None:
    """Join the FACILITY_TYPE values from FACILITY_TYPES into a single string
    so the YAML lookup can grep for matching synonyms. Returns None if no
    types are present."""
    ft = p.get("FACILITY_TYPES") or []
    if isinstance(ft, dict):
        ft = [ft]
    if not isinstance(ft, list):
        return None
    types = [_clean(entry.get("FACILITY_TYPE")) for entry in ft if isinstance(entry, dict)]
    types = [t for t in types if t]
    if not types:
        return None
    return "; ".join(types)


def _normalize_tceq_msw(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """TCEQ MSW XLS. Flat shape with `Phys Addr *` keys."""
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="tceq_msw_facilities_xls",
        source_record_id=source_record_id,
        name=_clean(p.get("Site Name")),
        street=_clean(p.get("Phys Addr Line 1")),
        city=_clean(p.get("Phys Addr City")),
        state=_upper2(p.get("Phys Addr State")),
        zip=_normalize_zip(p.get("Phys Addr Zip")),
        county=_clean(p.get("County")),
        latitude=_coord(p.get("Latitude")),
        longitude=_coord(p.get("Longitude")),
        tceq_additional_id=_clean(p.get("Additional ID")),
        tceq_rn=_clean(p.get("RN")),
        raw_facility_type_string=_clean(p.get("Physical Type")),
        raw_payload=p,
    )


def _normalize_nc_deq_non_discharge(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """NC DEQ DWR Non-Discharge ArcGIS view. No address line; no lat/lng
    (geometry stripped). County is the geographic attribution."""
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="nc_deq_non_discharge_facilities",
        source_record_id=source_record_id,
        name=_clean(p.get("FACILITY") or p.get("OWNER")),
        state="NC",  # NC by ArcGIS construction
        county=_clean(p.get("COUNTY")),
        nc_permit_number=_clean(p.get("PERMITNUMBER")),
        raw_facility_type_string=_clean(p.get("PERMIT_TYPE")),
        website=_clean(p.get("URL")),
        raw_payload=p,
    )


def _normalize_nc_deq_solid_waste(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """NC DEQ DWM Solid Waste XLSX. Has full address + lat/lng for 97.7% of
    rows."""
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="nc_deq_solid_waste_facility_list",
        source_record_id=source_record_id,
        name=_clean(p.get("Facility Name")),
        street=_clean(p.get("Address")),
        city=_clean(p.get("City")),
        state=_upper2(p.get("State")) or "NC",
        zip=_normalize_zip(p.get("Zip")),
        county=_clean(p.get("County")),
        latitude=_coord(p.get("Latitude")),
        longitude=_coord(p.get("Longitude")),
        nc_facility_id=_clean(p.get("Facility Id")),
        raw_facility_type_string=_clean(p.get("Activity")),
        phone=_clean(p.get("Phone")),
        raw_payload=p,
    )


def _normalize_nc_deq_septage_firm(raw_id: int, source_record_id: str, p: dict) -> NormalizedRaw:
    """NC DEQ DWM Septage Firm XLSX. No state column; Address is a single
    line with `street; city` semicolon-delimited. No coords. County is `-`
    for out-of-state firms."""
    addr_raw = _clean(p.get("Address"))
    street = None
    city = None
    if addr_raw and ";" in addr_raw:
        parts = [a.strip() for a in addr_raw.split(";", 1)]
        street, city = parts[0] or None, parts[1] or None
    elif addr_raw:
        street = addr_raw

    county = _clean(p.get("County"))
    state = "NC" if county and county != "-" else None
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="nc_deq_septage_firm_list",
        source_record_id=source_record_id,
        name=_clean(p.get("Name")),
        street=street,
        city=city,
        state=state,
        county=county if county != "-" else None,
        nc_septage_permit=_clean(p.get("Permit")),
        raw_facility_type_string=_clean(p.get("Activity")),  # uniformly "Hauler"
        phone=_clean(p.get("Phone")),
        raw_payload=p,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

NORMALIZERS: dict[str, callable] = {
    "epa_echo": _normalize_epa_echo,
    "epa_cwns_2022": _normalize_epa_cwns,
    "tceq_msw_facilities_xls": _normalize_tceq_msw,
    "nc_deq_non_discharge_facilities": _normalize_nc_deq_non_discharge,
    "nc_deq_solid_waste_facility_list": _normalize_nc_deq_solid_waste,
    "nc_deq_septage_firm_list": _normalize_nc_deq_septage_firm,
}


def normalize(
    *, raw_id: int, source_slug: str, source_record_id: str, raw_payload: dict
) -> NormalizedRaw:
    """Dispatch to the per-source normalizer. Raises KeyError for unknown
    source slugs so misconfigured raws surface loudly instead of getting
    silently dropped."""
    fn = NORMALIZERS.get(source_slug)
    if fn is None:
        raise KeyError(
            f"No normalizer registered for source slug {source_slug!r}. "
            f"Add a _normalize_<slug>() in resolver/_normalize.py and "
            f"register it in NORMALIZERS."
        )
    return fn(raw_id, source_record_id, raw_payload)


# ---------------------------------------------------------------------------
# Geocoding address synthesis
# ---------------------------------------------------------------------------
# Shared helper used by the Census Geocoder backfill (one-time pass that
# populates geocoding_cache) and by the resolver (looks up the same cache
# at runtime when a raw has no native coords).
#
# Both must use the SAME string for a given raw so address_hash collides
# on the cache lookup. Definition lives here, in the normalize module,
# because it depends on the per-source address shape we already
# understand.
# ---------------------------------------------------------------------------


def synthesize_address_for_geocoding(raw: NormalizedRaw) -> str | None:
    """Produce the one-line address used by Census Geocoder for a given raw,
    or None if the raw lacks sufficient address data.

    The output is fed directly to `orchestration.geocoder.
    geocode_with_state_check(address=..., state=raw.state, ...)`. The
    `geocoder` module normalizes (trim/upper-case) and hashes the string
    before caching; we don't need to pre-normalize here.

    Source-specific shapes:
      - NC SF:  "<street>, <city>, NC"   when both populated. The source's
                `Address` column is "street; city" semicolon-delimited and
                the normalizer already splits it into raw.street + raw.city.
      - NC ND:  "<FACILITY>, <COUNTY>, NC"  when both populated. NC ND has
                no street address column; the FACILITY name is often
                address-like (e.g. '972 New Elam Church Rd. SFR' for
                single-family-residence permits). Most won't match, but
                the SFR rows have a decent shot.
      - Other sources: not handled — they either already have native coords
                or don't have a useful address to geocode.
    """
    if raw.source_slug == "nc_deq_septage_firm_list":
        if raw.street and raw.city:
            state = raw.state or "NC"
            return f"{raw.street}, {raw.city}, {state}"
        return None

    if raw.source_slug == "nc_deq_non_discharge_facilities":
        if raw.name and raw.county:
            return f"{raw.name}, {raw.county}, NC"
        return None

    return None
