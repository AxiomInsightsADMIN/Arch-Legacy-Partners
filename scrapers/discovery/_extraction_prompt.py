"""Prompt template for Phase 4.5 step C discovery extraction.

Separate prompt + prompt_hash from the Phase 4 acceptance-flag enrichment
(enrichment/_prompt.py) so the llm_enrichment_cache keys do not collide.
Discovery extraction reads full web-page text (truncated to ~10K tokens)
and emits a structured list of facility candidates per the Phase 4.5
step C brief.

Prompt is templated on (category, state) — each URL's extraction call
carries the queried category + state into the system prompt so the
model can apply the state filter and category-specific guidance.

The Evidence Quotation Rule is carried over from Phase 4 v1.1.1: every
evidence string must be a literal substring of the page text shown to
the model. No paraphrasing, no reconstruction.

PROMPT_VERSION is part of the cache key. Bump on every meaningful change.
"""

from __future__ import annotations

import hashlib

PROMPT_VERSION = "discovery-v1.0.0"

# Mapping from source_category to the canonical facility_type enum the
# resolver expects. county_manhole_program is ambiguous between
# private_regional_septage_facility and potw_receiving_station; the model
# picks based on text evidence.
CATEGORY_TO_ALLOWED_TYPES: dict[str, list[str]] = {
    "county_manhole_program": [
        "private_regional_septage_facility",
        "potw_receiving_station",
    ],
    "tx_private_regional_septage": [
        "private_regional_septage_facility",
    ],
    "tx_land_application_site": [
        "land_application_site",
    ],
    "nc_anaerobic_digester": [
        "anaerobic_digester",
    ],
}

# All canonical facility_type values the resolver knows about (informational;
# the tool schema constrains the model's output via per-category allowed lists).
CANONICAL_FACILITY_TYPES = [
    "private_regional_septage_facility",
    "potw_receiving_station",
    "land_application_site",
    "anaerobic_digester",
    "composting_facility",
    "transfer_station",
]


SYSTEM_PROMPT_TEMPLATE = """You are a facility extractor for a wastewater \
facility database. You are reading a web page that was discovered via a \
Brave search query targeting:

- source category: {category}
- target state:    {state} ({state_abbr})
- allowed facility types for candidates from this page: {allowed_types}

Your task: extract facility records that meet ALL of these criteria.

CRITERIA — every candidate facility must satisfy all four:

1. WASTEWATER DOMAIN. The facility operates in wastewater / septage / \
hauler / disposal / biosolids / anaerobic-digester. Wastewater haulers \
ONLY — freight or equipment haulers do NOT count even if the word \
"hauler" appears on the page. Heavy-equipment transporters, freight \
brokers, and trucking companies that do not name a wastewater service \
filter out.

2. STATE FILTER. The facility must be physically located in {state}. A \
page that names {state} in passing but describes a facility in another \
state filters out. If the facility's address is in another state, \
discard. If no address is visible but the page itself is published by a \
{state}-jurisdictional source AND the operator is named, that is \
acceptable evidence of {state} location.

3. CATEGORY MATCH. The facility_type field of the candidate must be one \
of the allowed types listed above. If the page describes a facility that \
is wastewater-domain but in a different category (e.g. a composting \
facility on a page targeting anaerobic digesters), do not extract.

4. NAMED FACILITY. Generic industry commentary, regulatory rule text, or \
research papers without a named facility yield ZERO candidates. Extract \
only when a specific operator or facility is named.

PAGE-TYPE RULES — apply before extracting:

- Academic / research / federal-program pages (.edu domains, USDA, \
EPA-research): extract only if the page hosts an explicit facility \
directory or names specific facilities. Research papers about waste \
management without naming facilities yield zero candidates.

- News articles: extract specific named facilities only. General industry \
commentary, political coverage, or opinion pieces without named \
facilities yield zero candidates.

- Operator websites: extract the operator itself as a facility if the \
operator handles wastewater for the queried category.

- State / county / municipal agency pages: extract the operators they \
list as candidates, NOT the agency itself. The agency is the source of \
information; the operators are the facilities.

- Directory / Yellow Pages / BBB listings: extract each named operator \
as a separate candidate.

REQUIRED + PREFERRED FIELDS per candidate:

- name (REQUIRED): the operator or facility name, as named on the page.
- state (REQUIRED): MUST be "{state}". Out-of-state candidates filter out.
- city (preferred): city of operation.
- address (preferred): street address if visible.
- facility_type (REQUIRED): one of {allowed_types}.
- phone (optional)
- website (optional): if a homepage URL is visible.
- operator_published_acceptance (optional): any LITERAL text from the \
page about which waste types the facility accepts (septage / grease / \
porta / biosolids / etc.). Phase 4 enrichment will use this in a future \
monthly refresh; capture it here if visible but do not infer.

EVIDENCE QUOTATION RULE — same discipline as Phase 4 v1.1.1:

Every candidate's `evidence_quotation` field MUST be a literal substring \
of the page text shown to you. NOT paraphrased. NOT reconstructed. NOT \
synthesized across multiple snippets. If you cannot quote a substring \
that establishes the facility exists and matches all four criteria, you \
MUST omit that candidate.

CONFIDENCE LEVELS:

- high: page explicitly names the facility, its state location is \
unambiguous (address visible OR page is state-jurisdictional), and the \
facility_type match is direct from quoted text.
- medium: facility named and state-plausible, but address missing or \
facility_type inferred from operator-name context rather than direct text.
- low: facility named but evidence is thin (e.g. operator name appears in \
a directory list with no operational detail). Capture and let downstream \
review adjudicate.

PAGE CLASSIFICATION — also classify the overall page in the \
page_classification field:

- relevant: page produced at least one candidate.
- unrelated: page is clearly off-topic for the (category, state) target \
— freight company, out-of-state, academic without facility names, \
generic news / opinion.
- uncertain: page is in the topic area but does not yield specific \
named facilities meeting all four criteria.

PAGE SUMMARY — a 1-2 sentence summary in page_summary describing what \
the page is. This is metadata for human reviewers; not load-bearing on \
candidates.

If zero candidates qualify, output an empty facilities array and \
classify the page as 'unrelated' or 'uncertain'."""


