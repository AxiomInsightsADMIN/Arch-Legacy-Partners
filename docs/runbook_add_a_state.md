# Runbook: Adding a New State Post-Delivery

This runbook is the procedure that operationally justifies the
$40-per-state fee in the kickoff brief. It is written for **Austin or
someone he hires** to follow end-to-end without contacting Axiom
Insights. Each step is concrete: file paths, code snippets,
verification checks.

**Time estimate**: 2–4 hours per state, dominated by the source audit
(step 2) and the per-source loader builds (step 6). Lower bound for
states whose agency data shapes are similar to TCEQ/NC DEQ; upper
bound for states that publish in unusual formats (e.g. PDF-only
permit lists, no API).

**Skill estimate**: comfortable with Python 3.11, SQL, and reading
Postgres migrations. No Playwright knowledge needed unless the new
state agency hosts data behind an APEX app or similar JS-driven
download (rare).

---

## Overview — the additions per state

Concretely, adding a state means:

1. **Geocoder envelope** for the new state (one entry in `STATE_BOUNDS`)
2. **Source-table seed migration** registering the new state's data sources
3. **Filter rule update** widening `V1_STATES` in `resolver/_filters.py`
4. **Per-source loaders** in `scrapers/state/`
5. **Resolver normalizer** entries in `resolver/_normalize.py`
6. **Category-map overrides** for the state's classifier codes
7. **Access-view migration** adding 7 per-state-per-type views
8. **Workflow update** wiring the new scraper steps into the monthly cron
9. **Documentation updates** in `docs/data_dictionary.md`, `docs/sources.md`,
   and a new `docs/state_audits/<XX>_audit.md`
10. **First load + verification** confirming the new state appears
    cleanly in `canonical_facility`

ECHO and CWNS are federal sources — they auto-expand to the new
state via the `V1_STATES` filter widening; no per-state code change
needed for those two loaders.

---

## Step 1: Pick the state and gather basics

Decide which state to add. The locked decision is one state per $40
fee — confirm scope before starting work.

Capture for your audit notes:

- **USPS 2-letter code** (e.g. `FL`, `CA`)
- **State environmental / health agency** (the equivalent of TCEQ for
  Texas, NC DEQ for North Carolina)
- **Geocoder envelope** — go to <https://en.wikipedia.org/wiki/<State_Name>>
  and find the lat/lng bounding box. Round outward by ~0.3° to
  include offshore facilities. For example:
  - TX: `(25.5, 36.7, -106.8, -93.4)`
  - NC: `(33.7, 36.7, -84.5, -75.3)`
  - FL would be approximately `(24.4, 31.1, -87.7, -79.9)`

---

## Step 2: State agency source audit

Open a new file `docs/state_audits/<XX>_audit.md` (lower-case 2-letter
state code in the filename). Follow the template established by
`docs/tceq_pdl_audit.md` and `docs/nc_deq_audit.md`. For each
candidate URL identified, record:

| Field | What to capture |
|---|---|
| Source name | Human-readable name (e.g. "Florida DEP Domestic Wastewater Permits") |
| URL | Full URL of the data page |
| Data format | XLSX / XLS / CSV / JSON / ArcGIS REST / HTML / PDF |
| Refresh cadence | Daily / weekly / monthly / annual / on-demand / unknown |
| robots.txt status | `allow` / `disallow` / `none`. Check `<host>/robots.txt`. |
| ToS posture | `permissive` / `restrictive` / `unknown`. Link the ToS or closest-equivalent policy doc. |
| Audit notes | 1–2 paragraphs on coverage, gaps, and any quirks |

**Checklist per candidate source:**

- [ ] robots.txt fetched and inspected (`https://<host>/robots.txt`)
- [ ] If `Disallow: /` on the path you'd scrape → DECLINE the source
  (locked decision 8.12). Find an alternative download path.
- [ ] ToS / Public Domain / Linking Policy reviewed; URL recorded
- [ ] Sample download attempted in a browser; format confirmed
- [ ] Approximate row count or facility count noted
- [ ] Stable identifier column identified (per-source `source_record_id`)

