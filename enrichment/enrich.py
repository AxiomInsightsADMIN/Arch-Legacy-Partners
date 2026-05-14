"""Per-facility enrichment driver.

Public:
  - `enrich_facility(facility, *, conn)` -> EnrichedFacility — runs the
    Brave + Haiku pipeline for one facility, caching by content_hash.
  - main CLI:
      python -m enrichment.enrich --input <facilities.json> --output <results.json>
      python -m enrichment.enrich --canonical-id <UUID>         (one-off)

Calibration usage: feed `local/_calibration_facilities.json` as --input.
Full-pass usage: a separate driver (TBD post-calibration) queries
`canonical_facility` directly for facility_type IS NOT NULL rows.

Cost tracking: aggregates input/output tokens across all calls and
prints running cost at run end. Phase 4 cap is $40 total per kickoff
brief; calibration is expected to be well under that.

Idempotency: cached calls do NOT contact Anthropic or Brave. Re-runs
of the same input file with the same prompt version are free.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from enrichment import _brave, _cache, _haiku  # noqa: E402
from enrichment._prompt import PROMPT_VERSION, prompt_hash  # noqa: E402
from scrapers._loader_utils import db_connect  # noqa: E402

# Haiku 4.5 pricing per Anthropic's pricing page (verify before scaling).
# Conservative public-pricing estimates for Phase 4 cost tracking:
INPUT_USD_PER_MTOK = 0.80
OUTPUT_USD_PER_MTOK = 4.00


@dataclass
class EnrichedFacility:
    facility_id: str
    facility_name: str
    bucket: str | None
    # Verdicts
    accepts_septage_value: str
    accepts_septage_confidence: float
    accepts_septage_evidence: str
    accepts_grease_trap_value: str
    accepts_grease_trap_confidence: float
    accepts_grease_trap_evidence: str
    accepts_portable_toilet_value: str
    accepts_portable_toilet_confidence: float
    accepts_portable_toilet_evidence: str
    # Pipeline metadata
    brave_query: str
    brave_result_count: int
    brave_results: list[dict]
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str | None = None


def _haiku_to_record(
    facility: dict,
    haiku_result,
    brave_query: str,
    search_results: list[dict],
    cache_hit: bool,
    cost_usd: float,
) -> EnrichedFacility:
    """Project a HaikuResult + Brave context into the flat EnrichedFacility shape."""
    s = haiku_result.accepts_septage
    g = haiku_result.accepts_grease_trap
    p = haiku_result.accepts_portable_toilet
    return EnrichedFacility(
        facility_id=facility.get("id", ""),
        facility_name=facility.get("name", ""),
        bucket=facility.get("source_bucket"),
        accepts_septage_value=s.value,
        accepts_septage_confidence=s.confidence,
        accepts_septage_evidence=s.evidence,
        accepts_grease_trap_value=g.value,
        accepts_grease_trap_confidence=g.confidence,
        accepts_grease_trap_evidence=g.evidence,
        accepts_portable_toilet_value=p.value,
        accepts_portable_toilet_confidence=p.confidence,
        accepts_portable_toilet_evidence=p.evidence,
        brave_query=brave_query,
        brave_result_count=len(search_results),
        brave_results=search_results,
        cache_hit=cache_hit,
        input_tokens=haiku_result.input_tokens,
        output_tokens=haiku_result.output_tokens,
        cost_usd=cost_usd,
        error=haiku_result.error,
    )


def _promote_to_canonical(
    conn,
    *,
    facility_id: str,
    haiku: _haiku.HaikuResult,
) -> None:
    """UPDATE canonical_facility.accepts_* from a HaikuResult.

    Called on BOTH paths in enrich_facility:
      - fresh Haiku call: right after _cache.store() succeeds
      - cache hit: right after reconstructing the HaikuResult from
        the cached response_json

    Why both paths: the canonical row should converge to the cached
    verdict regardless of how it got into the cache. This makes
    re-running idempotent — the cache stays untouched if already
    populated (the cache-hit path skips Haiku), and the canonical
    row picks up the same verdict either way.

    No-op when haiku.error is set or facility_id is empty — we don't
    have a trustworthy verdict to write.

    Per-row commit (matches the per-row cache-write pattern from the
    Phase 4 resilience events). Worst-case loss on a kill is one
    in-flight canonical update.
    """
    if haiku.error is not None or not facility_id:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE canonical_facility
               SET accepts_septage         = %s,
                   accepts_grease_trap     = %s,
                   accepts_portable_toilet = %s,
                   last_seen_at            = NOW()
             WHERE id = %s
            """,
            (
                haiku.accepts_septage.value,
                haiku.accepts_grease_trap.value,
                haiku.accepts_portable_toilet.value,
                facility_id,
            ),
        )
        conn.commit()
    finally:
        cur.close()


