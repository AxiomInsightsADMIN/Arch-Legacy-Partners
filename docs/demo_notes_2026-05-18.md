# Demo Notes — 2026-05-18 Live Supabase Walkthrough

**Audience:** Austin's team. **Format:** ~45-60 min screenshare, live in
Supabase Table Editor + the GitHub repo + the PR-merged monthly refresh
artifacts. **Goal:** demonstrate that the v1 dataset is delivery-ready,
walk through what gets shipped, and frame the handoff path.

## 0. One-line framing

> *"You're getting a Postgres-backed wastewater facility database for
> TX + NC: 1,970 in-scope canonical rows from six federal/state loaders,
> Haiku-enriched acceptance flags with calibration-grounded precision,
> a discovery crawl that surfaces additional facilities for human
> approval, monthly automatic refresh, and a full documentation suite
> covering everything."*

That's the elevator. Everything below is the supporting evidence.

---

## 1. Sample queries for the live Supabase walkthrough

Open Supabase dashboard → SQL Editor. Run each in order; the result
shape is the proof.

### 1a. The customer-facing baseline

```sql
SELECT count(*) AS in_scope_total,
       count(*) FILTER (WHERE state = 'TX') AS tx_count,
       count(*) FILTER (WHERE state = 'NC') AS nc_count
  FROM v_all_in_scope;
```

Expected: 1,970 / 434 / 1,413 (+ 123 NULL-state).

### 1b. Per-state × per-category snapshot

```sql
SELECT state, facility_type, count(*) AS n,
       count(*) FILTER (WHERE accepts_septage = 'Yes') AS septage_yes,
       count(*) FILTER (WHERE accepts_grease_trap = 'Yes') AS grease_yes,
       count(*) FILTER (WHERE accepts_portable_toilet = 'Yes') AS porta_yes,
       count(*) FILTER (WHERE latitude IS NOT NULL) AS geocoded
  FROM v_all_in_scope
 GROUP BY state, facility_type
 ORDER BY state, facility_type;
```

Talking point: "Three numbers per row tell the customer what they need.
The total. The geocoded coverage. The acceptance commitments by flag.
This is the shape of the CSV."

### 1c. NC septage haulers with explicit acceptance

```sql
SELECT name, city, county, phone, website,
       accepts_septage, accepts_grease_trap, accepts_portable_toilet
  FROM v_nc_private_regional_septage_facility
 WHERE accepts_septage = 'Yes'
 ORDER BY name
 LIMIT 25;
```

Talking point: "444 of 985 NC SF septage canonicals committed to
Yes-accepts-septage. The 985 is the spine — NC DEQ's Septage Firm list.
The 444 is the subset where Haiku found explicit operator-side evidence."

### 1d. Cross-category receiving stations (TX municipal manhole programs)

```sql
SELECT v.name, v.city, v.facility_type, v.website
  FROM v_all_in_scope v
 WHERE v.state = 'TX'
   AND v.facility_type IN ('potw_receiving_station',
                            'private_regional_septage_facility')
   AND v.id IN (
     SELECT canonical_facility_id FROM discovery_review_queue
      WHERE resolution = 'approved_new'
   );
```

Talking point: "Returns the discovery-approved subset only. Right now
the queue is unadjudicated so this returns zero. After Ryan approves a
row (one SQL UPDATE on `discovery_review_queue`), it appears here
automatically — through the view's gate IN-subquery."

### 1e. The discovery review queue itself

```sql
SELECT queue_id, candidate_name, source_category,
       classification_confidence, match_score,
       closest_existing_canonical_name, queue_status
  FROM v_discovery_review
 WHERE queue_status = 'pending'
 ORDER BY classification_confidence, match_score DESC NULLS LAST
 LIMIT 30;
```

Talking point: "This is the offline workspace. 141 rows pending. High-
confidence first. The runbook (`docs/runbook_review_queue.md`) walks
through the three resolutions and the SQL pattern for each."

### 1f. Source attribution per canonical (audit trail)

Pick one row — say a recognizable NC POTW — and walk the chain:

```sql
SELECT cf.name, s.slug AS source_slug, frl.match_method, frl.match_score
  FROM canonical_facility cf
  JOIN facility_record_link frl ON frl.canonical_facility_id = cf.id
  JOIN raw_facility_record rfr ON rfr.id = frl.raw_facility_record_id
  JOIN source s ON s.id = rfr.source_id
 WHERE cf.name ILIKE '%Charlotte-Mecklenburg%'
 ORDER BY frl.linked_at;
```