**v1 coverage targets per facility category** — the new state should
ideally surface data for as many of these as possible:

1. POTW receiving station (often in the state's domestic wastewater
   permit registry, but watch the A3.3 receiving-station-specific
   framing in `docs/data_dictionary.md` §3)
2. County manhole program (usually no state-level registry; defer to
   Phase 4.5 discovery crawl)
3. Land application site (often in state's biosolids or 503 program
   registry)
4. Private / regional septage facility (sometimes in state's septage
   hauler registry; sometimes in solid-waste registry)
5. Composting facility (state DEQ / DEP solid waste division)
6. Anaerobic digester (rarely a public roster; often a v1 gap per state)
7. Transfer station (state solid-waste division)

Coverage gaps are acceptable — log them in
`docs/v1_scope_limitations.md` for the new state.

---

## Step 3: Update the geocoder envelope

Edit `orchestration/geocoder.py`. Add the new state's bounding box to
the `STATE_BOUNDS` dict:

```python
STATE_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    # state: (min_lat, max_lat, min_lng, max_lng)
    "TX": (25.5, 36.7, -106.8, -93.4),
    "NC": (33.7, 36.7, -84.5, -75.3),
    "FL": (24.4, 31.1, -87.7, -79.9),  # add the new state here
}
```

Add a unit test in `tests/test_geocoder.py` covering the envelope
shape and a known-inside city anchor:

```python
def test_fl_envelope_shape(self):
    lo_lat, hi_lat, lo_lng, hi_lng = STATE_BOUNDS["FL"]
    assert lo_lat < hi_lat
    assert lo_lng < hi_lng
    assert lo_lat >= 24.0 and hi_lat <= 32.0
    assert lo_lng >= -88.0 and hi_lng <= -79.0

# Add to TestCoordsConsistentWithState.test_inside parametrize:
(25.7617, -80.1918, "FL"),  # Miami
```

Run the test suite locally to confirm:

```bash
.venv/Scripts/python.exe -m pytest tests/test_geocoder.py -v
```

---

## Step 4: Widen the v1 state-coverage filter

Edit `resolver/_filters.py`. The `V1_STATES` frozenset gates the
ECHO CWPState filter and any cross-state-coverage logic:

```python
V1_STATES: frozenset[str] = frozenset({"TX", "NC", "FL"})
```

That single line widening lets ECHO and CWNS auto-expand: when the
next monthly cron runs and the federal scrapers pull data for FL,
those rows survive the CWPState filter and feed the resolver.

Update `docs/build_log.md` → "Phase 3 prep → State-coverage filter"
to reflect the widened coverage set.

---

## Step 5: Source registry seed migration

Create a new migration file. Use a timestamp prefix that sorts after
all existing migrations:

```
supabase/migrations/<YYYYMMDDHHMMSS>_<state>_subsource_seed.sql
```

For example: `20260901090000_fl_subsource_seed.sql`.

Template (mirror `20260511230000_nc_deq_subsource_seed.sql`):

```sql
-- =============================================================================
-- <YYYYMMDDHHMMSS>_<state>_subsource_seed.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- <STATE> source registry seed. Authored <YYYY-MM-DD> by <operator>.
--
-- Idempotent: ON CONFLICT (slug) DO UPDATE so re-running is safe.
-- =============================================================================

BEGIN;

INSERT INTO source (
    slug, name, type, base_url, tos_url, tos_posture,
    robots_txt_status, notes, last_checked_at
) VALUES
(
    '<state>_<source_slug_1>',
    '<Human-readable name 1>',
    'state',
    'https://<source-url-1>',
    'https://<tos-url>',
    'permissive',
    'allow',
    '<one-paragraph audit summary referencing docs/state_audits/<XX>_audit.md>',
    NOW()
),
(
    '<state>_<source_slug_2>',
    '<Human-readable name 2>',
    ...
)
ON CONFLICT (slug) DO UPDATE SET
    name              = EXCLUDED.name,
    type              = EXCLUDED.type,
    base_url          = EXCLUDED.base_url,
    tos_url           = EXCLUDED.tos_url,
    tos_posture       = EXCLUDED.tos_posture,
    robots_txt_status = EXCLUDED.robots_txt_status,
    notes             = EXCLUDED.notes,
    last_checked_at   = EXCLUDED.last_checked_at;

COMMIT;
```

