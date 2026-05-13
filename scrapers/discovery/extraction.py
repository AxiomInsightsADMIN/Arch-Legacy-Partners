"""Phase 4.5 step C — discovery URL extraction pipeline.

For every discovered_url row with fetch_status='pending':
  1. Fetch via requests (30s timeout); fall back to Playwright if the
     page returns empty / clearly-JS-rendered content.
  2. Parse: BeautifulSoup for HTML, pdfplumber for PDFs. Detect by
     Content-Type or URL extension.
  3. Truncate the extracted text to MAX_CONTENT_CHARS (~10K tokens).
  4. Compute content_hash = sha256(truncated_text).
  5. Look up (content_hash, discovery_prompt_hash) in llm_enrichment_cache.
     - HIT: re-use the cached Haiku output (free).
     - MISS: call Haiku 4.5 with the discovery extraction prompt,
       cache the response.
  6. UPDATE discovered_url SET fetch_status='fetched',
     classified_relevance=..., content_hash=..., fetched_at=NOW().
  7. INSERT each extracted facility candidate into
     discovery_candidate_facility with the full Haiku JSON as
     raw_payload.

Operational parameters (from Ryan's step C brief):
  - Hard Haiku cost cap at $10. On breach, exit cleanly and report.
  - Heartbeat every 50 URLs (plus first URL and any error).
  - Checkpoint to local/_discovery_extraction_progress.json every 100 URLs.
  - 1.0s polite delay between page fetches (cache hits skip delay).
  - Per-row commit so a mid-run kill loses at most one in-flight URL.
  - On fetch failure (timeout, 4xx, 5xx, transport error): set
    fetch_status='failed', no Haiku call.
  - On Haiku error: set fetch_status='failed', log, continue.

Idempotency: re-running over the same discovered_url set is safe; rows
with fetch_status='fetched' or 'failed' are skipped. The Supabase cache
gives free replays on (content_hash, prompt_hash) hits.

Run from project root (.venv activated):
  .venv/Scripts/python.exe -m scrapers.discovery.extraction
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from bs4 import BeautifulSoup

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import anthropic  # noqa: E402

from enrichment import _cache as cache_helper  # noqa: E402  (reuse cache wrapper)
from scrapers._loader_utils import db_connect  # noqa: E402
from scrapers.discovery._extraction_prompt import (  # noqa: E402
    PROMPT_VERSION,
    TOOL_SCHEMA,
    build_user_message,
    prompt_hash,
    render_system_prompt,
)

# Constants ---------------------------------------------------------------
HAIKU_BUDGET_USD = 10.0
HEARTBEAT_EVERY = 50
CHECKPOINT_EVERY = 100
POLITE_DELAY_S = 1.0
FETCH_TIMEOUT = 30
MAX_CONTENT_CHARS = 40_000  # ~10K tokens for Haiku
MAX_OUTPUT_TOKENS = 1500

# Haiku 4.5 pricing for cost tracking (verify against Anthropic pricing page).
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"
INPUT_USD_PER_MTOK = 0.80
OUTPUT_USD_PER_MTOK = 4.00

USER_AGENT = (
    "Mozilla/5.0 (compatible; Axiom-Insights-ArchLegacy/0.1; "
    "Phase 4.5 discovery; contact: arch-legacy@axiominsights.example)"
)

# Pattern to identify "this page needs JavaScript" markers in raw HTML.
JS_REQUIRED_MARKERS = re.compile(
    r"(noscript|please enable javascript|please enable js|"
    r'<div id="(root|app|__next)"|window\.__INITIAL_STATE__|'
    r"this app requires javascript)",
    re.IGNORECASE,
)

PROGRESS_PATH = _PROJECT_ROOT / "local" / "_discovery_extraction_progress.json"


# --------------------------------------------------------------------------
@dataclass
class FetchResult:
    text: str | None
    content_type: str | None
    error: str | None
    used_playwright: bool = False


@dataclass
class ExtractionStats:
    urls_processed: int = 0
    urls_fetched_ok: int = 0
    urls_fetch_failed: int = 0
    urls_haiku_failed: int = 0
    urls_via_playwright: int = 0
    urls_cache_hit: int = 0
    candidates_inserted: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def bump_category(self, category: str, key: str, n: int = 1) -> None:
        bucket = self.by_category.setdefault(
            category,
            {
                "urls": 0,
                "fetched": 0,
                "failed": 0,
                "haiku_failed": 0,
                "cache_hit": 0,
                "candidates": 0,
                "page_relevant": 0,
                "page_unrelated": 0,
                "page_uncertain": 0,
            },
        )
        bucket[key] = bucket.get(key, 0) + n


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
def _detect_pdf(url: str, content_type: str | None) -> bool:
    if content_type and "pdf" in content_type.lower():
        return True
    return bool(url.lower().endswith(".pdf"))


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """pdfplumber text extraction. Bounded to first 30 pages."""
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= 30:
                break
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            pages.append(txt)
    return "\n\n".join(pages)


def _extract_html_text(html: str) -> str:
    """BeautifulSoup text extraction. Strips scripts/styles, collapses
    whitespace, returns visible text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines.
    return re.sub(r"\n{3,}", "\n\n", text)