def enrich_facility(facility: dict, *, conn) -> EnrichedFacility:
    """Brave Search + Haiku extract for one facility with cache lookup.

    Side effect: on a successful verdict (either fresh from Haiku or
    reconstructed from cache), UPDATEs canonical_facility.accepts_*
    via `_promote_to_canonical`. This closes the gap where Phase 4 stop
    4 had populated llm_enrichment_cache but never landed verdicts on
    the canonical rows, leaving v_all_in_scope showing NULLs.
    """
    fid = facility.get("id", "")
    brave_query = _brave.build_query(facility)

    # 1) Brave Search
    try:
        search_results = _brave.search(brave_query, count=3)
    except Exception as e:
        haiku = _haiku.HaikuResult(
            accepts_septage=_haiku.FieldVerdict("Unknown", 0.0, ""),
            accepts_grease_trap=_haiku.FieldVerdict("Unknown", 0.0, ""),
            accepts_portable_toilet=_haiku.FieldVerdict("Unknown", 0.0, ""),
            raw_response={},
            error=f"brave_search_failed: {type(e).__name__}: {e}",
        )
        return _haiku_to_record(facility, haiku, brave_query, [], False, 0.0)

    # 2) Cache lookup
    chash = _cache.content_hash(facility_id=fid, search_results=search_results)
    phash = prompt_hash()

    cache_cur = conn.cursor()
    try:
        cached = _cache.lookup(cache_cur, content_hash=chash, prompt_hash=phash)
    finally:
        cache_cur.close()

    if cached is not None:
        # Reconstruct HaikuResult from cached response_json
        raw = cached["response_json"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        haiku = _haiku.HaikuResult(
            accepts_septage=_haiku._to_verdict(raw.get("accepts_septage")),
            accepts_grease_trap=_haiku._to_verdict(raw.get("accepts_grease_trap")),
            accepts_portable_toilet=_haiku._to_verdict(raw.get("accepts_portable_toilet")),
            raw_response=raw,
            input_tokens=cached["input_tokens"] or 0,
            output_tokens=cached["output_tokens"] or 0,
            stop_reason="cache",
        )
        # Promote the cached verdict to the canonical row even on cache
        # hits — this is what makes re-runs converge canonical_facility
        # to the cached state regardless of which run populated the cache.
        _promote_to_canonical(conn, facility_id=fid, haiku=haiku)
        cost = (
            haiku.input_tokens * INPUT_USD_PER_MTOK / 1_000_000
            + haiku.output_tokens * OUTPUT_USD_PER_MTOK / 1_000_000
        )
        return _haiku_to_record(facility, haiku, brave_query, search_results, True, cost)

    # 3) Cache miss — call Haiku, cache the result
    haiku = _haiku.extract(facility=facility, search_results=search_results)
    if haiku.error is None:
        write_cur = conn.cursor()
        try:
            _cache.store(
                write_cur,
                content_hash=chash,
                prompt_hash=phash,
                response_json=haiku.raw_response,
                model_id=_haiku.MODEL_ID,
                input_tokens=haiku.input_tokens,
                output_tokens=haiku.output_tokens,
            )
            conn.commit()
        finally:
            write_cur.close()
        # Promote the fresh verdict to the canonical row. Done AFTER the
        # cache write so a kill between cache-store and promote leaves the
        # cache populated; the next run cache-hits and promotes.
        _promote_to_canonical(conn, facility_id=fid, haiku=haiku)

    cost = (
        haiku.input_tokens * INPUT_USD_PER_MTOK / 1_000_000
        + haiku.output_tokens * OUTPUT_USD_PER_MTOK / 1_000_000
    )
    return _haiku_to_record(facility, haiku, brave_query, search_results, False, cost)


def run_batch(facilities: list[dict], *, polite_delay_s: float = 0.4) -> list[EnrichedFacility]:
    """Walk a list of facilities, enrich each. polite_delay_s sleeps
    between Brave calls so we stay well below Brave's free-tier 1/sec."""
    conn = db_connect()
    out: list[EnrichedFacility] = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    cache_hits = 0
    started = time.time()
    try:
        for i, fac in enumerate(facilities, 1):
            r = enrich_facility(fac, conn=conn)
            out.append(r)
            total_in += r.input_tokens
            total_out += r.output_tokens
            total_cost += r.cost_usd
            if r.cache_hit:
                cache_hits += 1
            print(
                f"[{i:>3}/{len(facilities)}] {r.facility_name[:35]!r:37}  "
                f"S={r.accepts_septage_value:7s} "
                f"G={r.accepts_grease_trap_value:7s} "
                f"P={r.accepts_portable_toilet_value:7s} "
                f"{'CACHE' if r.cache_hit else '     '}  "
                f"in={r.input_tokens:>4} out={r.output_tokens:>3}",
                flush=True,
            )
            if r.error:
                print(f"      error: {r.error}", flush=True)
            if not r.cache_hit:
                time.sleep(polite_delay_s)
    finally:
        conn.close()
    elapsed = time.time() - started
    print()
    print(f"[batch] {len(facilities)} facilities in {elapsed:.1f}s")
    print(f"  cache hits:    {cache_hits}/{len(facilities)}")
    print(f"  input tokens:  {total_in:,}")
    print(f"  output tokens: {total_out:,}")
    print(f"  cost USD:      ${total_cost:.4f}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON list of facilities (see local/_calibration_facilities.json)",
    )
    ap.add_argument(
        "--output", type=Path, required=True, help="Output JSON path for the EnrichedFacility list"
    )
    args = ap.parse_args()

    facilities = json.loads(args.input.read_text(encoding="utf-8"))
    print(f"[enrich] prompt_version={PROMPT_VERSION}  prompt_hash={prompt_hash()[:12]}…")
    print(f"[enrich] enriching {len(facilities)} facilities")

    results = run_batch(facilities)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([asdict(r) for r in results], indent=2),
        encoding="utf-8",
    )
    print(f"[enrich] wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