Apply via psycopg2 (the project's established pattern — direct
`supabase db push` is also supported):

```bash
.venv/Scripts/python.exe -c "
import os, psycopg2
from pathlib import Path
root = Path.cwd()
for line in (root / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())
conn = psycopg2.connect(
    host=os.environ['SUPABASE_DB_HOST'], port=int(os.environ['SUPABASE_DB_PORT']),
    user=os.environ['SUPABASE_DB_USER'], password=os.environ['SUPABASE_DB_PASSWORD'],
    dbname=os.environ['SUPABASE_DB_NAME'], sslmode='require',
)
cur = conn.cursor()
cur.execute(open('supabase/migrations/<YYYYMMDDHHMMSS>_<state>_subsource_seed.sql').read())
conn.commit()
print('Applied.')
"
```

Verify the new slugs are in the `source` table:

```sql
SELECT slug, type FROM source WHERE slug LIKE '<state>_%' ORDER BY slug;
```

Update `tests/test_source_seed.py` if it has a hard-coded expected
slug count (CI assertion). Increment by the number of new slugs you
just inserted.

---

## Step 6: Per-source loaders

For each new state source (typically 1–3 loaders per state — solid
waste, septage, biosolids), create
`scrapers/state/<state>_<source>.py`. Use the existing loaders as
templates:

| Existing template | Use when the new source is… |
|---|---|
| `scrapers/state/tceq_msw_xls.py` | An HTTP-fetched XLS/XLSX file at a stable URL |
| `scrapers/state/nc_deq_non_discharge.py` | An ArcGIS FeatureServer REST endpoint |
| `scrapers/state/nc_deq_solid_waste.py` | Behind a WAF / manual-drop required |
| `scrapers/federal/epa_echo.py` | A REST API that returns CSV |
| `scrapers/federal/epa_cwns.py` | An APEX or JS-driven download |

Each loader must:

1. **Use `scrapers/_loader_utils.py`** for `db_connect()`,
   `begin_run()`, `finish_run()`, `write_signature()`, `hash_payload()`.
   Don't reinvent the operational plumbing.

2. **Upsert via the standard SQL pattern** — `ON CONFLICT
   (source_id, source_record_id) DO UPDATE WHERE payload_hash <>
   EXCLUDED.payload_hash`. This is what makes re-runs idempotent
   (0 inserted, 0 updated, N unchanged on unchanged source data).

3. **Emit one consolidated `source_signature` per logical refresh**
   — if your loader iterates multiple slices (e.g. one query per
   waste type), aggregate stats in memory and write ONE signature at
   the end of the run. See `scrapers/federal/epa_echo.py:run_all()`
   for the canonical pattern. Federal-loader Phase 5 follow-on
   commit `9a6eb53` documents the rationale.

4. **Pick a stable `source_record_id`** from the source data. Common
   patterns: permit number, facility ID, FRS_ID. Document the choice
   in the docstring.

5. **Add an `if __name__ == "__main__": main(...)`** entry point so
   the loader is runnable as `python -m scrapers.state.<module>`.

Run the new loader locally to do the first load:

```bash
.venv/Scripts/python.exe -m scrapers.state.<state>_<source>
```

Expected output: N inserted, 0 updated, 0 unchanged on first run; on
second run, 0 inserted, 0 updated, N unchanged.

---

## Step 7: Extend the resolver normalizer

Edit `resolver/_normalize.py`. Add a `_normalize_<slug>()` function
for each new source. The function takes `(raw_id, source_record_id,
raw_payload)` and returns a `NormalizedRaw` dataclass:

