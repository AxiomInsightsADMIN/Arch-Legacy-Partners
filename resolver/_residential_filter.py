"""Residential-address-pattern filter for NC ND (Phase 4 follow-on
to the design pin at commit b18283b).

NC DEQ DWR Non-Discharge permits store the residential address in the
FACILITY name field (because NC DEQ removed the street column for
privacy — 47% of permits are single-family residences). The Phase-3
resolver's RapidFuzz score-based matcher then treats those address
strings as identity and merges:

  - the NC ND residential permit (e.g. `972 New Elam Church Rd. SFR`),
  - any NC SF septage business serving that same address,
  - and ECHO industrial NPDES rows that happen to share the address-
    like string in CWPName

into one canonical_facility, typed as
`private_regional_septage_facility` (the NC SF source override). That
conflates the residential permit-holder with the regulated hauler
firm and unrelated NPDES discharger.

The fix: detect SFR-pattern names from NC ND specifically, and bypass
the score-based match step entirely. The raw still goes through
ID-first match (so a shared PERMITNUMBER would still merge, but
PERMITNUMBER is single-source within NC ND so it only collapses
within-source duplicates — which it should). Standalone canonical
creation is the correct outcome — residential permits do not merge
with business entities.

`check_residential(raw)` returns a two-flag verdict:
  - matched=True   — the SFR regex matched the name
  - high_confidence=True — and the PERMIT_TYPE confirms residential

Both `matched` cases bypass score-based matching. The
`high_confidence` distinction is for the report: high-confidence
exclusions are silent skips; medium-confidence exclusions (regex
matched but PERMIT_TYPE doesn't confirm) get logged as
`residential_filter_review` so an operator can spot-check whether the
regex is over-matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from resolver._normalize import NormalizedRaw

# Detection regex per the Phase 4 design pin at commit b18283b.
#
# Pattern: starts with a number, has at least one word in between
# (street name + type), ends with one of the residential suffixes.
#
# Catches:
#   "2716 Weaver Hill Dr. SFR"
#   "972 New Elam Church Rd. SFR"
#   "100 Main St. Residence"
#   "55 Oak Lane Residential"
#   "1038 King Dr. SFR"
#
# Does NOT catch:
#   "Twelve Mile Creek WWTP"          (no leading digit, no SFR suffix)
#   "Lloyds Portable Toilet Rentals"  (no leading digit)
#   "EMA Resources Class A Residuals Program"   ("Residuals" not "Residential")
#   "RES Energy Group"                (RES is part of name, not standalone suffix)
#
# The non-greedy `.+?` between the leading number and the suffix
# allows multi-word street names without over-matching short business
# names with trailing initials.
_SFR_PATTERN = re.compile(
    r"^\s*\d+\s+.+?\s+"
    r"(SFR|SFD|S\.F\.R\.|RESIDENCE|RESIDENTIAL|RES\.|SF[RW]?|HOME)"
    r"\s*$",
    re.IGNORECASE,
)

# Source slug for NC ND. The filter applies only here.
NC_ND_SOURCE_SLUG = "nc_deq_non_discharge_facilities"

# The PERMIT_TYPE value that high-confidence-confirms a residential
# permit in NC ND (589 rows in the current data carry this value).
# Any other PERMIT_TYPE value reduces the regex match to
# medium-confidence — the filter still applies (bypass score-match)
# but the stats classify it for review.
NC_ND_RESIDENTIAL_PERMIT_TYPE = "Single-Family Residence Wastewater Irrigation"


@dataclass(frozen=True)
class ResidentialFilterResult:
    """Verdict from check_residential. Both flags drive separate paths
    in the resolver."""

    matched: bool  # True if the SFR regex matched
    high_confidence: bool  # True if PERMIT_TYPE also confirms residential


def check_residential(raw: NormalizedRaw) -> ResidentialFilterResult:
    """Return the residential-filter verdict for a NormalizedRaw.

    Only nc_deq_non_discharge_facilities rows can match; all other
    sources return (matched=False, high_confidence=False) immediately
    so the filter is a no-op for them.
    """
    if raw.source_slug != NC_ND_SOURCE_SLUG:
        return ResidentialFilterResult(matched=False, high_confidence=False)
    if not raw.name:
        return ResidentialFilterResult(matched=False, high_confidence=False)
    if not _SFR_PATTERN.match(raw.name):
        return ResidentialFilterResult(matched=False, high_confidence=False)
    # Regex matched. Check PERMIT_TYPE (stored in raw_facility_type_string
    # for NC ND per resolver/_normalize.py) for the high-confidence signal.
    permit_type = (raw.raw_facility_type_string or "").strip()
    high_conf = permit_type == NC_ND_RESIDENTIAL_PERMIT_TYPE
    return ResidentialFilterResult(matched=True, high_confidence=high_conf)