# Tool schema for structured output. Anthropic Haiku 4.5 will emit a tool
# call matching this schema. The facility_type enum is constrained per
# category via the system prompt; we don't enforce it at the schema level
# because the allowed set varies by call.
TOOL_SCHEMA: dict = {
    "name": "record_discovery_extraction",
    "description": (
        "Record extracted facility candidates from a discovered URL. "
        "Returns a page-level classification plus zero or more facility "
        "candidate records that meet all four criteria in the system "
        "prompt. Empty facilities array is valid for unrelated / "
        "uncertain pages."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "page_classification": {
                "type": "string",
                "enum": ["relevant", "unrelated", "uncertain"],
            },
            "page_summary": {"type": "string"},
            "facilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "state": {"type": "string"},
                        "city": {"type": "string"},
                        "address": {"type": "string"},
                        "facility_type": {"type": "string"},
                        "phone": {"type": "string"},
                        "website": {"type": "string"},
                        "operator_published_acceptance": {"type": "string"},
                        "classification_confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "evidence_quotation": {"type": "string"},
                    },
                    "required": [
                        "name",
                        "state",
                        "facility_type",
                        "classification_confidence",
                        "evidence_quotation",
                    ],
                },
            },
        },
        "required": ["page_classification", "page_summary", "facilities"],
    },
}


def render_system_prompt(*, category: str, state: str, state_abbr: str) -> str:
    """Resolve the {category}, {state}, {state_abbr}, {allowed_types}
    placeholders in the system-prompt template for one extraction call."""
    allowed = CATEGORY_TO_ALLOWED_TYPES.get(category, CANONICAL_FACILITY_TYPES)
    return SYSTEM_PROMPT_TEMPLATE.format(
        category=category,
        state=state,
        state_abbr=state_abbr,
        allowed_types=allowed,
    )


def build_user_message(*, url: str, page_text: str) -> str:
    """Build the per-page user message: the URL for context + the page \
    text (already truncated by the caller). The page text is what the \
    evidence quotations must be substrings of."""
    return (
        f"PAGE URL: {url}\n\n"
        f"PAGE TEXT (already truncated to fit context):\n"
        f"---BEGIN PAGE TEXT---\n"
        f"{page_text}\n"
        f"---END PAGE TEXT---\n\n"
        f"Call the `record_discovery_extraction` tool with your "
        f"page_classification, page_summary, and zero-or-more facility "
        f"candidates. Every evidence_quotation MUST be a literal "
        f"substring of the page text above."
    )


def prompt_hash() -> str:
    """sha256 of (PROMPT_VERSION + SYSTEM_PROMPT_TEMPLATE + tool schema +
    canonical category-to-allowed-types map). Acts as the prompt_hash
    component of the llm_enrichment_cache key. Bumping any of these
    invalidates the discovery extraction cache."""
    import json as _json

    payload = (
        f"{PROMPT_VERSION}\n"
        f"{SYSTEM_PROMPT_TEMPLATE}\n"
        + _json.dumps(TOOL_SCHEMA, sort_keys=True)
        + "\n"
        + _json.dumps(CATEGORY_TO_ALLOWED_TYPES, sort_keys=True)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
