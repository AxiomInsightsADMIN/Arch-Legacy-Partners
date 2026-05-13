"""Brave Search API client for Phase 4 enrichment.

Single function: `search(query, count=3) -> list[dict]`. Returns the
title / url / description triples from the Brave web-search response.
No retries beyond what the caller wants; transport / rate-limit errors
propagate so the calibration driver can decide whether to back off.

Free tier: 2,000 queries/month at ~1 query/sec. Calibration uses 50
queries; full pass uses ~1,970. Well within free tier.
"""

from __future__ import annotations

import os

import requests

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "Axiom-Insights-ArchLegacy/0.1 (Phase 4 enrichment)"


def search(query: str, *, count: int = 3, timeout: int = 10) -> list[dict]:
    """Call Brave web-search. Returns up to `count` result dicts each with
    'title', 'url', 'description' keys (whatever Brave provides). On HTTP
    failure raises requests.HTTPError; on transport failure raises
    requests.RequestException."""
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("BRAVE_API_KEY not set; required for enrichment.brave.search")
    r = requests.get(
        BRAVE_ENDPOINT,
        params={"q": query, "count": count},
        headers={
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    web = body.get("web") or {}
    results = web.get("results") or []
    out = []
    for item in results[:count]:
        out.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "description": item.get("description") or "",
            }
        )
    return out


def build_query(facility: dict) -> str:
    """Construct a focused query for one facility. Prioritizes name +
    city + acceptance keywords. The exact phrasing affects Brave's
    ranking — we surface name and location, then add the three
    acceptance-flag keywords as OR-style hints so any matching content
    surfaces in the top-3."""
    name = (facility.get("name") or "").strip()
    city = (facility.get("city") or "").strip()
    state = (facility.get("state") or "").strip()
    parts = [name]
    if city:
        parts.append(city)
    if state:
        parts.append(state)
    # Acceptance-keyword hint helps Brave surface service-description
    # pages over generic directory listings. Single phrase, not OR'd,
    # so we don't blow out the query into nonsense.
    parts.append("accepts septage grease trap portable toilet")
    return " ".join(parts).strip()
