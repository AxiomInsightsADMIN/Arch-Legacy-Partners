# Build Log

Daily status entries per the kickoff brief, section 13. Newest entries first.
Each entry covers: what completed, what is in-progress, what is blocked,
decisions made, deviations from the brief (and why).

---

## 2026-05-11 — Day 1 (post-task-8 checkpoint: tasks 9–10, both checkpoint reviews approved)

**Completed**
- Checkpoint-2 self-review v1 and v2 — Ryan approved v2. Seven failures
  addressed; B2 CWNS APEX `download-state-zip` discovery captured.
- **Task 9** — wrote `.github/workflows/ci.yml` with three jobs: ruff lint
  + ruff format check, pytest, and schema-migration apply against a
  throwaway `postgres:16-alpine` service container. The schema job runs
  both migrations, verifies the source table has exactly 12 rows after
  seeding, asserts every expected slug is present, confirms
  `tceq_central_registry.robots_txt_status='disallow'`, and re-applies the
  seed to validate idempotency. All secrets surfaced (ANTHROPIC_API_KEY,
  BRAVE_API_KEY, SUPABASE_* family, ALERT_EMAIL) even though several are
  unset in GitHub Actions today.
- **Task 9** — wrote `.github/workflows/monthly_refresh.yml` stub on a
  `0 9 1 * *` cron (1st of each month, 09:00 UTC). Single placeholder step
  that prints a presence-only diagnostic for the secrets. Phase 5 replaces
  the body with the real load → resolve → enrich → export pipeline.
- Wrote a real pytest suite at `tests/test_geocoder.py`,
  `tests/test_facility_types_yaml.py`, and `tests/test_source_seed.py` so
  CI has 43 tests guarding the geocoder state-consistency policy
  (A1.6), the controlled vocabulary structure + regex compile (A3.x), and
  the source seed cardinality + locked decisions (A2.x). `tests/conftest.py`
  adds the project root to `sys.path` so imports work.
- Cleaned up `scrapers/sample_pulls.py` (removed 4 unused imports flagged
  by ruff). Ran `ruff format` across the project; 3 files reformatted.
- Local dry run before commit: `ruff check .` PASS, `ruff format --check .`
  PASS, `pytest -ra` 43/43 PASS.

**In progress**
- **Task 10** — staging files, committing as
  `Phase 1 Day 1: project skeleton, schema migration v0.1, source seed
  (12 rows), controlled vocab, geocoder stub, CI workflows. Checkpoint 2
  v2 approved.`, tagging `phase-1-day-1`, pushing to `main`. Will watch
  the CI run and report SHA + tag + CI status back.

**Blocked / pending**
- Ryan to approve the eventual Supabase migration apply as a separate
  authorized action after the push lands cleanly.
- Anthropic API key still PENDING_RYAN_PROVISION (Phase 4 blocker only).

**Decisions made**
- Ran `ruff format` across the repo before commit so CI's
  `ruff format --check .` step doesn't trip on opinionated formatting on
  the first push. Edits were mechanical (line-wrapping in
  `sample_pulls.py` and a few test files).
- Added `tests/conftest.py` to inject the project root onto `sys.path`.
  This is the minimal way to support `from orchestration.geocoder import
  ...` from test files without a `pyproject.toml`-level `pythonpath`
  edit. Future scrapers and loaders can import from any package via the
  same path.

**Deviations from the brief**
- None.

---

## 2026-05-11 — Day 1 (post-task-3 checkpoint: tasks 4–8)

**Completed**
- **Supabase CLI v2.98.2** installed to `%USERPROFILE%\bin` (not winget; no
  upstream winget package — direct GitHub release binary). `%USERPROFILE%\bin`
  added to user PATH.
- **`gh auth login`** completed via web flow, authenticated as
  `AxiomInsightsADMIN` (scopes: `gist, read:org, repo`; protocol HTTPS).
- **Task 4** — `git init -b main` in the project root; added `origin` remote
  to `https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners.git`;
  fetched and pointed local `main` at `origin/main` (commit `b1100ab`) using
  `git update-ref` + `git reset` so working-tree files were preserved.
  Remote `main` had stub `.gitignore` and `README.md`; my versions strictly
  supersede both. Added `.claude/` to `.gitignore`.
- **Task 5** — schema v0.1 written at `db/migrations/0001_initial.sql`. 14
  tables: `source`, `scraper_run`, `source_signature`, `raw_facility_record`,
  `canonical_facility`, `facility_record_link`, `field_provenance`,
  `canonical_facility_history`, `facility_type_lookup`, `geocoding_cache`,
  `llm_enrichment_cache`, `discovered_url`, `discovery_candidate_facility`,
  `discovery_review_queue`. UUID-keyed `canonical_facility`. Constraints
  enforce the locked architectural decisions (three-tier identity, separate
  provenance table, controlled-vocabulary acceptance flags, USPS state code,
  lat/long bounds). **Not yet applied** to Supabase.
- **Task 6** — `config/facility_types.yaml` written. Seven canonical types
  with synonyms, regex rules, deny-lists. YAML parses; all regex rules
  compile. **Not yet consumed** by any loader.
