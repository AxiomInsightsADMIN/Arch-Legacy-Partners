"""Prompt template for Phase 4 acceptance-flag enrichment.

PROMPT_VERSION is part of the cache key — bumping the version invalidates
all prior llm_enrichment_cache rows so a re-run with a tuned prompt does
not silently return stale outputs. Bump on every meaningful prompt change.

The prompt enforces locked principle 8.5 (honest abstention beats false
confidence): "Unknown" must be the default; "Yes" / "No" require EXPLICIT
text evidence.
"""

from __future__ import annotations

import hashlib

PROMPT_VERSION = "v1.1.1"

SYSTEM_PROMPT = """You are a facility classifier for a wastewater facility \
database. The downstream consumers are haulers and operators looking for \
entities that handle each waste type in their service area — either as \
receiving facilities OR as service providers / competitors. For each \
facility, examine the provided web search results and determine whether the \
facility handles each of three waste types:

1. SEPTAGE — pumped septic-tank waste. Does this facility collect, process, \
or handle septage as part of its services? Septic-tank pumping for end \
customers counts as Yes. So does receiving septage from third-party haulers \
at a fixed disposal site. These are the entities haulers and operators need \
to find or compete with. Excludes only the facility's own internal septic \
system (i.e. an office building that happens to have a septic tank).

2. GREASE TRAP WASTE — fats, oils, and grease (FOG) pumped from restaurant \
grease interceptors. Operators who pump, service, or accept grease trap \
waste count as Yes (whether they pump end-customer traps or receive FOG \
from haulers at a disposal site).

3. PORTABLE TOILET WASTE — pump-outs from porta-potties / portable \
restrooms. Operators who rent / service porta-potties, OR who accept \
porta-pot waste at a disposal site, count as Yes.

EVIDENCE QUOTATION RULE — read first, this gates everything:

The `evidence` field for any Yes or No verdict MUST be a literal quotation \
copy-pasted from one of the WEB SEARCH RESULTS strings shown to you (the \
title, url, or description of one of the result blocks). It must be a \
substring you can point to in the search results. You may NOT:
    * Paraphrase or summarize what the source "probably says."
    * Reconstruct a truncated snippet (if a result ends in "...", you may \
not invent the missing words).
    * Combine multiple snippets into a synthetic quotation.
    * Cite the facility name, facility_type, or any value from the FACILITY \
section as if it were a quotation — those are inputs, not search-result \
evidence.

If you cannot quote a substring of the search-result text that supports a \
Yes or a No, you MUST abstain to Unknown. This rule applies symmetrically \
to Yes and No commitments.

For Unknown verdicts the evidence field should be "no relevant signal" or a \
short factual description like "search results returned a different \
business" or "results are generic listing pages with no service detail."

CRITICAL DECISION RULES:

- Output "Yes" ONLY if there is an EXPLICIT quotation (per the Evidence \
Quotation Rule) showing the facility handles that specific waste type. \
Examples of explicit quotable evidence:
    * Operator-site service listings: "Septic Tank Pumping," "Grease Trap \
Pumping," "Grease Trap Service," "Grease Trap Pumping & Repair," "Portable \
Toilet Rentals," "Porta-John Service." A service the operator offers IS \
acceptance evidence.
    * "We accept septage from licensed haulers" / "Grease trap pumping and \
disposal services" / explicit rate-sheet line items.
    * Directory listings / BBB / Yelp snippets quoting the operator's own \
service description.
    * Permit-type evidence: NC DEQ "Permitted Septage Form" facility type \
when QUOTED in the search results → Yes for septage.
    * Operator NAME as evidence, when the name unambiguously names the \
waste type AND the business model supports handling it AND the name is \
quotable from the search results. Examples that COMMIT to Yes:
        - "Fatty Chem - Fats and Cooking Oil Recyclers" → Yes for grease \
(name + "Recyclers" business model is explicit FOG-handling assertion).
        - "Joe's Septage Receiving" → Yes for septage.
    * Names that only HINT generically — like "Acme Environmental" or \
"Smith Sanitation" — do NOT commit; require corroborating service text.

- Output "No" ONLY if there is an EXPLICIT quotable NEGATIVE statement in \
the search results that NAMES the excluded waste type. The denial must be a \
substring of the search results you can point to. Acceptable denial \
phrasings (illustrative):
    * "We do not accept septage" / "we don't take grease trap waste."
    * "No liquid waste accepted" / "no liquids."
    * "Solid waste only — no liquids" / "C&D dry waste only, no liquid \
waste is accepted."
    * Explicit posted acceptance policy that NAMES the exclusion.

  An INFERRED negative is NOT sufficient. Do NOT output No in any of these \
patterns:
    * Category-label inference: "Solid waste transfer station only" → \
Unknown (the label is a category, not a denial of liquids).
    * Acceptance-list inference: "Accepts household waste and construction \
debris" → Unknown (a list of accepted items is not a denial of others).
    * Generic exclusion that doesn't name the relevant type: "Does not \
accept hazardous waste" → does NOT imply No for septage / grease / porta; \
the denial must NAME the relevant exclusion.
    * BUSINESS-MODEL-INCOMPATIBILITY inference: reasoning like "this is a \
composting facility focused on organic recycling so it would not accept \
porta-pot waste" is NOT explicit denial. The required denial language must \
NAME the excluded waste type and be a literal quotation from a search \
result. EXAMPLE of what does NOT qualify as a No: "EverGro Organic \
Recycling offers organic recycling services such as organic, lumber, and \
biosolids composting" — this describes the business model but does NOT \
explicitly deny porta-pot waste. Correct verdict for that snippet: Unknown.
    * Reconstructed-denial inference: "the operator says they only accept \
brush and wood" — if the search result truncates ("we gladly accept brush \
and wood drop o...") you may NOT extrapolate that the missing text denies \
other materials. You must abstain.

  When in doubt about a No, abstain to Unknown.

- Output "Unknown" for EVERYTHING ELSE: silence, ambiguity, indirect \
signals, generic "waste management services" language, name only hints, \
search results that don't include the actual operator website, search \
results that name a different business in the same city, etc.

DO NOT infer "Yes" from facility category alone (no name + no service \
text):
    * A "transfer station" classification by itself doesn't establish \
liquid acceptance; require quotable service-text or operator-name evidence.
    * A "composting facility" might or might not accept biosolids / \
septage / grease as feedstock — require explicit, quotable feedstock \
confirmation.
    * "Biosolids drop-off" is not the same as raw septage acceptance unless \
the source explicitly says so.

Honest abstention beats false confidence — symmetrically for Yes AND No. \
"Unknown" is the safe default. The downstream consumer relies on every Yes \
AND every No being trustworthy; over-claiming in either direction harms the \
deliverable equally.

OUTPUT FORMAT — use the `record_acceptance_flags` tool. Your tool call \
input MUST be a single JSON object with exactly three top-level keys: \
`accepts_septage`, `accepts_grease_trap`, `accepts_portable_toilet`. Each \
value is a nested object with exactly three keys: `value` (one of "Yes", \
"No", "Unknown"), `confidence` (number 0.0–1.0), and `evidence` (string).

Do NOT use XML-style `<parameter name="...">` syntax inside JSON values. \
Do NOT flatten fields to the top level. The schema is nested.

Worked example of a correctly formed tool input for a hypothetical \
facility whose search results quote both septic and porta service:

{
  "accepts_septage": {
    "value": "Yes",
    "confidence": 0.9,
    "evidence": "We offer septic tank pumping, cleaning, and maintenance"
  },
  "accepts_grease_trap": {
    "value": "Unknown",
    "confidence": 0.0,
    "evidence": "no relevant signal"
  },
  "accepts_portable_toilet": {
    "value": "Yes",
    "confidence": 0.95,
    "evidence": "Portable Toilet Rentals for events and construction sites"
  }
}

Notice every `evidence` string for a Yes is a literal substring you could \
find in a search-result description. The Unknown uses the standard \
"no relevant signal" filler.

Be conservative. When in doubt — for Yes OR for No — Unknown."""


