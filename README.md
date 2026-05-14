# Arch Legacy Partners — Wastewater Facility Database

Postgres-backed wastewater facility database for Texas and North Carolina, with
automated monthly refresh from federal and state sources, LLM-assisted
acceptance-flag enrichment, discovery-crawl for sources outside automated
reach, field-level provenance, and CSV export. Built by Axiom Insights for
Arch Legacy Partners (Austin Fruchter, client contact). Internal data
infrastructure — no UI.

## Current state

| | |
|---|---:|
| Customer-facing canonical facilities (`v_all_in_scope`) | **1,970** |
| Discovery candidates pending review (held out of scope) | 141 |
| Federal + state sources contributing | 6 |
| Categories covered (per state) | 7 |
| Acceptance-flag affirmatives across the 1,970 set | 978 (509 septage / 233 grease / 236 portable) |
| Cumulative API spend through build | ~$21 (Anthropic $13.62 / Brave $6.50) |
| Test coverage | 43 unit + integration tests |
| CI on `main` | green |

The dataset breaks down per state × category in
[docs/data_quality_scorecard_v1.md](docs/data_quality_scorecard_v1.md).
Known scope concessions and TPIA / manual-drop fallback paths are documented
in [docs/v1_scope_limitations.md](docs/v1_scope_limitations.md).

## Quick start

The data lives in Supabase. Day-1 operators don't need to clone the repo to
read it.

1. **Log in** to the Supabase dashboard for the project (URL handed off
   during transfer).
2. **Open Table Editor.** The views to start with:
   - `v_all_in_scope` — every canonical facility in scope (1,970 rows).
     Same row set as `exports/facilities_primary.csv`.
   - `v_tx_in_scope` / `v_nc_in_scope` — per-state slices.
   - 14 per-state-per-type views (e.g. `v_nc_private_regional_septage_facility`,
     `v_tx_transfer_station`) for category-specific slices.
   - 3 acceptance-flag positives (`v_accepts_septage`,
     `v_accepts_grease_trap`, `v_accepts_portable_toilet`) — facilities
     whose Phase 4 enrichment surfaced explicit `Yes`.
   - `v_discovery_review` — the offline review workspace for the discovery
     queue (141 rows pending).
3. **Reference queries** in [`db/queries/`](db/queries/) cover common asks:
   per-source row counts, facilities missing coords, facilities by county,
   hold-review queue, duplicate candidates.
4. **Monthly CSV refresh** lands as an auto-opened PR against `main` on the
   1st of each month at 09:00 UTC. The PR description carries the diff
   summary; the refresh branch is named `refresh/<YYYY-MM-DD>`. Merging the
   PR ships the new CSV at `exports/facilities_primary.csv` (and the
   provenance sibling). Don't merge a PR whose CI is red; see the runbook
   for diagnosis.

## Operations

| If you need to … | Read |
|---|---|
| Set up the six credentials (Anthropic, Brave, Supabase ×3, SMTP) | [docs/runbook_key_rotation.md](docs/runbook_key_rotation.md) — §7 handoff sequence |
| Run / diagnose the monthly refresh | [docs/runbook_monthly_refresh.md](docs/runbook_monthly_refresh.md) |
| Adjudicate the discovery review queue | [docs/runbook_review_queue.md](docs/runbook_review_queue.md) |
| Investigate a row-level data issue | [docs/runbook_data_quality.md](docs/runbook_data_quality.md) |
| Add a 3rd / 4th state post-v1 | [docs/runbook_add_a_state.md](docs/runbook_add_a_state.md) |
| Understand source provenance | [docs/sources.md](docs/sources.md) |
| Look up a column meaning | [docs/data_dictionary.md](docs/data_dictionary.md) |
| Understand the access-layer views | [docs/access_layer.md](docs/access_layer.md) |
| Trace a build decision | [docs/build_log.md](docs/build_log.md) |

## Documentation index

All operator-facing documentation lives in [`docs/`](docs/). Each is
operator-runnable in isolation; no doc is a prerequisite for another except
where explicitly cross-referenced.

