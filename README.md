# Arch Legacy Partners — Wastewater Facility Database

Internal Postgres-backed database of waste disposal facilities in Texas and
North Carolina across seven facility categories, with field-level provenance,
automated monthly refresh, CSV export, and full documentation.

Built and maintained by **Axiom Insights** for **Arch Legacy Partners** (client
contact: Austin Fruchter). Internal-use only — no customer-facing UI, no
internal web tool. Pure data infrastructure.

## Status

Phase 1, Day 1 — scaffolding in progress. See [docs/build_log.md](docs/build_log.md)
for daily progress entries.

## Facility categories

1. POTW (Publicly Owned Treatment Works) receiving stations
2. County manhole programs
3. Land application sites
4. Private and regional septage facilities
5. Composting facilities
6. Anaerobic digesters
7. Transfer stations

## Stack

- **Database** Postgres on Supabase (free tier)
- **Language** Python 3.11
- **Scraping** Playwright, BeautifulSoup, pdfplumber
- **Entity resolution** RapidFuzz
- **Geocoding** US Census Geocoder
- **LLM enrichment / discovery extraction** Anthropic Claude Haiku
- **Web search** Brave Search API
- **Orchestration** GitHub Actions, monthly cron

## Layout

```
/scrapers/         per-source loaders (federal, texas, north_carolina, counties, discovery)
/db/migrations/    SQL migrations applied to Supabase
/enrichment/       LLM waste-type enrichment + caching
/exports/          CSV export scripts (primary + provenance)
/orchestration/    cross-cutting drivers, entity resolution, drift detection
/config/           controlled vocabularies, source registry seeds, budget caps
/docs/             schema doc, data dictionary, runbooks, source audit
/tests/            pytest suite
/.github/workflows ci + monthly_refresh
```

## Setup (local)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env  # then fill in secrets
```

## Documentation

- [docs/build_log.md](docs/build_log.md) — daily build log
- [docs/source_audit_phase0.md](docs/source_audit_phase0.md) — Day-1 sample-pull
  audit per candidate source (pending)
- Schema doc, data dictionary, refresh runbook, add-a-state runbook, key
  rotation runbook, data-quality troubleshooting runbook — all delivered in
  Phase 6.

## License

Proprietary. See [LICENSE](LICENSE). Source and build artifacts transfer to
Arch Legacy Partners on delivery; until then they sit under Axiom Insights.