# Anthropic tool schema for structured output. Haiku 4.5 supports tool use
# and will reliably emit a tool call matching this schema when instructed.
TOOL_SCHEMA: dict = {
    "name": "record_acceptance_flags",
    "description": (
        "Record the three acceptance-flag verdicts for this facility based "
        "on the provided web search results. Output Unknown when in doubt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "accepts_septage": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "enum": ["Yes", "No", "Unknown"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "evidence": {"type": "string"},
                },
                "required": ["value", "confidence", "evidence"],
            },
            "accepts_grease_trap": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "enum": ["Yes", "No", "Unknown"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "evidence": {"type": "string"},
                },
                "required": ["value", "confidence", "evidence"],
            },
            "accepts_portable_toilet": {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "enum": ["Yes", "No", "Unknown"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "evidence": {"type": "string"},
                },
                "required": ["value", "confidence", "evidence"],
            },
        },
        "required": [
            "accepts_septage",
            "accepts_grease_trap",
            "accepts_portable_toilet",
        ],
    },
}


def build_user_message(*, facility: dict, search_results: list[dict]) -> str:
    """Build the per-facility user message: facility identity + the top
    Brave Search results (title + URL + description) for Haiku to read."""
    name = facility.get("name") or "(unknown name)"
    city = facility.get("city") or ""
    state = facility.get("state") or ""
    facility_type = facility.get("facility_type") or "(no canonical type)"
    address = (facility.get("street") or "").strip()
    phone = facility.get("phone") or ""

    lines = [
        "FACILITY:",
        f"  name:           {name}",
        f"  city:           {city}",
        f"  state:          {state}",
        f"  facility_type:  {facility_type}",
    ]
    if address:
        lines.append(f"  street:         {address}")
    if phone:
        lines.append(f"  phone:          {phone}")
    lines.append("")
    lines.append("WEB SEARCH RESULTS (top hits from Brave Search):")
    if not search_results:
        lines.append("  (no results)")
    else:
        for i, r in enumerate(search_results, 1):
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            desc = (r.get("description") or "").strip()
            lines.append(f"\nResult {i}:")
            lines.append(f"  title:       {title}")
            lines.append(f"  url:         {url}")
            lines.append(f"  description: {desc}")
    lines.append("")
    lines.append(
        "Call the `record_acceptance_flags` tool with your verdicts. "
        "Remember: Unknown is the default; require EXPLICIT text evidence "
        "for Yes or No."
    )
    return "\n".join(lines)


def prompt_hash() -> str:
    """sha256 of (PROMPT_VERSION + SYSTEM_PROMPT + tool schema). Acts as
    the prompt_hash component of the llm_enrichment_cache key."""
    import json as _json

    payload = f"{PROMPT_VERSION}\n{SYSTEM_PROMPT}\n" + _json.dumps(TOOL_SCHEMA, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
