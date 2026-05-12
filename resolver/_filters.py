"""Pre-resolution row exclusion filters. Three rules, pinned in
docs/build_log.md -> Phase 3 prep:

  1. **CWPState** for `epa_echo` — drop rows where CWPState is not in
     {TX, NC}. (~11 rows total: OK 3, VA 2, LA 2, SC 2, MD 1, AR 1.)
  2. **NOT CONSTRUCTED** for `tceq_msw_facilities_xls` — drop rows where
     raw_payload['Physical Site Status'] = 'NOT CONSTRUCTED' (256/1,494).
  3. **HHW Collection** for `nc_deq_solid_waste_facility_list` — drop rows
     where raw_payload['Activity'] = 'Collection' (30/435). Exception:
     keep if explicit hauler-receiving capability is documented in payload
     (none observed in current data; default exclude wins).

The filters run BEFORE matching. Filtered rows are not linked to any
canonical and not written to `facility_record_link`. The raw rows remain
in `raw_facility_record` (load-time fidelity is preserved).

A row excluded by a filter is reported in the run summary so the operator
can verify counts match expectations (e.g. 11 ECHO + 256 TCEQ + 30 NC SW
= 297 excluded out of 99,405 = 99,108 to canonicalize).
"""

from __future__ import annotations

from dataclasses import dataclass

from resolver._normalize import NormalizedRaw

EXCLUSION_REASONS: dict[str, str] = {
    "cwpstate_out_of_scope": "CWPState not in {TX, NC}",
    "not_constructed": "Physical Site Status = 'NOT CONSTRUCTED'",
    "hhw_collection": "NC SW Activity = 'Collection' (HHW citizen drop-off)",
}

# v1 coverage set; widen here when new states ship.
V1_STATES: frozenset[str] = frozenset({"TX", "NC"})


@dataclass(frozen=True)
class FilterDecision:
    """Result of running the filter chain on a single NormalizedRaw."""

    keep: bool
    reason: str | None  # None when keep=True; one of EXCLUSION_REASONS keys otherwise


def _hauler_receiving_capability(raw_payload: dict) -> bool:
    """The HHW Collection exception per build_log: include a Collection row
    with confidence='low' if explicit hauler-receiving capability is
    documented in the raw_payload. Today, the NC SW XLSX schema has no
    field that signals this — none of the 30 Collection rows have it.
    Future-proof: a payload key like 'AcceptsHaulerLoad' or a 'Notes' field
    saying 'hauler receiving' would flip this. Today returns False uniformly.
    """
    if not isinstance(raw_payload, dict):
        return False
    flag = raw_payload.get("AcceptsHaulerLoad")
    if isinstance(flag, str) and flag.strip().lower() in {"yes", "true", "y"}:
        return True
    notes = raw_payload.get("Notes") or ""
    return bool(isinstance(notes, str) and "hauler receiv" in notes.lower())


def apply_filters(raw: NormalizedRaw) -> FilterDecision:
    """Run the three per-source filters in order. First hit wins."""
    slug = raw.source_slug
    payload = raw.raw_payload or {}

    if slug == "epa_echo":
        state = (payload.get("CWPState") or "").strip().upper()
        if state and state not in V1_STATES:
            return FilterDecision(keep=False, reason="cwpstate_out_of_scope")
        return FilterDecision(keep=True, reason=None)

    if slug == "tceq_msw_facilities_xls":
        status = (payload.get("Physical Site Status") or "").strip().upper()
        if status == "NOT CONSTRUCTED":
            return FilterDecision(keep=False, reason="not_constructed")
        return FilterDecision(keep=True, reason=None)

    if slug == "nc_deq_solid_waste_facility_list":
        activity = (payload.get("Activity") or "").strip()
        if activity == "Collection":
            if _hauler_receiving_capability(payload):
                # Keep, but flag the canonical write to mark confidence='low'.
                # The decision shape returns keep=True; the resolver writes
                # the low-confidence flag at the canonicalize layer.
                return FilterDecision(keep=True, reason=None)
            return FilterDecision(keep=False, reason="hhw_collection")
        return FilterDecision(keep=True, reason=None)

    # No filters defined for nc_deq_non_discharge_facilities (1,259 rows all
    # kept), nc_deq_septage_firm_list (759 rows all kept), epa_cwns_2022
    # (3,132 rows all kept). The CWNS rows have STATE_CODE NULL at top-level
    # because PHYSICAL_LOCATION is nested; coverage was checked when the
    # loader ran (2,312 TX + 820 NC = 3,132).
    return FilterDecision(keep=True, reason=None)