def _looks_js_rendered(raw_html: str, extracted_text: str) -> bool:
    """Heuristic: tiny extracted text + JS-framework markers in raw HTML.
    Returns True if the page probably needs JavaScript to render."""
    if len(extracted_text) >= 400:
        return False
    return bool(JS_REQUIRED_MARKERS.search(raw_html))


def _fetch_via_requests(url: str) -> tuple[bytes | None, str | None, str | None]:
    """Returns (body_bytes, content_type, error_string)."""
    try:
        resp = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", ""), None
    except requests.HTTPError as e:
        return None, None, f"http_{e.response.status_code if e.response else 'err'}"
    except requests.Timeout:
        return None, None, "timeout"
    except requests.RequestException as e:
        return None, None, f"{type(e).__name__}"


def _fetch_via_playwright(url: str, *, browser) -> tuple[str | None, str | None]:
    """Playwright fallback. Uses a passed-in browser instance.
    Returns (text, error_string)."""
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        try:
            page.goto(url, timeout=FETCH_TIMEOUT * 1000, wait_until="domcontentloaded")
            # Let JS settle briefly.
            page.wait_for_timeout(2000)
            text = page.evaluate("document.body ? document.body.innerText : ''")
            return text or "", None
        except Exception as e:
            return None, f"playwright_{type(e).__name__}"
    finally:
        page.close()


def fetch_page(url: str, *, playwright_browser=None) -> FetchResult:
    """Top-level fetcher. requests first; Playwright fallback for JS-rendered."""
    body, ct, err = _fetch_via_requests(url)
    if err is not None:
        return FetchResult(text=None, content_type=ct, error=err)
    assert body is not None

    if _detect_pdf(url, ct):
        try:
            text = _extract_pdf_text(body)
        except Exception as e:
            return FetchResult(text=None, content_type=ct, error=f"pdf_parse_{type(e).__name__}")
        return FetchResult(text=text, content_type=ct or "application/pdf", error=None)

    # HTML path
    try:
        raw_html = body.decode("utf-8", errors="replace")
    except Exception:
        raw_html = body.decode("latin-1", errors="replace")
    text = _extract_html_text(raw_html)

    # Playwright fallback if requests' content looks JS-blocked.
    if _looks_js_rendered(raw_html, text) and playwright_browser is not None:
        pw_text, pw_err = _fetch_via_playwright(url, browser=playwright_browser)
        if pw_err is None and pw_text and len(pw_text) > len(text):
            return FetchResult(
                text=pw_text, content_type=ct or "text/html", error=None, used_playwright=True
            )

    return FetchResult(text=text, content_type=ct or "text/html", error=None)


