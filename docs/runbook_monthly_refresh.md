# Monthly Refresh Runbook

This runbook is for the operator who has to make the monthly refresh
complete cleanly — most readers will hit this doc only when something
has gone wrong, so it's structured for diagnosis first, reference
second.

What runs: `.github/workflows/monthly_refresh.yml` on the 1st of each
month at 09:00 UTC. Scrapes → drift-checks → geocode-backfills →
rebuilds canonical → exports CSV → opens a PR.

Companion docs:

- `docs/access_layer.md` — verification queries after the PR merges
- `docs/runbook_data_quality.md` (doc 7) — investigating row-level issues
- `docs/runbook_key_rotation.md` (doc 6) — secret rotation when a 401 / 403 appears

---

## 1. What success looks like

The green-path narrative:

1. **Cron fires** at 09:00 UTC on the 1st. GitHub Actions runs
   `.github/workflows/monthly_refresh.yml`. The same workflow can be
   triggered ad-hoc via `gh workflow run "Monthly Refresh"` or the
   "Run workflow" button on the Actions tab.

2. **Twenty steps execute in order** (full list in
   `docs/schema.md` → "Migration history" and in the workflow YAML
   itself). The shape:

   ```
   checkout → setup-python → install-requirements → install-playwright
     → scrapers (6) → drift-detector → geocoder-backfill
     → resolver --rebuild --force → CSV export
     → refresh branch push → PR open
   ```

3. **Drift detector returns `overall_status: pass`** — all 6 sources
   either show `first_run_no_prior` (first signature) or
   `compared_vs_prior` with all four checks (HTTP, row-count drop,
   schema_hash, byte-size delta) within thresholds.

4. **Refresh branch appears**: `refresh/YYYY-MM-DD` (e.g.
   `refresh/2026-06-01`) with three force-added files:
   `exports/facilities_primary.csv`,
   `exports/facilities_provenance.csv`, `exports/drift_report.json`.

5. **PR opens against `main`** titled "Monthly refresh YYYY-MM-DD"
   with a body summarizing the run. CI re-runs against the refresh
   branch (lint, format, pytest, schema-migration apply) — green
   within ~3 minutes.

6. **No email alert fires.** The alert step is gated on
   `if: failure() && env.SMTP_HOST != '' && env.ALERT_EMAIL != ''`.

The operator reviews the PR (next section), merges, and the new CSVs
land on `main`. Total wall clock on the green path: ~12–18 minutes.

---

## 2. PR review checklist (before merging)

When the PR appears, work through this list. Total time ~5 minutes.

### ☐ CI status on the refresh branch

Open the PR's Checks tab. Four checks must be green:

- Ruff lint (`.github/workflows/ci.yml` job)
- Ruff format
- Pytest (currently 48 tests)
- Schema migration apply (throwaway Postgres)

If any check fails, **do not merge**. CI failures on a refresh PR
usually indicate a code-side issue that landed between refreshes
(rare; most months CI is identical to last month). Investigate via
`gh run view <run_id> --log-failed`.

### ☐ `exports/drift_report.json` content

Click the file in the PR. At the top:

```json
{
  "overall_status": "pass",       // must be "pass"
  "pause_count": 0,
  "source_count": 6
}
```

If `overall_status` is `"pause"`, the workflow halted **before** the
resolver ran. The CSVs in the PR are from the previous month, not
this month — see the per-source `reason` field for which source
paused and why, and route to §3.2 below.

### ☐ CSV diff sanity check

Open `exports/facilities_primary.csv` on the PR's Files tab. Typical
month-to-month diff:

| Diff shape | What it means |
|---|---|
| 0 lines changed | All scraper rows unchanged + same canonical UUIDs. Possible only on a `--rebuild`-free re-run; the monthly refresh always rebuilds so this is rare. |
| 10–300 lines changed | Normal. New permits added at source side, a few canonicals consolidated, address fields refined. |
| 300–2,000 lines changed | Borderline. Read the drift_report.json `details` blocks and skim the build_log for any recent design pin that would explain. |
| > 2,000 lines changed | Investigate before merging. Likely a real source-side regime change (e.g., NC DEQ migrated their ArcGIS endpoint) or a resolver-side regression. Don't merge — escalate. |

