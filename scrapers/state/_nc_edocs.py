"""
Shared NC DEQ edocs (Laserfiche WebLink) discovery + fetch helper.

Both NC DEQ DWM rosters -- the Solid Waste Permitted Facilities list and the
Septage Firm list -- are published as XLSX documents in NC DEQ's Laserfiche
WebLink repository at ``edocs.deq.nc.gov/WasteManagement/``. Each document is
addressed by a numeric ``docid`` that ROTATES every publication period (the
file names carry a ``_YYYYMMDD`` suffix), so the docid must be DISCOVERED at
run time, never hardcoded.

Access reality (established 2026-06-02; supersedes the old "network-layer WAF
block" note in ``docs/nc_deq_audit.md`` section C):

* The original Phase 2 audit probed edocs from a non-US workstation IP, saw
  TCP timeouts, and concluded the host was network-blocked. That was a
  GEOGRAPHIC block, not a network-layer one. A US-region egress (the GitHub
  Actions ubuntu runner) reaches edocs fine -- proven by the Task 3 session
  probe (run 26796842744) and the docid-discovery probe.
* A docid URL only serves its file inside a real browser session: a bare
  stateless request bounces to ``Error.aspx``. We must land on the WebLink
  welcome page first to pick up the four session cookies, then request the
  docid download in the SAME browser context. This module does exactly that
  via Playwright -- a plain headless Chromium from the runner's normal
  egress. No anti-detection escalation, IP rotation, or proxy use.

Two-tier docid discovery:

1. PRIMARY -- scrape the public DEQ "Solid Waste Facility Lists" web page
   (``www.deq.nc.gov``, reachable + robots-permissive). That page links each
   roster by its CURRENT edocs ``docid=``; DEQ updates the link when a new
   file is published, so the page is the canonical current-file pointer.
   Robust to both docid rotation and any edocs folder reshuffle.
2. FALLBACK -- browse the edocs folder that holds both rosters
   ("Permitting Branch Webpage", folder id 1684679) in-session and pick the
   document whose name starts with the report's prefix (newest by the
   trailing ``_YYYYMMDD`` when more than one is present).

If both discovery tiers fail, ``fetch_report_xlsx`` raises ``EdocsFetchError``
and the calling loader falls back to its manual-drop pickup directory.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from pathlib import Path

import requests

BASE = "https://edocs.deq.nc.gov/WasteManagement/"
DEQ_FACILITY_LISTS_PAGE = (
    "https://www.deq.nc.gov/about/divisions/waste-management/"
    "solid-waste-section/"
    "solid-waste-permitted-facility-information-and-guidance/"
    "solid-waste-facility-lists"
)
# Present as a normal desktop browser. Not anti-detection -- the runner IS a
# normal client; this only avoids the default python-requests UA being treated
# oddly by the DEQ Drupal front end.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# edocs folder that holds both rosters (discovered 2026-06-02). Used only by
# the fallback discovery tier; the primary tier never needs a folder id.
FALLBACK_FOLDER_ID = "1684679"  # "Permitting Branch Webpage"

# report key -> document-name prefix as published in edocs / WebLink.
REPORTS: dict[str, str] = {
    "solid_waste": "PermittedFacilityList",
    "septage_firm": "PermittedSeptageFirm",
}

WEBSITE_TIMEOUT = 45
NAV_TIMEOUT_MS = 45_000
DOWNLOAD_TIMEOUT_MS = 20_000

_DOCID_RE = re.compile(r"[?&]docid=(\d+)", re.I)
_DOCVIEW_ID_RE = re.compile(r"[?&]id=(\d+)", re.I)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DATE_SUFFIX_RE = re.compile(r"(\d{8})$")


class EdocsFetchError(RuntimeError):
    """Autonomous discovery+fetch from edocs failed. Callers catch this and
    fall back to their manual-drop pickup directory."""


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _classify(text: str, href: str = "") -> str | None:
    """Map a roster link / document name to a report key, or None.

    Handles both the friendly website link text ("Solid Waste Permitted
    Facilities", "Septage Firm") and the raw WebLink document name
    ("PermittedFacilityList_20260428", "PermittedSeptageFirm_20260428").
    """
    t = (text or "").lower()
    n = _norm(text)
    if "septage" in t or n.startswith("permittedseptagefirm"):
        return "septage_firm"
    if n.startswith("permittedfacilitylist") or (
        "permitted" in t and ("facilit" in t or "solid waste" in t)
    ):
        return "solid_waste"
    return None


def discover_docids_via_website(log: Callable[[str], None] = print) -> dict[str, str]:
    """PRIMARY discovery. Fetch the DEQ Solid Waste Facility Lists page and
    return ``{report_key: docid}`` for every roster link found. A missing
    roster is simply absent from the returned dict; a transport error raises
    a ``requests`` exception, which the orchestrator catches."""
    r = requests.get(
        DEQ_FACILITY_LISTS_PAGE,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=WEBSITE_TIMEOUT,
    )
    r.raise_for_status()
    out: dict[str, str] = {}
    for href, inner in _ANCHOR_RE.findall(r.text):
        m = _DOCID_RE.search(href)
        if not m:
            continue
        report = _classify(_strip_tags(inner), href)
        if report and report not in out:
            out[report] = m.group(1)
            log(f"DEQ-website: {report} -> docid={m.group(1)}")
    return out


def electronicfile_url(docid: str) -> str:
    return f"{BASE}ElectronicFile.aspx?docid={docid}&dbid=0&repo=WasteManagement"


# --------------------------------------------------------------------------
# Playwright session helpers (playwright is imported lazily so importing this
# module -- e.g. for the website-only discovery path or under ruff/pytest --
# never requires the browser binary).
# --------------------------------------------------------------------------
def _safe_goto(page, url: str) -> None:
    # page.goto raises when the response is a file download rather than a
    # navigable document -- that is the success path for ElectronicFile.aspx,
    # so the navigation error is expected and swallowed here.
    with contextlib.suppress(Exception):
        page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)


def _collect_doc_anchors(page) -> list[tuple[str, str]]:
    """Return ``[(text, docid)]`` for DocView / ElectronicFile links rendered
    in the current folder page."""
    try:
        anchors = page.eval_on_selector_all(
            "a",
            "els => els.map(e => [ (e.textContent||'').replace(/\\s+/g,' ')"
            ".trim().slice(0,160), e.href||'' ])",
        )
    except Exception:
        return []
    docs: list[tuple[str, str]] = []
    for text, href in anchors:
        low = (href or "").lower()
        if "docview.aspx" in low:
            m = _DOCVIEW_ID_RE.search(href)
        elif "electronicfile.aspx" in low:
            m = _DOCID_RE.search(href)
        else:
            continue
        if m:
            docs.append((text, m.group(1)))
    return docs


def _discover_docid_via_folder(ctx, name_prefix: str, log: Callable[[str], None]) -> str | None:
    """FALLBACK discovery. Browse ``FALLBACK_FOLDER_ID`` in-session and return
    the docid whose document name starts with ``name_prefix`` (newest by the
    trailing ``_YYYYMMDD``), or None."""
    page = ctx.new_page()
    try:
        page.goto(
            f"{BASE}Browse.aspx?id={FALLBACK_FOLDER_ID}&dbid=0&repo=WasteManagement",
            wait_until="networkidle",
            timeout=NAV_TIMEOUT_MS,
        )
        docs = _collect_doc_anchors(page)
    except Exception as e:
        log(f"folder-fallback browse failed: {type(e).__name__}: {e}")
        return None
    finally:
        page.close()

    pref = _norm(name_prefix)
    cands = [(text, did) for (text, did) in docs if _norm(text).startswith(pref)]
    if not cands:
        return None

    def _recency(item: tuple[str, str]) -> tuple[int, int]:
        text, did = item
        m = _DATE_SUFFIX_RE.search(_norm(text))
        return (int(m.group(1)) if m else 0, int(did))

    cands.sort(key=_recency, reverse=True)
    log(f"folder-fallback: {len(cands)} match(es); newest -> docid={cands[0][1]}")
    return cands[0][1]


def _establish_session(ctx, log: Callable[[str], None]) -> None:
    """Land on the WebLink welcome page so the context picks up the session
    cookies required for any docid download."""
    page = ctx.new_page()
    try:
        resp = page.goto(BASE, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        log(
            f"session established: status={resp.status if resp else None} "
            f"cookies={len(ctx.cookies())}"
        )
    finally:
        page.close()


def _download_xlsx(ctx, docid: str, log: Callable[[str], None]) -> bytes:
    from playwright.sync_api import TimeoutError as PWTimeout

    page = ctx.new_page()
    try:
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl:
                _safe_goto(page, electronicfile_url(docid))
            download = dl.value
        except PWTimeout as e:
            raise EdocsFetchError(
                f"no download fired for docid={docid} -- session missing or docid invalid"
            ) from e
        local = download.path()
        if not local:
            raise EdocsFetchError(f"download for docid={docid} produced no local file")
        body = Path(local).read_bytes()
        name = download.suggested_filename
    finally:
        page.close()

    if body[:4] != b"PK\x03\x04":
        raise EdocsFetchError(f"docid={docid} did not return an XLSX (first 4 bytes={body[:4]!r})")
    log(f"downloaded docid={docid}: {len(body):,} bytes name={name!r}")
    return body


def fetch_report_xlsx(report: str, *, log: Callable[[str], None] = print) -> tuple[bytes, str, str]:
    """Discover the current docid for ``report`` and fetch its XLSX from edocs
    in a single browser session.

    Returns ``(xlsx_bytes, docid, source_url)``. Raises ``EdocsFetchError`` on
    any failure so the caller can fall back to its manual-drop pickup.
    ``report`` must be a key of :data:`REPORTS`.
    """
    if report not in REPORTS:
        raise ValueError(f"unknown report {report!r}; expected one of {sorted(REPORTS)}")
    name_prefix = REPORTS[report]

    # Tier 1: DEQ website (no browser needed).
    docid: str | None = None
    try:
        docid = discover_docids_via_website(log).get(report)
    except Exception as e:
        log(f"DEQ-website discovery failed: {type(e).__name__}: {e}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(accept_downloads=True, user_agent=USER_AGENT)
            _establish_session(ctx, log)
            if not docid:
                log(f"website discovery yielded no docid for {report}; trying folder fallback")
                docid = _discover_docid_via_folder(ctx, name_prefix, log)
            if not docid:
                raise EdocsFetchError(
                    f"could not discover a docid for report {report!r} via the DEQ "
                    f"website or the edocs folder fallback"
                )
            body = _download_xlsx(ctx, docid, log)
        finally:
            browser.close()

    return body, docid, electronicfile_url(docid)