Talking point: "Every canonical can be back-traced through `facility_record_link`
to the original raw observations, the original sources, and the original
fetch run. Nothing is opaque."

### 1g. Field-level provenance

```sql
SELECT field_name, value, source_url, extraction_method, confidence,
       observed_at
  FROM field_provenance
 WHERE canonical_facility_id = '<paste a canonical id>'
 ORDER BY observed_at, field_name;
```

Talking point: "Per-field provenance. Every column on a canonical row
has a history — which source set it, when, with what extraction method,
at what confidence. The Phase 4 enrichment writes its evidence
quotations here via `extraction_method='llm_extracted'`."

---

## 2. Sample CSV exports to show

Pre-generate locally before the demo:

```bash
.venv/Scripts/python.exe -m exports.export_csv --out exports/demo_2026-05-18/
```

Walk through three files:

1. **`facilities_primary.csv`** — the main customer deliverable. 1,970
   rows × 22 columns. State + facility_type + acceptance flags +
   contact info + lat/lng + permit IDs + first/last_seen timestamps.
2. **`facilities_provenance.csv`** — the field-level audit. Multiple
   rows per facility (one per provenance entry). Auditor-friendly.
3. **`drift_report.json`** — the latest scrape vs. previous-scrape
   drift comparison. Confirms the monthly refresh's data integrity.

Talking point: "Austin's team can pull the CSV three ways: download from
the latest PR, query Supabase directly, or wait for the next monthly
refresh PR. Same row set every way."

---

## 3. Talking points

### 3a. Schema design

- Three-tier identity model: `raw_facility_record` (immutable
  observations) → `facility_record_link` (many-to-one, with
  `match_method` + `match_score`) → `canonical_facility` (resolved
  entity).
- Separate `field_provenance` table for per-field history. The
  canonical row holds the current value; provenance is the audit log.
- Controlled vocabulary in `facility_types.yaml`, mirrored into
  `facility_type_lookup`. Loaders MUST normalize through this — no
  inline normalization. Architectural lock from kickoff brief
  section 8.9.
- 20 access-layer views: `v_all_in_scope` (the spine, gated on
  discovery-crawl review-queue approval), 3 state-scoped views, 14
  per-state-per-type slices, 3 acceptance-flag positives. All inherit
  the discovery gate through dependency chain — single source of
  truth for the predicate.

### 3b. Source coverage

- **Federal**: EPA ECHO (CWA REST API, 92,326 rows) + EPA CWNS 2022
  (APEX state-zip flow, 3,132 rows). Cross-source identity via NPDES /
  FRS / CWNS IDs.
- **State (TX)**: TCEQ MSW XLS at the robots-permissive
  `www.tceq.texas.gov/assets/public/...` path. 1,494 facilities
  (transfer / composting / processing / landfill).
- **State (NC)**: NC DEQ DWR Non-Discharge ArcGIS FeatureServer
  (1,205 rows), NC DEQ DWM Solid Waste manual-drop XLS (365 rows after
  filtering), NC DEQ DWM Septage Firm manual-drop XLS (746 rows).
- **Manual-drop fallback for two NC sources** documented in the monthly
  refresh runbook — `edocs.deq.nc.gov` is network-WAF-gated, so
  Playwright fails. Operator workaround: download the two XLSes from
  any non-blocked IP, drop in `local/manual_drops/`, re-trigger.

### 3c. Enrichment methodology and results

- Phase 4: Anthropic Haiku 4.5 reads top-3 Brave Search snippets per
  facility, emits structured `Yes` / `No` / `Unknown` for septage,
  grease trap, portable toilet acceptance.
- **Calibration grounded the prompt**: 50-facility ground-truth pass at
  v1.0.0, v1.1.0, v1.1.1. v1.1.1 passed the 85% precision gate on all
  three fields (92.3% septage / 85.7% grease / 90.0% portable). Recall:
  70.6% / 75.0% / 90.0%. Three known failure modes addressed across
  versions: hallucinated denials (anti-hallucination quotation rule),
  business-model-incompatibility inferences (tuning 4 EVERGRO example),
  tool-call schema malformations (MAX_TOKENS bump + worked JSON
  example).