# --------------------------------------------------------------------------
# Haiku
# --------------------------------------------------------------------------
def _truncate(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    head = MAX_CONTENT_CHARS - 200
    return text[:head] + "\n\n...[TRUNCATED]..."


def _to_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def haiku_extract(
    *,
    content: str,
    url: str,
    category: str,
    state_name: str,
    state_abbr: str,
    client: anthropic.Anthropic,
) -> tuple[dict | None, int, int, str | None]:
    """One Haiku extraction call. Returns (parsed_tool_input, in_tokens,
    out_tokens, error). On any error, returns (None, 0, 0, error_string)."""
    sys_prompt = render_system_prompt(category=category, state=state_name, state_abbr=state_abbr)
    user_msg = build_user_message(url=url, page_text=content)
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL_ID,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=sys_prompt,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": TOOL_SCHEMA["name"]},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return None, 0, 0, f"anthropic_{type(e).__name__}: {e}"

    in_tok = getattr(resp.usage, "input_tokens", 0)
    out_tok = getattr(resp.usage, "output_tokens", 0)

    tool_input = None
    for block in resp.content or []:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_SCHEMA["name"]:
            tool_input = block.input
            break
    if not isinstance(tool_input, dict):
        return None, in_tok, out_tok, "no_tool_use_block"
    return tool_input, in_tok, out_tok, None


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def fetch_pending_urls(conn) -> list[dict]:
    """Pull every discovered_url row where fetch_status='pending'."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, source_category, state, query, url
              FROM discovered_url
             WHERE fetch_status = 'pending'
             ORDER BY source_category, state, id
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
    return [
        {"id": r[0], "source_category": r[1], "state": r[2], "query": r[3], "url": r[4]}
        for r in rows
    ]


def update_discovered_url(
    conn,
    *,
    url_id: int,
    fetch_status: str,
    content_hash: str | None,
    classified_relevance: str | None,
) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE discovered_url
               SET fetch_status = %s,
                   content_hash = %s,
                   classified_relevance = %s,
                   fetched_at = NOW()
             WHERE id = %s
            """,
            (fetch_status, content_hash, classified_relevance, url_id),
        )
        conn.commit()
    finally:
        cur.close()


