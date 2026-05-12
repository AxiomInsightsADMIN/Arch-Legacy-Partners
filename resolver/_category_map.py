"""Raw facility-type string -> one of seven canonical category slugs from
config/facility_types.yaml.

Match precedence per the YAML header:
  1. regex_rules (most specific wins; first listed wins on tie)
  2. synonyms (exact match, case-insensitive)
  3. not_synonyms hits demote the candidate to 'unknown'

Returns (canonical_type, confidence). Confidence:
  - 'high' when the raw string explicitly matches a synonym or regex
  - 'medium' when a source-specific override applies
  - 'low'  when no match found (canonical_type is None)

Loaders MUST normalize through this module (locked decision 8.9). The
resolver writes the canonical category at canonical_facility.facility_type
and records the raw string at field_provenance under
'facility_type_raw' so the original is preserved for re-pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "config" / "facility_types.yaml"

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class CategoryMatch:
    canonical_type: str | None  # one of 7 slugs or None
    confidence: Confidence
    source_rule: str | None  # 'synonym', 'regex', 'source_override', or None


# ---------------------------------------------------------------------------
# Source-specific overrides — applied when YAML lookup returns nothing
# ---------------------------------------------------------------------------
# These map source-classifier values to canonical type slugs. They are
# 'medium' confidence because they're derived from per-source classifiers
# rather than from text matching against the YAML controlled vocabulary.
#
# Pinned mappings:
#   - TCEQ Physical Type codes per GI-613 (see build_log Phase 2 step 1)
#   - NC SW Activity codes per build_log Phase 2 step 3
#   - NC SF Activity is always "Hauler" -> private_regional_septage_facility
#   - NC ND PERMIT_TYPE strings -> land_application_site for "503"-tagged rows
# ---------------------------------------------------------------------------

TCEQ_PHYSICAL_TYPE_OVERRIDES: dict[str, str] = {
    # Composting
    "5RC": "composting_facility",
    "5RCX": "composting_facility",
    # Anaerobic Digester / Beneficial Gas Recovery
    "9GR": "anaerobic_digester",
    # Transfer Station
    "5TS": "transfer_station",
    "5LV": "transfer_station",
    "5CC": "transfer_station",  # Citizens Collection Stations — TCEQ codes as 7-subtype
    # Liquid Waste -> Private Septage (partial; subtype mapping is heuristic)
    "5GG": "private_regional_septage_facility",
    "5TL": "private_regional_septage_facility",
    "5GM": "private_regional_septage_facility",
}

NC_SW_ACTIVITY_OVERRIDES: dict[str, str] = {
    "Trans": "transfer_station",
    "Compost": "composting_facility",
    # Activity='Collection' is filtered out for v1 (HHW exception); included
    # here for completeness in case the filter exception fires.
    "Collection": "transfer_station",
}

NC_SF_ACTIVITY_OVERRIDES: dict[str, str] = {
    "Hauler": "private_regional_septage_facility",
}

# NC ND PERMIT_TYPE substrings -> canonical type. Substring match because
# the strings include qualifiers (e.g. "Land Application of Residual
# Solids (503)" and "(503 Exempt)" both should map).
NC_ND_PERMIT_TYPE_SUBSTRING_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("residual solids", "land_application_site"),
    ("wastewater irrigation", "private_regional_septage_facility"),
    ("reclaimed water", "potw_receiving_station"),
    ("closed-loop recycle", None),  # explicitly out-of-scope; no canonical
    ("high rate infiltration", None),
)


# ---------------------------------------------------------------------------
# YAML lookup
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_yaml() -> dict:
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def _compiled_rules() -> list[tuple[str, list[str], list[re.Pattern], list[str]]]:
    """Return list of (canonical_slug, synonyms_lower, compiled_regex_patterns,
    not_synonyms_lower) for each of the 7 types. Cached at module level."""
    cfg = _load_yaml()
    types = cfg.get("types") or {}
    out = []
    for slug, body in types.items():
        synonyms = [s.lower() for s in (body.get("synonyms") or [])]
        regex_rules = [re.compile(p, flags=re.IGNORECASE) for p in (body.get("regex_rules") or [])]
        not_synonyms = [s.lower() for s in (body.get("not_synonyms") or [])]
        out.append((slug, synonyms, regex_rules, not_synonyms))
    return out


def _yaml_lookup(raw_type: str) -> CategoryMatch:
    """Run regex_rules then synonyms across the YAML. Apply not_synonyms
    demotion."""
    lowered = raw_type.lower()

    # 1) Regex pass — most specific wins; first match by file order on tie.
    for slug, _, regexes, not_synonyms in _compiled_rules():
        for pattern in regexes:
            if pattern.search(raw_type):
                # Check not_synonyms — if the raw string contains a
                # not_synonym for this slug, demote to unknown.
                if any(ns in lowered for ns in not_synonyms):
                    continue
                return CategoryMatch(slug, "high", "regex")

    # 2) Synonym pass — exact (case-insensitive) only.
    for slug, synonyms, _, not_synonyms in _compiled_rules():
        if lowered in synonyms:
            if any(ns in lowered for ns in not_synonyms):
                continue
            return CategoryMatch(slug, "high", "synonym")

    return CategoryMatch(None, "low", None)


# ---------------------------------------------------------------------------
# Source overrides
# ---------------------------------------------------------------------------


def _source_override(source_slug: str, raw_type: str) -> CategoryMatch | None:
    """Apply per-source classifier-code overrides. Returns None if no
    override matches; CategoryMatch with confidence='medium' otherwise."""
    if not raw_type:
        return None
    raw = raw_type.strip()

    # Try exact then prefix (some TCEQ codes have suffixes like '1 AE & 4 AE').
    if source_slug == "tceq_msw_facilities_xls" and raw in TCEQ_PHYSICAL_TYPE_OVERRIDES:
        return CategoryMatch(TCEQ_PHYSICAL_TYPE_OVERRIDES[raw], "medium", "source_override")

    if source_slug == "nc_deq_solid_waste_facility_list" and raw in NC_SW_ACTIVITY_OVERRIDES:
        return CategoryMatch(NC_SW_ACTIVITY_OVERRIDES[raw], "medium", "source_override")

    if source_slug == "nc_deq_septage_firm_list" and raw in NC_SF_ACTIVITY_OVERRIDES:
        return CategoryMatch(NC_SF_ACTIVITY_OVERRIDES[raw], "medium", "source_override")

    if source_slug == "nc_deq_non_discharge_facilities":
        lowered = raw.lower()
        for substr, slug in NC_ND_PERMIT_TYPE_SUBSTRING_OVERRIDES:
            if substr in lowered:
                if slug is None:
                    return CategoryMatch(None, "low", "source_override")
                return CategoryMatch(slug, "medium", "source_override")

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_to_canonical(*, source_slug: str, raw_type: str | None) -> CategoryMatch:
    """Top-level entry point. Tries:
      1. YAML lookup (regex then synonym, both case-insensitive)
      2. Source-specific classifier code override

    Returns CategoryMatch(canonical_type=None, confidence='low', source_rule=None)
    if neither rule fires.
    """
    if not raw_type:
        return CategoryMatch(None, "low", None)

    # YAML first — it carries the high-confidence text-pattern rules.
    via_yaml = _yaml_lookup(raw_type)
    if via_yaml.canonical_type is not None:
        return via_yaml

    # Fall back to per-source classifier-code overrides.
    via_override = _source_override(source_slug, raw_type)
    if via_override is not None:
        return via_override

    return CategoryMatch(None, "low", None)