```python
def _normalize_fl_dep_facilities(
    raw_id: int, source_record_id: str, p: dict
) -> NormalizedRaw:
    return NormalizedRaw(
        raw_id=raw_id,
        source_slug="fl_dep_facilities",
        source_record_id=source_record_id,
        name=_clean(p.get("FacilityName")),
        street=_clean(p.get("StreetAddress")),
        city=_clean(p.get("City")),
        state=_upper2(p.get("State")) or "FL",
        zip=_normalize_zip(p.get("ZipCode")),
        county=_clean(p.get("County")),
        latitude=_coord(p.get("Latitude")),
        longitude=_coord(p.get("Longitude")),
        # Add state-specific identifier(s) here. Use new NormalizedRaw
        # fields if needed (e.g. fl_permit_number) and update IdRegistry
        # in resolver/_id_match.py.
        raw_facility_type_string=_clean(p.get("FacilityType")),
        raw_payload=p,
    )
```

Register in the `NORMALIZERS` dict at the bottom of the file:

```python
NORMALIZERS: dict[str, callable] = {
    "epa_echo": _normalize_epa_echo,
    "epa_cwns_2022": _normalize_epa_cwns,
    "tceq_msw_facilities_xls": _normalize_tceq_msw,
    "nc_deq_non_discharge_facilities": _normalize_nc_deq_non_discharge,
    "nc_deq_solid_waste_facility_list": _normalize_nc_deq_solid_waste,
    "nc_deq_septage_firm_list": _normalize_nc_deq_septage_firm,
    "fl_dep_facilities": _normalize_fl_dep_facilities,   # new
}
```

If you added a state-specific identifier (e.g. `fl_permit_number`),
add it to:

- `NormalizedRaw` dataclass fields (`resolver/_normalize.py`)
- `IdRegistry` dataclass fields and `_bucket()` method (`resolver/_id_match.py`)
- `ID_PRECEDENCE` tuple in `resolver/_id_match.py` (insert in
  appropriate position — federal cross-source IDs first, state IDs
  after)

---

## Step 8: Add category-map overrides if needed

Edit `resolver/_category_map.py`. If the new state's source uses
state-specific classifier codes (analogous to TCEQ `Physical Type`
or NC SW `Activity`), add a `<STATE>_<COLUMN>_OVERRIDES` dict and
wire it into `_source_override()`:

```python
FL_FACILITY_TYPE_OVERRIDES: dict[str, str] = {
    "POTW": "potw_receiving_station",
    "BiosolidLandApp": "land_application_site",
    "MaterialRecovery": "transfer_station",
    # ...
}

def _source_override(source_slug: str, raw_type: str) -> CategoryMatch | None:
    # ...
    if source_slug == "fl_dep_facilities" and raw_type in FL_FACILITY_TYPE_OVERRIDES:
        return CategoryMatch(
            FL_FACILITY_TYPE_OVERRIDES[raw_type], "medium", "source_override"
        )
```

For sources that produce free-form type strings, the YAML lookup in
`config/facility_types.yaml` will handle them via regex/synonym match
— no per-source override needed.

---

## Step 9: Access-view migration

Create `supabase/migrations/<YYYYMMDDHHMMSS>_create_<state>_views.sql`.
For each of the 7 facility_type slugs, add a per-state view following
the pattern from `20260512090000_create_access_views.sql`:

```sql
BEGIN;

CREATE OR REPLACE VIEW v_<state>_potw_receiving_station AS
  SELECT * FROM v_all_in_scope
   WHERE state = '<STATE_UPPER>' AND facility_type = 'potw_receiving_station';
COMMENT ON VIEW v_<state>_potw_receiving_station IS
  '<STATE_UPPER> POTW Receiving Stations. <one-line note>';

-- ... 6 more views, one per canonical facility_type slug

CREATE OR REPLACE VIEW v_<state>_in_scope AS
  SELECT * FROM v_all_in_scope WHERE state = '<STATE_UPPER>';
COMMENT ON VIEW v_<state>_in_scope IS
  '<STATE_UPPER> in-scope canonical facilities. v_all_in_scope filtered to state=<STATE_UPPER>.';

COMMIT;
```