def insert_candidate(
    conn,
    *,
    url_id: int,
    raw_payload: dict,
    classification_confidence: str,
) -> int:
    """Insert a discovery_candidate_facility row. Returns the new id."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO discovery_candidate_facility
                (discovered_url_id, raw_payload, classification_confidence)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (url_id, json.dumps(raw_payload), classification_confidence),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        cur.close()


def write_checkpoint(stats: ExtractionStats, *, total: int) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "stats": asdict(stats),
                "total_urls_planned": total,
                "remaining": total - stats.urls_processed,
                "checkpoint_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(PROGRESS_PATH)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
STATE_NAME_LOOKUP = {"TX": "Texas", "NC": "North Carolina"}


def main() -> int:
    print(
        f"[extraction] prompt_version={PROMPT_VERSION}  prompt_hash={prompt_hash()[:12]}…",
        flush=True,
    )
    print(f"[extraction] cost cap: ${HAIKU_BUDGET_USD:.2f}", flush=True)

    conn = db_connect()
    urls = fetch_pending_urls(conn)
    total = len(urls)
    print(f"[extraction] {total} URLs with fetch_status='pending' to process", flush=True)

    # Pre-flight breakdown
    from collections import Counter

    pre = Counter((u["source_category"], u["state"]) for u in urls)
    print("[extraction] per-(category, state) URLs to fetch:", flush=True)
    for (cat, st), n in sorted(pre.items()):
        print(f"  {cat:32s} {st:3s}  urls={n}", flush=True)

    client = anthropic.Anthropic()
    phash = prompt_hash()
    stats = ExtractionStats()
    started = time.time()
    hit_cap = False

    # Lazy Playwright initialization — only spin up if a page needs JS fallback.
    pw_ctx = None
    pw_browser = None

    try:
        for i, u in enumerate(urls, 1):
            stats.urls_processed += 1
            stats.bump_category(u["source_category"], "urls")

            # Hard cost cap check
            if stats.cost_usd >= HAIKU_BUDGET_USD:
                print(
                    f"\n[extraction] HARD STOP: cumulative cost "
                    f"${stats.cost_usd:.4f} >= cap ${HAIKU_BUDGET_USD:.2f}",
                    flush=True,
                )
                hit_cap = True
                break

            # 1) Fetch
            fr = fetch_page(u["url"], playwright_browser=pw_browser)

            # If fetch failed with what looks like a JS issue and we haven't
            # spun up Playwright, try to spin it up now and retry once.
            if fr.error is None and fr.text is not None and len(fr.text) < 200:
                # tiny content; try Playwright if not yet booted
                if pw_browser is None:
                    try:
                        from playwright.sync_api import sync_playwright

                        pw_ctx = sync_playwright().start()
                        pw_browser = pw_ctx.chromium.launch(headless=True)
                        fr = fetch_page(u["url"], playwright_browser=pw_browser)
                    except Exception as e:
                        stats.errors.append(
                            {
                                "url_id": u["id"],
                                "url": u["url"],
                                "phase": "playwright_boot",
                                "error": f"{type(e).__name__}: {e}",
                            }
                        )
                else:
                    # browser already booted — retry through it
                    fr = fetch_page(u["url"], playwright_browser=pw_browser)
                if fr.used_playwright:
                    stats.urls_via_playwright += 1

            if fr.error is not None or not fr.text:
                stats.urls_fetch_failed += 1
                stats.bump_category(u["source_category"], "failed")
                update_discovered_url(
                    conn,
                    url_id=u["id"],
                    fetch_status="failed",
                    content_hash=None,
                    classified_relevance=None,
                )
                if i % HEARTBEAT_EVERY == 0 or i == 1 or stats.urls_fetch_failed <= 3:
                    print(
                        f"[{i:>4}/{total}] FETCH_FAIL  {u['source_category']:30s} "
                        f"{u['state']}  err={fr.error}  cumcost=${stats.cost_usd:.4f}",
                        flush=True,
                    )
                if i % CHECKPOINT_EVERY == 0:
                    write_checkpoint(stats, total=total)
                continue

            # 2) Truncate
            content = _truncate(fr.text)
            chash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

            # 3) Cache lookup
            cache_cur = conn.cursor()
            try:
                cached = cache_helper.lookup(cache_cur, content_hash=chash, prompt_hash=phash)
            finally:
                cache_cur.close()

            if cached is not None:
                stats.urls_cache_hit += 1
                stats.bump_category(u["source_category"], "cache_hit")
                tool_input = cached["response_json"]
                if isinstance(tool_input, str):
                    tool_input = json.loads(tool_input)
                in_tok = _to_int(cached.get("input_tokens"))
                out_tok = _to_int(cached.get("output_tokens"))
                call_err = None
            else:
                tool_input, in_tok, out_tok, call_err = haiku_extract(
                    content=content,
                    url=u["url"],
                    category=u["source_category"],
                    state_name=STATE_NAME_LOOKUP.get(u["state"], u["state"]),
                    state_abbr=u["state"],
                    client=client,
                )
                stats.input_tokens += in_tok
                stats.output_tokens += out_tok
                call_cost = (
                    in_tok * INPUT_USD_PER_MTOK / 1_000_000
                    + out_tok * OUTPUT_USD_PER_MTOK / 1_000_000
                )
                stats.cost_usd += call_cost
                if call_err is None and tool_input is not None:
                    # Cache the successful response
                    write_cur = conn.cursor()
                    try:
                        cache_helper.store(
                            write_cur,
                            content_hash=chash,
                            prompt_hash=phash,
                            response_json=tool_input,
                            model_id=HAIKU_MODEL_ID,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                        )
                        conn.commit()
                    finally:
                        write_cur.close()

            if call_err is not None or tool_input is None:
                stats.urls_haiku_failed += 1
                stats.bump_category(u["source_category"], "haiku_failed")
                stats.errors.append(
                    {
                        "url_id": u["id"],
                        "url": u["url"],
                        "phase": "haiku",
                        "error": call_err or "no_tool_input",
                    }
                )
                update_discovered_url(
                    conn,
                    url_id=u["id"],
                    fetch_status="failed",
                    content_hash=chash,
                    classified_relevance=None,
                )
                if i % HEARTBEAT_EVERY == 0 or stats.urls_haiku_failed <= 3:
                    print(
                        f"[{i:>4}/{total}] HAIKU_FAIL  {u['source_category']:30s} "
                        f"{u['state']}  err={call_err}  cumcost=${stats.cost_usd:.4f}",
                        flush=True,
                    )
                if i % CHECKPOINT_EVERY == 0:
                    write_checkpoint(stats, total=total)
                time.sleep(POLITE_DELAY_S)
                continue

            stats.urls_fetched_ok += 1
            stats.bump_category(u["source_category"], "fetched")

            classification = tool_input.get("page_classification", "uncertain")
            stats.bump_category(u["source_category"], f"page_{classification}")
            facilities = tool_input.get("facilities") or []

            update_discovered_url(
                conn,
                url_id=u["id"],
                fetch_status="fetched",
                content_hash=chash,
                classified_relevance=classification,
            )

            # 4) Insert candidate rows
            inserted_this_url = 0
            for fac in facilities:
                if not isinstance(fac, dict):
                    continue
                conf = fac.get("classification_confidence")
                if conf not in {"high", "medium", "low"}:
                    conf = "low"
                insert_candidate(
                    conn,
                    url_id=u["id"],
                    raw_payload=fac,
                    classification_confidence=conf,
                )
                inserted_this_url += 1
            stats.candidates_inserted += inserted_this_url
            stats.bump_category(u["source_category"], "candidates", inserted_this_url)

            # Heartbeat
            if i % HEARTBEAT_EVERY == 0 or i == 1:
                print(
                    f"[{i:>4}/{total}] {u['source_category']:30s} {u['state']}  "
                    f"page={classification:9s}  candidates={inserted_this_url:>2}  "
                    f"{'CACHE' if cached is not None else '     '}  "
                    f"cumcost=${stats.cost_usd:.4f}",
                    flush=True,
                )

            # Checkpoint
            if i % CHECKPOINT_EVERY == 0:
                write_checkpoint(stats, total=total)

            # Polite delay (skip on cache hits — no upstream load)
            if cached is None:
                time.sleep(POLITE_DELAY_S)
    finally:
        if pw_browser is not None:
            with contextlib.suppress(Exception):
                pw_browser.close()
        if pw_ctx is not None:
            with contextlib.suppress(Exception):
                pw_ctx.stop()
        conn.close()

    elapsed = time.time() - started
    write_checkpoint(stats, total=total)

    # ---- Summary ----
    print(flush=True)
    print(
        f"[extraction] processed {stats.urls_processed}/{total} URLs in {elapsed / 60:.1f} min",
        flush=True,
    )
    print(f"  fetched ok:     {stats.urls_fetched_ok}", flush=True)
    print(f"  fetch failed:   {stats.urls_fetch_failed}", flush=True)
    print(f"  haiku failed:   {stats.urls_haiku_failed}", flush=True)
    print(f"  via playwright: {stats.urls_via_playwright}", flush=True)
    print(f"  cache hits:     {stats.urls_cache_hit}", flush=True)
    print(f"  candidates:     {stats.candidates_inserted}", flush=True)
    print(f"  input tokens:   {stats.input_tokens:,}", flush=True)
    print(f"  output tokens:  {stats.output_tokens:,}", flush=True)
    print(f"  cost USD:       ${stats.cost_usd:.4f}  (cap ${HAIKU_BUDGET_USD:.2f})", flush=True)
    if hit_cap:
        print("  [WARN] extraction stopped early due to cost cap.", flush=True)

    print(flush=True)
    print("[extraction] per-category result:", flush=True)
    for cat, c in sorted(stats.by_category.items()):
        print(
            f"  {cat:32s}  urls={c.get('urls', 0):>4}  "
            f"fetched={c.get('fetched', 0):>4}  "
            f"failed={c.get('failed', 0):>3}  "
            f"haiku_fail={c.get('haiku_failed', 0):>3}  "
            f"candidates={c.get('candidates', 0):>4}  "
            f"rel={c.get('page_relevant', 0):>4}  "
            f"unrel={c.get('page_unrelated', 0):>4}  "
            f"uncert={c.get('page_uncertain', 0):>3}",
            flush=True,
        )

    if stats.errors:
        print(flush=True)
        print(f"[extraction] error sample (first 10 of {len(stats.errors)}):", flush=True)
        for e in stats.errors[:10]:
            print(
                f"  url_id={e['url_id']:>5}  phase={e['phase']:18s}  {e['error'][:120]}",
                flush=True,
            )

    return 0 if not hit_cap else 2


if __name__ == "__main__":
    raise SystemExit(main())