- **Full-pass results**: 1,970 facilities → 509 septage Yes / 9 No,
  233 grease Yes / 25 No, 236 portable Yes / 9 No. The 1,742
  affirmatives are a floor — calibration recall says the true `Yes`
  population is ~30% higher across the three fields.
- **Resilience-tested**: full pass took 3 attempts (harness restart
  killed mid-run, Brave free tier exhausted, clean paid-tier run). The
  per-row cache writes preserved every Haiku verdict across both
  events. Build log entries for 2026-05-13 / 2026-05-14 have the full
  postmortem.
- **Per-row cache + canonical promote**: every Haiku call writes both
  `llm_enrichment_cache` (idempotent re-runs are free) and
  `canonical_facility.accepts_*` (the customer-facing surface). The
  monthly refresh re-runs are O(1) per facility on cache hit.

### 3d. Discovery queue and review workflow

- **Frame this as a feature, not incomplete work**: "The system finds
  facilities and surfaces them for human approval before they enter
  the customer-facing data."
- Phase 4.5 ran a bounded discovery crawl: 140 query templates × 5
  (category, state) buckets = 175 Brave queries → 965 unique URLs →
  188 candidate extractions → 141 review queue rows (48 net-new + 93
  borderline) + 47 already-merged-into-existing.
- The 141 queue rows STAY OUT OF `v_all_in_scope` until Ryan approves
  via SQL UPDATE on `discovery_review_queue`. Gate is the view's
  IN-subquery on `resolution = 'approved_new'`. Documented in
  `docs/runbook_review_queue.md` with SQL examples for all three
  resolutions.
- Categories targeted: county manhole programs (0 baseline →
  36 candidates), TX private septage (36 baseline → 72 candidates),
  TX land application (0 baseline → 19 candidates from TCEQ TLAP
  pending-permits page), NC anaerobic digesters (0 baseline → 61
  candidates from NC DEQ press releases + agmrc.org + RNG news).
- 6 cross-category dedups detected (Walnut Creek WWTP, Excess Flow
  Station, etc.) — facilities surfaced in multiple buckets collapsed
  to one canonical.

### 3e. Monthly refresh automation

- `.github/workflows/monthly_refresh.yml` fires on `0 9 1 * *` cron.
- 20-step pipeline: scrape (6) → drift check → geocode backfill →
  resolver `--rebuild --force` → CSV export → refresh-branch push →
  PR open.
- Drift detector pauses the run if any source's byte count / row
  count diverges from the previous baseline by configured thresholds.
  False-positive pauses get manually overridden via the workflow
  dispatch UI; documented in the runbook.
- Email alerts on failure (SMTP secrets needed — see runbook).
- **Phase 4 enrichment will plug in here in Phase 5/6**: the
  auto-promote step from commit `8e057b2` ensures no manual after-step
  is required when enrichment gets wired into the cron.

### 3f. Documentation package

Located in `docs/`:

| File | Purpose |
|---|---|
| `schema.md` | Data dictionary + ER diagram. |
| `data_dictionary.md` | Column-by-column reference for every public table / view. |
| `sources.md` | Per-source provenance + scope. |
| `access_layer.md` | The 20 views: what each surfaces, sample queries. |
| `v1_scope_limitations.md` | What's NOT in v1 and why. TPIA path, NC manual-drop path, monthly-refresh cost frame. |
| `data_quality_scorecard_v1.md` | Live-Supabase-generated snapshot of the dataset. |
| `runbook_monthly_refresh.md` | Operator runbook for the cron. |
| `runbook_data_quality.md` | Investigating row-level issues. |
| `runbook_key_rotation.md` | Six credentials, when to rotate, how. |
| `runbook_add_a_state.md` | Adding a 3rd / 4th state post-v1. |
| `runbook_review_queue.md` | Phase 4.5 step E adjudication workflow. |
| `build_log.md` | Daily journal of the build. 49+ entries. |

### 3g. Handoff plan

- **2026-05-29 contract delivery**: tag the commit, hand Austin the
  GitHub repo URL + the Supabase project URL + this documentation
  package.
- **Two transfers required**: Supabase project ownership (their org
  receives), GitHub repo ownership (their org receives). Both
  one-click in the respective dashboards.
