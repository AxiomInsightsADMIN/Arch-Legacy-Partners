"""Anthropic Haiku 4.5 client wrapper for Phase 4 acceptance-flag extraction.

Returns a structured `HaikuResult` carrying the three acceptance verdicts,
the raw tool-call payload (for cache storage), and token usage (for cost
tracking against the $40 Phase 4 cap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic

from enrichment._prompt import SYSTEM_PROMPT, TOOL_SCHEMA, build_user_message

MODEL_ID = "claude-haiku-4-5-20251001"
# v1.1.1: bumped from 500 → 1000 after v1.1.0 surfaced 5 facilities where
# the longer prompt led Haiku to verbose evidence strings that hit the cap
# mid-tool-call, producing malformed schema output (XML-style
# `<parameter name="value">` artifacts leaking into JSON values). 1000 is
# generous; typical successful output is 200–350 tokens.
MAX_TOKENS = 1000


@dataclass
class FieldVerdict:
    value: str  # 'Yes' | 'No' | 'Unknown'
    confidence: float  # 0.0-1.0
    evidence: str  # supporting text snippet


@dataclass
class HaikuResult:
    """Per-facility output from one Haiku call."""

    accepts_septage: FieldVerdict
    accepts_grease_trap: FieldVerdict
    accepts_portable_toilet: FieldVerdict
    raw_response: dict  # the full tool_use input dict — cached
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def _to_verdict(d: Any) -> FieldVerdict:
    """Coerce a dict from Haiku's tool input into FieldVerdict, defaulting
    to a conservative Unknown if anything is missing or malformed."""
    if not isinstance(d, dict):
        return FieldVerdict("Unknown", 0.0, "malformed tool output")
    value = d.get("value")
    if value not in {"Yes", "No", "Unknown"}:
        return FieldVerdict("Unknown", 0.0, f"invalid value: {value!r}")
    conf = d.get("confidence")
    try:
        conf = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    evidence = (d.get("evidence") or "").strip()
    return FieldVerdict(value, conf, evidence)


def extract(*, facility: dict, search_results: list[dict]) -> HaikuResult:
    """Run one Haiku call. Returns a HaikuResult with the three verdicts.
    On Anthropic API failure, returns HaikuResult with error set and all
    verdicts defaulted to Unknown / 0.0 — never raises."""
    user_msg = build_user_message(facility=facility, search_results=search_results)
    notes: list[str] = []
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": TOOL_SCHEMA["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return HaikuResult(
            accepts_septage=FieldVerdict("Unknown", 0.0, ""),
            accepts_grease_trap=FieldVerdict("Unknown", 0.0, ""),
            accepts_portable_toilet=FieldVerdict("Unknown", 0.0, ""),
            raw_response={},
            error=f"{type(e).__name__}: {e}",
        )

    # Find the tool_use content block
    tool_call = None
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_SCHEMA["name"]:
            tool_call = block
            break

    if tool_call is None:
        notes.append("no_tool_use_block_in_response")
        return HaikuResult(
            accepts_septage=FieldVerdict("Unknown", 0.0, "no tool call returned"),
            accepts_grease_trap=FieldVerdict("Unknown", 0.0, "no tool call returned"),
            accepts_portable_toilet=FieldVerdict("Unknown", 0.0, "no tool call returned"),
            raw_response={},
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            stop_reason=getattr(resp, "stop_reason", None),
            notes=notes,
        )

    tool_input = tool_call.input or {}
    return HaikuResult(
        accepts_septage=_to_verdict(tool_input.get("accepts_septage")),
        accepts_grease_trap=_to_verdict(tool_input.get("accepts_grease_trap")),
        accepts_portable_toilet=_to_verdict(tool_input.get("accepts_portable_toilet")),
        raw_response=tool_input,
        input_tokens=getattr(resp.usage, "input_tokens", 0),
        output_tokens=getattr(resp.usage, "output_tokens", 0),
        stop_reason=getattr(resp, "stop_reason", None),
        notes=notes,
    )