Apply via the same psycopg2 pattern as step 5.

Verify the 8 new views exist:

```sql
SELECT viewname FROM pg_views
 WHERE schemaname='public' AND viewname LIKE 'v_<state>_%'
 ORDER BY viewname;
```

---

## Step 10: Workflow update

Edit `.github/workflows/monthly_refresh.yml`. Add one step per new
loader, inserted in the "State scrapers" section between the
existing TCEQ + NC steps and the manual-drop gate:

```yaml
      - name: Scraper — <Florida DEP Domestic Wastewater> (FL)
        run: python -m scrapers.state.fl_dep_facilities
```

If any new state source requires a manual-drop fallback (WAF-blocked
or login-walled):

1. Add `continue-on-error: true` to that step
2. Extend `orchestration/verify_nc_manual_drop_freshness.py` to a
   more general gate (rename to `verify_manual_drop_freshness.py`,
   take the slug list from an env var or a config file). Or
   write a parallel `verify_<state>_manual_drop_freshness.py` and
   add a workflow step calling it.

---

## Step 11: Run the full pipeline

Execute the monthly refresh pipeline locally to land the first batch
of canonical data for the new state:

```bash
# Federal scrapers re-pull with the widened V1_STATES filter; new state
# rows now flow through.
.venv/Scripts/python.exe -m scrapers.federal.epa_echo TX NC <STATE>
.venv/Scripts/python.exe -m scrapers.federal.epa_cwns TX NC <STATE>

# State-specific loaders for the new state
.venv/Scripts/python.exe -m scrapers.state.<state>_<source_1>
.venv/Scripts/python.exe -m scrapers.state.<state>_<source_2>

# Drift detector (will show first_run_no_prior for the new sources)
.venv/Scripts/python.exe -m orchestration.drift_detector

# Geocoder backfill if any new sources need it
.venv/Scripts/python.exe -m orchestration.geocoder_backfill --sources <state>_<source_with_no_coords>

# Resolver full rebuild — picks up the widened state coverage
.venv/Scripts/python.exe -m resolver.entity_resolver --rebuild --force

# CSV export
.venv/Scripts/python.exe -m exports.export_csv
```

---

## Step 12: Verification checklist

After the first load, verify:

```sql
-- Raw row count by source (new sources should appear)
SELECT s.slug, COUNT(*)
  FROM raw_facility_record r JOIN source s ON s.id = r.source_id
 GROUP BY s.slug ORDER BY s.slug;

-- Canonical facility count by state (new state should appear with a count)
SELECT state, COUNT(*)
  FROM canonical_facility
 WHERE facility_type IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC;

-- Per-state-per-type breakdown for the new state
SELECT facility_type, COUNT(*)
  FROM v_<state>_in_scope
 GROUP BY 1 ORDER BY 2 DESC;

-- Verify the new state's NPDES rows came from ECHO
SELECT COUNT(*)
  FROM raw_facility_record r JOIN source s ON s.id = r.source_id
 WHERE s.slug = 'epa_echo'
   AND r.raw_payload->>'CWPState' = '<STATE_UPPER>';

-- Verify drift detector passes for the new sources
.venv/Scripts/python.exe -m orchestration.drift_detector
```

Expected outcomes:

- [ ] `raw_facility_record` shows the new state's sources with non-zero counts
- [ ] `canonical_facility` count by state shows the new state with a reasonable count (compare to the source row counts — typically 1.2–1.5× collapse via ID-first + score-based matching)
- [ ] `v_<state>_in_scope` returns rows
- [ ] At least one `v_<state>_<facility_type>` view returns rows (otherwise the YAML or source overrides didn't catch the new state's classifier strings — debug `resolver/_category_map.py`)
- [ ] Drift detector shows `pass` for all sources including the new ones (`first_run_no_prior` for first-time loaders)

