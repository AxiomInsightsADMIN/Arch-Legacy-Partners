"""llm_enrichment_cache wrapper.

Cache key: (content_hash, prompt_hash). On hit we return the cached
response_json + token counts. On miss the caller does the Haiku call and
writes back via `store()`.

`content_hash` is computed from (search_results_canonical_json,
facility_id). Bumping the search results (e.g., fresh Brave query)
invalidates the cache for that facility. `prompt_hash` is from
enrichment._prompt.prompt_hash() — bumping the prompt template
invalidates the entire cache without manual cleanup.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(*, facility_id: str, search_results: list[dict]) -> str:
    """sha256 of (facility_id + canonical_json(search_results))."""
    payload = json.dumps(search_results, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{facility_id}\n{payload}".encode()).hexdigest()


def lookup(cur, *, content_hash: str, prompt_hash: str) -> dict | None:
    """Return cached response_json + token counts as a dict, or None on miss."""
    cur.execute(
        """
        SELECT response_json, model_id, input_tokens, output_tokens, created_at
          FROM llm_enrichment_cache
         WHERE content_hash = %s AND prompt_hash = %s
         LIMIT 1
        """,
        (content_hash, prompt_hash),
    )
    row = cur.fetchone()
    if not row:
        return None
    response_json, model_id, in_tok, out_tok, created_at = row
    return {
        "response_json": response_json,
        "model_id": model_id,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "created_at": created_at,
    }


def store(
    cur,
    *,
    content_hash: str,
    prompt_hash: str,
    response_json: dict[str, Any],
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Upsert a cache row. ON CONFLICT (content_hash, prompt_hash) is a no-op
    because the cache content is content-addressed — same input = same output."""
    cur.execute(
        """
        INSERT INTO llm_enrichment_cache
            (content_hash, prompt_hash, response_json,
             model_id, input_tokens, output_tokens)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (content_hash, prompt_hash) DO NOTHING
        """,
        (
            content_hash,
            prompt_hash,
            json.dumps(response_json),
            model_id,
            input_tokens,
            output_tokens,
        ),
    )
