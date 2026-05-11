"""
sample_pulls.py
Day-1 reconnaissance — pull <=100-row samples from each candidate data source,
save raw payloads to local/source_samples/ (gitignored), and write a compact
JSON summary that source_audit_phase0.md is built from.

READ-ONLY. NO DATABASE WRITES. Per the kickoff brief, task 8.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "local" / "source_samples"
LOCAL.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Axiom-Insights-ArchLegacy/0.1 (Day-1 source audit; "
    "operator: Axiom Insights; contact: geturfreebmw24@gmail.com)"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
TIMEOUT = 30


@dataclass
class SampleResult:
    slug: str
    name: str
    url_tried: str
    status: str  # ok | empty | http_error | timeout | exception | declined_robots
    http_status: int | None = None
    rows_observed: int | None = None
    columns_or_keys: list[str] = field(default_factory=list)
    bytes_received: int | None = None
    elapsed_sec: float | None = None
    notes: str = ""
    saved_to: str | None = None


def _save(payload: bytes, slug: str, ext: str) -> str:
    """Save raw payload to the local samples dir. Returns relative path."""
    fname = f"{slug}.{ext}"
    out = LOCAL / fname
    out.write_bytes(payload)
    return str(out.relative_to(ROOT))


def _get(url: str, params: dict | None = None, accept: str | None = None) -> requests.Response:
    h = dict(HEADERS)
    if accept:
        h["Accept"] = accept
    return requests.get(url, params=params, headers=h, timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# EPA ECHO — use the documented REST API (allowed) rather than scraping the
# disallowed search-results pages. Two-step: get_facilities returns a QID
# (server-side cached query); get_download returns rows for that QID.
# ---------------------------------------------------------------------------
def sample_epa_echo(state: str) -> SampleResult:
    slug = f"epa_echo_{state.lower()}"
    name = f"EPA ECHO facilities (CWA, state={state})"
    base = "https://echodata.epa.gov/echo/cwa_rest_services"
    setup_params = {
        "output": "JSON",
        "p_st": state,
        "p_act": "Y",
        "responseset": "1",  # we only need the QID; don't pay for inline rows
    }
    t0 = time.time()
    try:
        r1 = _get(f"{base}.get_facilities", params=setup_params, accept="application/json")
        if r1.status_code != 200:
            return SampleResult(
                slug,
                name,
                r1.url,
                "http_error",
                r1.status_code,
                None,
                [],
                len(r1.content),
                round(time.time() - t0, 2),
                notes=f"setup non-200; body head={r1.text[:200]!r}",
            )
        meta = r1.json().get("Results", {}) or {}
        qid = meta.get("QueryID")
        total_rows = meta.get("QueryRows")
        if not qid:
            return SampleResult(
                slug,
                name,
                r1.url,
                "empty",
                r1.status_code,
                None,
                sorted(meta.keys()),
                len(r1.content),
                round(time.time() - t0, 2),
                notes="get_facilities returned no QueryID",
            )

        # Now fetch 100 rows via the CSV download endpoint with a curated
        # column set covering everything the canonical schema needs.
        qcolumns = ",".join(
            [
                "1",  # REGISTRY_ID (FRS)
                "3",  # SOURCE_ID (NPDES permit)
                "4",  # CWA_FACILITY_TYPE_INDICATOR
                "21",  # FAC_NAME
                "22",  # FAC_STREET
                "23",  # FAC_CITY
                "24",  # FAC_STATE
                "25",  # FAC_ZIP
                "26",  # FAC_COUNTY
                "27",  # FAC_LAT
                "28",  # FAC_LONG
            ]
        )
        dl_params = {
            "qid": qid,
            "qcolumns": qcolumns,
            "responseset": "100",
            "output": "CSV",
        }
        r2 = _get(f"{base}.get_download", params=dl_params, accept="text/csv")
        elapsed = round(time.time() - t0, 2)
        if r2.status_code != 200:
            return SampleResult(
                slug,
                name,
                r2.url,
                "http_error",
                r2.status_code,
                None,
                [],
                len(r2.content),
                elapsed,
                notes=f"download non-200; total_rows={total_rows}; body head={r2.text[:200]!r}",
            )
        body = r2.content
        lines = body.decode("utf-8", errors="replace").splitlines()
        header = lines[0].split(",") if lines else []
        rows_observed = max(0, len(lines) - 1)
        saved = _save(body, slug, "csv")
        return SampleResult(
            slug,
            name,
            r2.url,
            "ok" if rows_observed else "empty",
            r2.status_code,
            rows_observed,
            header[:30],
            len(body),
            elapsed,
            notes=f"ECHO CWA REST two-step pull; QueryRows total={total_rows}",
            saved_to=saved,
        )
    except requests.Timeout:
        return SampleResult(
            slug,
            name,
            base,
            "timeout",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes="timeout",
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            base,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


# ---------------------------------------------------------------------------
# EPA CWNS 2022 — static dataset. The downloads page is a Drupal page.
# For Day-1 we record what's on the downloads page (file list) and grab the
# data dictionary if available. We don't pull the full 50+MB datasets in
# this recon pass.
# ---------------------------------------------------------------------------
def sample_epa_cwns() -> SampleResult:
    slug = "epa_cwns_2022"
    name = "EPA CWNS 2022 downloads index"
    url = "https://www.epa.gov/cwns/clean-watersheds-needs-survey-cwns-2022-report-and-data"
    t0 = time.time()
    try:
        r = _get(url, accept="text/html")
        elapsed = round(time.time() - t0, 2)
        # Pull all .xlsx / .zip / .accdb / .csv hrefs as the "schema observed".
        hrefs = re.findall(
            r'href="([^"]+\.(?:xlsx|zip|accdb|csv|xls))"', r.text, flags=re.IGNORECASE
        )
        hrefs = sorted({urljoin(url, h) for h in hrefs})
        saved = _save(r.content, slug, "html")
        return SampleResult(
            slug,
            name,
            url,
            "ok" if hrefs else "empty",
            r.status_code,
            len(hrefs),
            hrefs[:50],
            len(r.content),
            elapsed,
            notes="Static dataset; we record downloads-index URLs as the "
            "'schema' for Day-1. Full pull deferred to Phase 1 loader.",
            saved_to=saved,
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            url,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


# ---------------------------------------------------------------------------
# TCEQ — Central Registry (CRPUB) is robots-disallowed. We pull the TCEQ
# Public Data Lookup index instead, where the same records are published as
# downloadable CSV/XLSX. This is the supported path.
# ---------------------------------------------------------------------------
def sample_tceq_lookup_data() -> SampleResult:
    slug = "tceq_public_data_lookup"
    name = "TCEQ Public Data Lookup index"
    url = "https://www.tceq.texas.gov/agency/data/lookup-data"
    t0 = time.time()
    try:
        r = _get(url, accept="text/html")
        elapsed = round(time.time() - t0, 2)
        # Capture link text → href pairs that look like data downloads or
        # registry tables we care about.
        link_pattern = re.compile(r'<a [^>]*href="([^"]+)"[^>]*>([^<]+)</a>', flags=re.IGNORECASE)
        items: list[str] = []
        for href, text in link_pattern.findall(r.text):
            t = text.strip()
            if not t:
                continue
            low = t.lower()
            if any(
                k in low
                for k in (
                    "wastewater",
                    "domestic",
                    "biosolids",
                    "sludge",
                    "septage",
                    "permit",
                    "registry",
                    "facility",
                )
            ):
                items.append(f"{t} -> {urljoin(url, href)}")
        items = sorted(set(items))
        saved = _save(r.content, slug, "html")
        return SampleResult(
            slug,
            name,
            url,
            "ok" if items else "empty",
            r.status_code,
            len(items),
            items[:30],
            len(r.content),
            elapsed,
            notes="Day-1 recon shows the catalogue of CSV/XLSX downloads; "
            "specific download URLs feed Phase 2 TCEQ loaders.",
            saved_to=saved,
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            url,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


def sample_tceq_domestic_wastewater() -> SampleResult:
    """Fetch the TCEQ Domestic Wastewater permits landing page."""
    slug = "tceq_domestic_wastewater"
    name = "TCEQ Domestic Wastewater Permits landing"
    url = "https://www.tceq.texas.gov/permitting/wastewater/municipal"
    t0 = time.time()
    try:
        r = _get(url, accept="text/html")
        elapsed = round(time.time() - t0, 2)
        # Count internal links and detect document references — we are looking
        # for the structure of the page and links to downloads / permit lists.
        links = re.findall(r'href="([^"]+)"', r.text, flags=re.IGNORECASE)
        internal = [u for u in links if u.startswith("/") or "tceq.texas.gov" in u]
        docs = [u for u in internal if re.search(r"\.(pdf|xlsx|xls|csv|doc|docx)(\?|$)", u, re.I)]
        saved = _save(r.content, slug, "html")
        return SampleResult(
            slug,
            name,
            url,
            "ok" if internal else "empty",
            r.status_code,
            len(internal),
            docs[:30],
            len(r.content),
            elapsed,
            notes="HTML landing page. Permit-list and biosolids download links "
            "below the fold feed the Phase 2 TCEQ loader.",
            saved_to=saved,
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            url,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


# ---------------------------------------------------------------------------
# NC DEQ — DWR (water resources) and DWM (waste management) division pages.
# Both Drupal-served, robots permissive. We record the structure of each
# division's program index.
# ---------------------------------------------------------------------------
def sample_nc_deq_dwr() -> SampleResult:
    slug = "nc_deq_dwr"
    name = "NC DEQ DWR — Permits and Programs"
    url = "https://www.deq.nc.gov/about/divisions/water-resources/water-resources-permits"
    t0 = time.time()
    try:
        r = _get(url, accept="text/html")
        elapsed = round(time.time() - t0, 2)
        links = re.findall(r'href="([^"]+)"', r.text, flags=re.IGNORECASE)
        internal = [u for u in links if u.startswith("/") or "deq.nc.gov" in u]
        docs = [u for u in internal if re.search(r"\.(pdf|xlsx|xls|csv)(\?|$)", u, re.I)]
        saved = _save(r.content, slug, "html")
        return SampleResult(
            slug,
            name,
            url,
            "ok" if internal else "empty",
            r.status_code,
            len(internal),
            docs[:30],
            len(r.content),
            elapsed,
            notes="DWR permits index. NPDES, biosolids/residuals, and "
            "POTW receiving programs are reached from here.",
            saved_to=saved,
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            url,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


def sample_nc_deq_dwm() -> SampleResult:
    slug = "nc_deq_dwm"
    name = "NC DEQ DWM — Solid Waste Section"
    url = "https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section"
    t0 = time.time()
    try:
        r = _get(url, accept="text/html")
        elapsed = round(time.time() - t0, 2)
        links = re.findall(r'href="([^"]+)"', r.text, flags=re.IGNORECASE)
        internal = [u for u in links if u.startswith("/") or "deq.nc.gov" in u]
        docs = [u for u in internal if re.search(r"\.(pdf|xlsx|xls|csv)(\?|$)", u, re.I)]
        saved = _save(r.content, slug, "html")
        return SampleResult(
            slug,
            name,
            url,
            "ok" if internal else "empty",
            r.status_code,
            len(internal),
            docs[:30],
            len(r.content),
            elapsed,
            notes="DWM solid-waste section. Transfer stations and composting "
            "facility lists are reached from here.",
            saved_to=saved,
        )
    except Exception as e:
        return SampleResult(
            slug,
            name,
            url,
            "exception",
            None,
            None,
            [],
            None,
            round(time.time() - t0, 2),
            notes=str(e),
        )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    results: list[SampleResult] = []
    pulls = [
        ("ECHO TX", lambda: sample_epa_echo("TX")),
        ("ECHO NC", lambda: sample_epa_echo("NC")),
        ("CWNS 2022", sample_epa_cwns),
        ("TCEQ Lookup Data", sample_tceq_lookup_data),
        ("TCEQ Dom WW", sample_tceq_domestic_wastewater),
        ("NC DEQ DWR", sample_nc_deq_dwr),
        ("NC DEQ DWM", sample_nc_deq_dwm),
    ]
    for label, fn in pulls:
        print(f"[*] {label} …", flush=True)
        try:
            res = fn()
        except Exception as e:
            res = SampleResult(
                label.lower().replace(" ", "_"),
                label,
                "",
                "exception",
                None,
                None,
                [],
                None,
                None,
                notes=f"top-level exception: {e}",
            )
        results.append(res)
        print(
            f"    -> {res.status} "
            f"({res.http_status}) rows={res.rows_observed} "
            f"saved={res.saved_to or '-'}",
            flush=True,
        )

    summary_path = LOCAL / "_summary.json"
    summary_path.write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    print(f"\nSummary written to {summary_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