`exports/facilities_provenance.csv` will always change every refresh
because `observed_at` is `NOW()` per row. Don't sanity-check that file
on diff size — only the primary file.

### ☐ Email alert (when configured)

If SMTP secrets are populated and an email landed, the workflow had
at least one failed step. Open the email; the body contains a link to
the workflow run. Click through to the failing step.

If no SMTP secrets are configured yet, you won't get an email. The
GitHub Actions tab is the only failure surface — set up a daily
filter on `actions/runs` for the Monthly Refresh workflow until SMTP
is wired (see `docs/runbook_key_rotation.md` for the SMTP setup).

---

## 3. Failure diagnosis by step

### 3.1. Scraper failures (steps 6–11)

The scraper steps run in this order:

| Step | Scraper | Likely failure modes |
|---|---|---|
| 6 | `scrapers.federal.epa_echo` | Site moved (rare); CSV column drift; transient HTTP timeout |
| 7 | `scrapers.federal.epa_cwns` | Playwright session timeout; APEX `<select>` selector changed; survey iframe URL changed |
| 8 | `scrapers.state.tceq_msw_xls` | TCEQ moved the XLS URL; xlrd choke on a corrupt cell |
| 9 | `scrapers.state.nc_deq_non_discharge` | ArcGIS REST endpoint URL changed; FeatureServer paginated record-cap changed |
| 10 | `scrapers.state.nc_deq_solid_waste` | **edocs WAF block (every run)**. See §5 below for the manual-drop workflow. |
| 11 | `scrapers.state.nc_deq_septage_firm` | Same edocs WAF block as step 10. |

**Steps 10 + 11 always fail in CI** — edocs.deq.nc.gov enforces a
network-layer WAF block and the runner filesystem has no manual XLSX.
Both steps carry `continue-on-error: true` so the workflow proceeds
past them. The freshness gate immediately after step 11
(`orchestration.verify_nc_manual_drop_freshness`) then confirms that
the operator already ran those two scrapers locally within the last
7 days — if not, the workflow halts there with a runbook-pointer
error before the resolver runs. The operator's local-drop procedure
lives in §5 below.

If you see a scraper failure on steps **6, 7, 8, or 9**, that's
unexpected:

1. Open the failing step's log via `gh run view <run_id> --log-failed`.
2. Look for the **stack trace's last line in our code** (skip the
   `requests` / `playwright` library frames).
3. Common causes by exception class:
   - `requests.HTTPError` with 404 → source moved its URL. Check the
     source's home page; update the loader's URL constant; submit a
     hotfix PR.
   - `requests.HTTPError` with 5xx → transient. Re-trigger
     `workflow_dispatch` once. If it fails again, the source has an
     outage — note in `docs/build_log.md` and wait.
   - `playwright.TimeoutError` → CWNS APEX page slow / changed.
     First try `workflow_dispatch` once; if it fails again, the
     selector chain has drifted (rare; happens when EPA touches
     the APEX app). Escalate.
   - `KeyError` / `IndexError` in the parser → schema drift. The
     source added/removed/renamed a column. Read the source's CSV
     header against the loader's expected column set. Update the
     loader's column expectations, ship a hotfix PR, re-trigger.
   - `psycopg2.errors.*` → DB-side issue. Most likely cause: someone
     applied an out-of-band migration that broke an upsert. Check
     `supabase/migrations/`; escalate if a migration is unfamiliar.

### 3.2. Drift detector pauses (step 12)

The detector pauses on any of four trigger conditions (locked
decision 8.7). When `drift_report.json` shows
`overall_status: "pause"`, drill into the per-source `details`:

| Trigger | What the `reason` field looks like | What to check | Real-problem signal | Override path |
|---|---|---|---|---|
| HTTP non-200 | `http_status_<code>` | The latest `source_signature.http_status` is non-200 for that source. Check the source URL by hand — does it 200 in your browser? | If the source returns 404, the URL moved (real). If 5xx, transient. | Re-trigger if transient. Patch the loader URL if moved. |
| Row count drop > 30% | `row_count_drop_<n>_pct` | Compare `details.latest_row_count` vs `details.prior_row_count`. Is the drop real or a per-state-slice artifact? | The federal-loader fix (commit `9a6eb53`) consolidates state slices into one signature, so this should never be the per-state artifact again. A real drop means the source actually shrunk. | Often legitimate — e.g., the source pruned closed facilities. If the drop is explained by an upstream announcement, manually clear: `UPDATE source_signature SET … ; DELETE FROM source_signature WHERE row_count='old_value'` (escalate before doing this; this is mutating operational data). |
| Schema hash mismatch | `schema_hash_mismatch` | `details.latest_schema_hash_prefix` vs `details.prior_schema_hash_prefix`. A real column-set change at the source. | Always a real signal. The source's column shape changed. | DO NOT override. The resolver's normalizer expects the prior column set and will skip-or-misroute rows under the new shape. Update the loader's column expectations first, then accept the new schema hash. |
| Byte size delta > 50% | `byte_size_delta_<n>_pct` | `details.latest_byte_size` vs `details.prior_byte_size`. Often a side-effect of a row count change. | Co-incident with a row count drop/spike — same root cause. | Same as row count drop — clear after confirming legitimacy. |

**Detector exit code is non-zero on any pause**, which fails step 12
and halts the workflow before the resolver runs. This is the locked
design: a drift-paused source MUST NOT feed the resolver, or the
canonical_facility table gets poisoned with a partial / corrupted
snapshot.

To **re-run after fixing**: trigger `workflow_dispatch`. The detector
re-reads the latest signatures (no state to clear).

To **override a known-false pause** (rare; only when you've confirmed
the source is fine but the detector is overreacting): there's
deliberately no `--force-pass` flag — the workaround is to manually
INSERT a synthetic "all clear" signature into `source_signature` so
the next comparison passes. Escalate before doing this.

### 3.3. Resolver issues (step 14)

The resolver step runs `python -m resolver.entity_resolver --rebuild --force`.

| Symptom | Likely cause | Fix |
|---|---|---|
| `--rebuild` fails without `--force` | Step's `run:` line missing `--force`. | Inspect the workflow YAML — should never happen on CI but possible after an edit. |
| FK constraint violation on `field_provenance.canonical_facility_id` | TRUNCATE CASCADE didn't fire. | Run `TRUNCATE canonical_facility CASCADE` manually; re-trigger. |
| `RuntimeError: schema_hash mismatch across states` | A federal scraper run produced different column-set hashes for different states (real cross-state schema drift). | The fix is upstream — update the loader's column expectations, re-load, then re-run resolver. |
| Hold-queue overflow | `hold_review_queue.sql` returns >100K rows. | Not a workflow failure but worth flagging. Phase 4 enrichment is the operational answer; no immediate action needed. |
| Resolver completes but canonical count drops sharply (>10%) | Either real source-side shrinkage (already caught by drift detector) OR a category-map bug demoting rows. | Compare `v_all_in_scope` count to the prior month's. If unexplained, escalate. |

### 3.4. CSV export issues (step 15)

| Symptom | Likely cause | Fix |
|---|---|---|
| `facilities_primary.csv` has 0 data rows | Resolver step failed silently (rare) or `v_all_in_scope` is empty. | Query `SELECT COUNT(*) FROM canonical_facility WHERE facility_type IS NOT NULL` — should be ~2,300+. If 0, the resolver didn't run; check step 14 log. |
| Byte count anomaly (file < 100 KB) | Same as above — empty result. | Same. |
| `UnicodeEncodeError` | A source row contained a non-UTF-8 char that the loader didn't normalize. | Find the offending row via `SELECT * FROM raw_facility_record WHERE raw_payload::text LIKE '%<bad-bytes>%'`. Sanitize in the loader. |
| `psycopg2.errors.UndefinedColumn` | A view referenced a column that was dropped. | Re-apply the access-views migration: `psql … -f supabase/migrations/20260512090000_create_access_views.sql`. |