- **Task 7** — `db/migrations/0002_source_seed.sql` written. 11 source rows
  (the brief said "eight" but enumerated 11 — I included all enumerated).
  Idempotent via `ON CONFLICT (slug) DO UPDATE`. ToS posture and robots.txt
  status populated from Day-1 audit; the audit revealed that
  `www15.tceq.texas.gov/robots.txt` returns a full `Disallow: /`, so the
  `tceq_central_registry` row records `robots_txt_status='disallow'` and
  the notes route the loader to TCEQ Public Data Lookup instead. **Not yet
  applied** to Supabase.
- **Task 8** — recon script at `scrapers/sample_pulls.py` issued one
  read-only HTTP request per source. Findings:
  - **EPA ECHO** — two-step REST flow works (`get_facilities` → QID,
    `get_download` → CSV). 72,499 TX and 19,827 NC active CWA facilities.
    Sample CSVs trimmed to 100 rows each, saved to `local/source_samples/`
    (gitignored). Schema confirmed.
  - **EPA CWNS 2022** — entrypoint page describes downloadable CSV + Access
    DB via the 2022 CWNS Data Dashboard, which redirects to an Oracle APEX
    app at `sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub`. No direct CSV URL
    on the entrypoint; Phase 1 CWNS loader will need Playwright or APEX
    inspection.
  - **TCEQ CRPUB** — robots-disallowed; declined per locked decision 8.12.
  - **TCEQ Public Data Lookup** — 6 relevant lookup-path entries including
    waste management records and stormwater/wastewater status downloads.
  - **TCEQ Domestic Wastewater landing** — 14 relevant program links.
  - **NC DEQ DWR permits page** — 15 relevant program links (NPDES,
    non-discharge, industrial, municipal permitting).
  - **NC DEQ DWM solid waste landing** — 4 relevant; composting linked
    directly, transfer stations live deeper.
  - Audit doc at `docs/source_audit_phase0.md` with full per-source
    breakdown and two minor reclassifications to the section-9 mapping
    table (CWNS×POTW: Practical → Partial; ECHO×Land-Application: Partial
    confirmed).

**In progress**
- None. Stopping at the task-8 checkpoint per the brief.

**Blocked / pending**
- Ryan to review tasks 4–8 before any push to `main` (task 9 workflows
  + task 10 first commit).
- Schema and source-seed SQL not yet applied to Supabase — pending Ryan's
  review of the SQL itself.
- Anthropic API key still PENDING_RYAN_PROVISION (still a Phase-4 blocker,
  not a Phase-1/2 blocker).

**Decisions made**
- Supabase CLI installed via direct GitHub release rather than scoop or
  winget. Reasoning: winget has no Supabase package; installing scoop just
  to install supabase is excess scope. The Supabase binary on `%USERPROFILE%\bin`
  is a single ~98 MB file, easy to rotate when a new release ships.
- Section-9 ratings reclassified minimally: CWNS×POTW from Practical to
  Partial; ECHO×Land-Application confirmed at Partial. Documented in the
  audit doc.
- ECHO column codes pulled for the audit omitted state/zip/county codes
  (24/25/26). Recorded as a recon oversight in the audit doc, not a source
  gap — Phase 1 loader will include them.

**Deviations from the brief**
- Brief said "eight candidate sources" in task 7 but enumerated 11 — I
  seeded all 11. Logged for Ryan's review.
- Tasks 5 and 6 were drafted in parallel while `gh auth login` was pending
  Ryan's browser interaction. The git work in task 4 ran immediately after
  auth completed, before any of the SQL/YAML was committed (no commits
  yet; staging order doesn't matter at this stage). Surfacing for
  transparency.

---

## 2026-05-11 — Day 1 (scaffolding)

**Completed**
- Confirmed Python 3.11 install path. Installed Python 3.11.9 via winget (the
  machine had only Python 3.14 by default).
- Created Python 3.11 venv at `.venv` in the project root.
- Pinned and installed all base dependencies into `requirements.txt`
  (major-version pinned only, not frozen, per the brief).
- Installed Playwright Chromium (headless shell).
- Wrote project skeleton: directory tree, `.gitignore`, `README.md`,
  `pyproject.toml` (ruff + pytest configured), `LICENSE` (proprietary
  placeholder), `requirements.txt`, `.env.example`, and the local `.env` with
  the credentials from the brief.
- Confirmed `gh` (2.88.1) and `git` (2.53.0) CLIs available. Supabase CLI is
  not yet installed; deferred to task 4 (post-checkpoint).

**In progress**
- None. Stopping at the task-3 checkpoint per the brief.

**Blocked / pending**
- Ryan to review tasks 1-3 before any work past task 4 begins.
- Anthropic API key still PENDING_RYAN_PROVISION (not a blocker until Phase 4).
- Supabase CLI install pending (needed for `supabase init` / `supabase link`
  at task 4, after Ryan's checkpoint).
- Austin's GitHub username (deferred), CSV delivery preference, May-29
  delivery confirmation.

**Decisions made**
- Installed Python 3.11 via winget rather than pyenv-win or the python.org
  installer. Winget is the cleanest first-party path on Windows 11 and avoids
  shimming complexity for a single-Python-version project. If Ryan wants
  pyenv-win specifically (for multi-version dev work later), we can switch
  before any commits to the venv path land.
- Added `.env.example` alongside `.env`. The example file is committed so the
  secret schema is reviewable; the real `.env` is gitignored.

**Deviations from the brief**
- None.