---

## Step 13: Update the docs

Touch four docs:

1. **`docs/data_dictionary.md`** §4 — add the new source slugs to the table
2. **`docs/data_dictionary.md`** §5 — extend the `facility_type → source mapping` table with the new state's loaders for each category they feed
3. **`docs/sources.md`** — add a new "<STATE> state sources" section with per-source detail (use existing TX / NC sections as templates)
4. **`docs/state_audits/<XX>_audit.md`** — finalize from step 2

Optionally update `docs/v1_scope_limitations.md` if the new state has
scope concessions (e.g. one of its agencies is robots-disallowed).

---

## Step 14: Commit + ship

Single commit recommended:

```bash
git add \
  orchestration/geocoder.py tests/test_geocoder.py \
  resolver/_filters.py \
  resolver/_normalize.py resolver/_id_match.py resolver/_category_map.py \
  scrapers/state/<state>_*.py \
  supabase/migrations/<YYYYMMDDHHMMSS>_<state>_subsource_seed.sql \
  supabase/migrations/<YYYYMMDDHHMMSS>_create_<state>_views.sql \
  .github/workflows/monthly_refresh.yml \
  docs/data_dictionary.md docs/sources.md \
  docs/state_audits/<XX>_audit.md \
  tests/test_source_seed.py

git commit -m "Add <STATE> state coverage: <N> new sources, <N> new views"
git push origin main
```

Confirm CI passes on the commit. The monthly cron picks up the new
state automatically on the next 1st-of-month firing.

---

## Cost / time / fee justification ($40 per state)

| Phase | Estimated time | Skill |
|---|---|---|
| Source audit (step 2) | 60–120 min | Web research; reading robots.txt and ToS |
| Geocoder envelope + filter widening (steps 3 + 4) | 15 min | Python edit |
| Source seed migration (step 5) | 15 min | SQL |
| Per-source loaders (step 6) | 45–120 min per source | Python; usually 1–3 sources per state |
| Resolver normalizer + ID registry + category map (steps 7 + 8) | 30 min | Python edits to known patterns |
| Access-view migration (step 9) | 10 min | SQL boilerplate |
| Workflow update (step 10) | 10 min | YAML edits |
| First load + verification (steps 11 + 12) | 30–60 min | Sit + watch |
| Docs (step 13) | 30 min | Markdown edits |
| Commit + CI (step 14) | 10 min | Git |
| **Total** | **~3–6 hours** | One person, one session |

At $40 per state and a 3–6 hour effort, the rate is $7–$13/hr —
intentionally low because most of the work is mechanical and the
patterns are already established. The fee is set to be low enough
that Austin can authorize per-state additions without going through
formal procurement.

States that exceed the 6-hour estimate (e.g. an agency that requires
a Playwright-driven download with anti-bot countermeasures, or a
PDF-only registry that needs an extraction layer) should be flagged
back to Axiom Insights — the $40 fee assumes the state's data shape
is similar to TX or NC.

---

## When to escalate to Axiom Insights

Escalate when:

- The new state's primary data source is **robots-disallowed** and
  there's no alternative download path. The locked decision 8.12
  forbids scraping; we'd need to add a manual-drop loader, which is
  a significant build.
- The new state's data is in a **format the codebase doesn't yet
  handle** (e.g. SOAP API, Excel files larger than 100 MB, scanned
  PDFs requiring OCR). Locked architecture is HTTP + REST + CSV/XLSX
  / XLS / JSON / ArcGIS REST / Playwright; outside that, we need a
  scoped extension.
- The state has **a new category not in the 7-category v1 scope**.
  Locked: the 7 categories are v1; adding cat 8 needs scope
  re-negotiation.
- **Geographic ambiguity** — e.g. a state with offshore-territory
  facilities that fall outside the envelope-based consistency check.
  The geocoder may need a per-state extension to STATE_BOUNDS that
  includes the offshore region.

In all other cases, follow this runbook end-to-end without
escalation.