---

## 4. Email alert interpretation

When the workflow fails and SMTP is configured, the operator gets:

**Subject:** `[Arch Legacy] Monthly refresh FAILED: <YYYY-MM-DD>`

**Body:**
```
The monthly_refresh workflow failed on <YYYY-MM-DD>.

Workflow run: https://github.com/AxiomInsightsADMIN/Arch-Legacy-Partners/actions/runs/<run-id>

Click through for the failing step name and the full log. If the
failure is in an NC scraper step (Solid Waste or Septage Firm), the
most common cause is that no manual XLSX drop is available in CI —
run those scrapers locally and re-trigger this workflow via
workflow_dispatch.
```

**What to do first:**

1. Click the workflow-run URL in the email.
2. Find the red ✕ in the step list — that's the failing step.
3. Match the step name to §3.1 / §3.2 / §3.3 / §3.4 above.

**If the alert doesn't arrive** but the workflow did fail: SMTP isn't
configured. Open `docs/runbook_key_rotation.md` § SMTP to set up the
four secrets (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD).

---

## 5. Manual-drop fallback (NC SW + NC SF)

This procedure runs every month until edocs.deq.nc.gov stops blocking
Playwright at the network layer (no ETA — the block has held since
Phase 2 step 3, the load-bearing operational reality).

### Workflow contract (current)

The cron's two NC scraper steps (10 + 11) carry `continue-on-error: true`
— they always fail in CI because the runner filesystem has no manual
XLSX, but the workflow proceeds past them. Immediately after step 11,
a freshness gate runs:

```
python -m orchestration.verify_nc_manual_drop_freshness
```

That gate queries Supabase for successful `scraper_run` rows on both
NC manual-drop sources within the **last 7 days**. If either source
is missing a recent run, the gate exits non-zero (halting the
workflow before drift detection + resolver) with this exact message:

```
ERROR: Manual drop required for one or both NC sources within the last 7 days.
  Missing successful scraper_run for: <slug>[, <slug>]
  See docs/runbook_monthly_refresh.md section 5 for the operator procedure.
  Re-trigger via workflow_dispatch after the manual drop completes.
```

When you see that message in the workflow log (or in the email alert
body when SMTP is configured), follow the Step-by-step procedure
below to drop the missing XLSX(s) and re-trigger. The gate halts
**before** the resolver runs, so stale NC data never poisons
`canonical_facility`.

### Step-by-step

1. **On your workstation** (not in CI — CI has no manual drops), open
   a real browser and authenticate to NC DEQ if needed. The two
   files don't require login but they require a real-browser session
   with cookies.

2. **Download the two XLSX files** from edocs:

   | Source | edocs URL | Save as |
   |---|---|---|
   | NC SW | <https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132701&dbid=0&repo=WasteManagement> | `nc_deq_solid_waste_YYYY-MM-DD.xlsx` |
   | NC SF | <https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132702&dbid=0&repo=WasteManagement> | `nc_deq_septage_firm_YYYY-MM-DD.xlsx` |

   **VPN note:** if you're not on a US-anchor connection, the edocs
   page may fail with the same WAF block you'd hit from CI. Use a
   US-residential VPN exit if needed. The block is geo-related
   secondarily to the network-layer signature.

3. **Drop the files** into the loader's pickup directory:

   ```
   local/manual_drops/nc_deq_solid_waste/<file>.xlsx
   local/manual_drops/nc_deq_septage_firm/<file>.xlsx
   ```

   The directories are gitignored (per `.gitignore` `local/` rule).
   The loaders' `_newest_manual_drop()` helpers walk these dirs and
   pick the most recently modified XLSX.