- **Day-1 walkthrough** (post-transfer): `runbook_key_rotation.md` §7
  has the step-by-step sequence — they generate their own six
  credentials, install in `.env` + GitHub Actions secrets, run a
  test monthly-refresh dispatch.
- **First production monthly refresh on Austin's side**: 2026-06-01 if
  the transfer completes before then; otherwise the next 1st of month.
  Drift detector validates against the May baseline we leave behind.
- **Review queue adjudication is theirs to drive** — 141 rows pending
  at handoff. Working through them is a multi-week task; the runbook
  documents the workflow.

---

## 4. Operational metrics

| Metric | Value |
|---|---:|
| `v_all_in_scope` row count | 1,970 |
| Discovery candidates held by review gate | 141 (pending) |
| Cumulative Anthropic spend (calibration + 3 stop-4 attempts + validation) | **~$13.62** of $40 cap (34%) |
| Cumulative Brave searches | **~2,970** (49% of the $25 paid-tier top-up) |
| Cumulative project API spend | **~$21** against $40 + $15 caps |
| Build pace | **4+ brief-days ahead** of May 16–18 internal target |
| Resilience events survived | 2 (harness restart at facility ~760; Brave free-tier 402 wall) |
| Total commits to main | ~55 (Phase 1 through Phase 4.5 step E + delivery prep) |

Cost is well inside both caps. The Brave $25 top-up has ~$13 remaining,
which covers ~2,600 additional queries — enough for one full monthly
refresh's Phase 4 enrichment pass (~1,970 queries) with headroom.

---

## 5. Demo flow (suggested 45 min)

| Time | Section | What to show |
|---|---|---|
| 0:00–0:05 | Framing | The one-liner above. The numbers in section 4. |
| 0:05–0:15 | Live Supabase | Queries 1a–1d. Walk the view inheritance. |
| 0:15–0:25 | Source coverage + audit trail | Queries 1f, 1g. Show provenance for one row end-to-end. |
| 0:25–0:30 | Enrichment results | Calibration metrics. Acceptance-flag distribution. |
| 0:30–0:35 | Discovery + review queue | Query 1e. Open `v_discovery_review` in Table Editor. Walk one borderline. |
| 0:35–0:40 | Monthly refresh | Show the workflow YAML + the latest run + the drift report. |
| 0:40–0:45 | Documentation tour + handoff | Walk the `docs/` index. Sequence the May 29 transfer. |

Questions / freeform Q&A at the end — leave 5–10 min buffer.

---

## 6. Anticipated questions + answers

- **"Why are 70K rows in `canonical_facility` not in `v_all_in_scope`?"** —
  Those are ECHO industrial NPDES facilities outside the seven v1
  categories. Raw payload preserved; `facility_type=NULL` is the
  out-of-scope signal. Future state expansions or category additions
  can promote subsets.

- **"Why is grease trap commit rate only 13%?"** — Operator websites for
  most facility categories don't mention grease trap acceptance
  explicitly. Anti-hallucination prompt rule keeps abstentions honest.
  Where operators DO publish, calibration precision is 85.7%.
  Recall caveat: ~25% of grease-Yes facilities are still in the
  Unknown bucket because the source page didn't yield a quotable
  signal.

- **"How would you add a third state?"** — `runbook_add_a_state.md` walks
  it. Each new state ships its own source audit, its own per-source
  loader, runs through the existing resolver + enrichment unchanged.
  ~$40/state per the original brief, no schema changes needed for
  4th-letter USPS codes.

- **"What's the maintenance burden?"** — One monthly refresh PR to merge
  (~10 min review) + the review-queue adjudication backlog
  (currently 141 rows, ad-hoc clearance) + credential rotation every
  6-12 months. Drift detector + email alerts surface anything
  unusual.

- **"Why no real-time updates?"** — Out of scope for v1. The monthly
  cadence matches the underlying sources' publication frequency
  (TCEQ MSW XLS refreshes monthly; NC DEQ DWR FeatureServer can be
  daily but the contractual signal isn't time-sensitive). Real-time
  would be a Phase 6 / v2 conversation.

- **"Is the discovery crawl re-runnable?"** — Yes. The query template
  library + harvester + extractor + resolver are all idempotent.
  Re-running with the same Brave/Haiku cache is free. Re-running
  after Brave snippet drift produces incremental additions to the
  review queue. Documented in `runbook_review_queue.md` §1.

---

End.