| Doc | Purpose |
|---|---|
| [docs/schema.md](docs/schema.md) | ER diagram + table-by-table data model. |
| [docs/data_dictionary.md](docs/data_dictionary.md) | Column-by-column reference for every public table and view. |
| [docs/sources.md](docs/sources.md) | Per-source provenance, robots posture, refresh cadence. |
| [docs/access_layer.md](docs/access_layer.md) | The 20 access-layer views and verification queries. |
| [docs/v1_scope_limitations.md](docs/v1_scope_limitations.md) | What is not in the v1 dataset, why, and the alternative paths (TPIA, manual drop). Includes the §6 monthly operating-cost frame. |
| [docs/data_quality_scorecard_v1.md](docs/data_quality_scorecard_v1.md) | Live-Supabase-generated dataset snapshot. Regenerate via `local/_build_scorecard.py` after any pipeline pass. |
| [docs/runbook_monthly_refresh.md](docs/runbook_monthly_refresh.md) | Operator runbook for the monthly cron. |
| [docs/runbook_data_quality.md](docs/runbook_data_quality.md) | Investigating row-level issues. |
| [docs/runbook_key_rotation.md](docs/runbook_key_rotation.md) | Six-credential rotation procedure + the day-1 handoff sequence (§7). |
| [docs/runbook_add_a_state.md](docs/runbook_add_a_state.md) | $40-per-state expansion path. |
| [docs/runbook_review_queue.md](docs/runbook_review_queue.md) | Phase 4.5 discovery-candidate adjudication workflow. |
| [docs/build_log.md](docs/build_log.md) | Daily journal of the build (~50 entries). |
| [docs/source_audit_phase0.md](docs/source_audit_phase0.md) | Phase 0 source-by-source sample-pull audit. |
| [docs/nc_deq_audit.md](docs/nc_deq_audit.md) | NC DEQ source-discovery audit detail. |
| [docs/tceq_pdl_audit.md](docs/tceq_pdl_audit.md) | TCEQ Public Data Lookup audit detail. |
| [docs/phase_1_2_coverage_scorecard.md](docs/phase_1_2_coverage_scorecard.md) | End-of-Phase-2 coverage snapshot. |
| [docs/demo_notes_2026-05-18.md](docs/demo_notes_2026-05-18.md) | Live-Supabase walkthrough script for the handoff demo. |
| [docs/checkpoint_2_self_review_v2.md](docs/checkpoint_2_self_review_v2.md) | End-of-scaffolding checkpoint review. |

## Stack

- **Database** Postgres 16 on Supabase
- **Language** Python 3.11
- **Scraping** Playwright, BeautifulSoup, pdfplumber
- **Entity resolution** RapidFuzz (name match) + 200m haversine proximity tiebreak
- **Geocoding** US Census Geocoder
- **LLM enrichment / discovery extraction** Anthropic Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)
- **Web search** Brave Search API (paid tier required — see runbook §2)
- **Orchestration** GitHub Actions, monthly cron `0 9 1 * *`

## Repo layout

```
scrapers/             per-source loaders (federal, state, county, discovery)
supabase/migrations/  SQL migrations applied via `supabase db push`
resolver/             entity resolution + candidate-import path
enrichment/           Phase 4 LLM acceptance-flag enrichment + caching
exports/              CSV export scripts (primary + provenance)
orchestration/        cross-cutting drivers (geocoder, drift detector)
config/               controlled vocabularies, source seeds, discovery queries
db/queries/           reference SQL for common asks
docs/                 schema, runbooks, audits, build log
tests/                pytest suite
.github/workflows/    CI + monthly_refresh
```

## Local setup

Operators rarely need a local checkout — most work happens through Supabase
Table Editor and the GitHub Actions UI. For developers extending the
pipeline:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env   # then populate the six credentials per the rotation runbook
```

Verify the connection: `python -m orchestration.drift_detector` should exit 0
with a per-source drift report.

## Operating cost

Approximately **$15–30 per monthly refresh** at v1 data volume, broken down:

- Brave Search (paid tier): **$5–15** per refresh covering Phase 4
  enrichment's ~1,970 facility queries plus Phase 4.5 discovery scope.
  Paid tier is required from day 1 — the free tier exhausts inside a single
  refresh.
- Anthropic Haiku 4.5: **$10–15** per refresh for the enrichment pass at
  ~$0.003/facility plus discovery extraction.

A 4-state expansion (TX + NC + 2 additional) scales linearly to roughly
**$30–60 per refresh**. The full cost frame is in
[docs/v1_scope_limitations.md §6](docs/v1_scope_limitations.md#6-monthly-refresh-operational-costs-brave--anthropic).

## Support

Axiom Insights provides a **30-day post-launch bug-fix window** following the
2026-05-29 contract delivery. Bug reports during the window go to the
delivery contact email (handed off during transfer). After the window, the
codebase + documentation + Supabase project belong to Arch Legacy Partners
to maintain. The runbooks are written for operator-self-service; the
monthly refresh workflow is failure-alerting via SMTP.

## License

Proprietary. See [LICENSE](LICENSE). Source and build artifacts transfer to
Arch Legacy Partners on delivery; until then they sit under Axiom Insights.