4. **Run the two scrapers locally** to upsert into Supabase:

   ```bash
   .venv/Scripts/python.exe -m scrapers.state.nc_deq_solid_waste
   .venv/Scripts/python.exe -m scrapers.state.nc_deq_septage_firm
   ```

   Idempotent: re-running an unchanged file produces 0 inserted, 0
   updated, N unchanged.

5. **Then trigger the monthly cron** via `workflow_dispatch`:

   ```bash
   gh workflow run "Monthly Refresh"
   ```

   The cron's NC SW + NC SF steps will fail in CI again (no manual
   drops in the runner) — that's **expected and absorbed** by their
   `continue-on-error: true` flag. The freshness gate immediately
   afterward will pass because step 4 of this procedure landed
   successful scraper_runs in Supabase within the 7-day window.
   The drift detector + resolver + CSV export then run normally and
   the refresh PR opens.

### Alternative: full local refresh

If the workflow-dispatch path is blocked or you want the cleanest
operational path until edocs unblocks, run the entire pipeline from
your workstation:

```bash
# Scrapers (federal + TX + NC)
.venv/Scripts/python.exe -m scrapers.federal.epa_echo TX NC
.venv/Scripts/python.exe -m scrapers.federal.epa_cwns TX NC
.venv/Scripts/python.exe -m scrapers.state.tceq_msw_xls
.venv/Scripts/python.exe -m scrapers.state.nc_deq_non_discharge
.venv/Scripts/python.exe -m scrapers.state.nc_deq_solid_waste
.venv/Scripts/python.exe -m scrapers.state.nc_deq_septage_firm

# Drift gate
.venv/Scripts/python.exe -m orchestration.drift_detector  # must exit 0

# Geocoder backfill (idempotent against geocoding_cache)
.venv/Scripts/python.exe -m orchestration.geocoder_backfill

# Resolver rebuild
.venv/Scripts/python.exe -m resolver.entity_resolver --rebuild --force

# CSV export
.venv/Scripts/python.exe -m exports.export_csv

# Refresh branch + PR
git checkout -b "refresh/$(date -u +%Y-%m-%d)"
git add -f exports/facilities_primary.csv exports/facilities_provenance.csv exports/drift_report.json
git commit -m "Monthly refresh $(date -u +%Y-%m-%d)"
git push -u origin HEAD
gh pr create --base main --title "Monthly refresh $(date -u +%Y-%m-%d)" --body "Manual refresh — edocs blocked CI path."
```

Wall clock: ~20 minutes (the geocoder backfill is the slowest step,
~14 min if it has new addresses to look up; near-instant if cached).

---

## 6. Escalation path

**Handle in-house (operator):**

- Re-trigger `workflow_dispatch` after a transient failure
- Manual-drop the two NC XLSX files and re-run their scrapers
- Approve / clear a drift pause that you've confirmed is legitimate
- Manually adjudicate rows in `hold_review_queue.sql` (data-quality
  workflow; see `docs/runbook_data_quality.md`)
- Merge the monthly refresh PR once the diff looks clean
- Revoke a credential after a rotation (see
  `docs/runbook_key_rotation.md`)

**Escalate to the build team:**

- Schema migration needed (the source added a column we want to
  capture, or we want a new index)
- Scraper code change needed (the source moved, changed shape, or
  added an auth requirement)
- New source slug needed (adding a state or operator-site source)
- Secret rotation that touches the loader code (Anthropic model
  upgrade, etc.)
- Any change to the workflow YAML beyond
  `continue-on-error: true` toggles
- Resolver behavior change (filter tweaks, threshold tuning,
  proximity tiebreak distance)
- Any case where mutating `source_signature` or
  `canonical_facility_history` directly seems necessary

The escalation contact, key-rotation log, and the API-key-rotation
calendar live in `docs/runbook_key_rotation.md` and the Phase 6
design pins in `docs/build_log.md`.
