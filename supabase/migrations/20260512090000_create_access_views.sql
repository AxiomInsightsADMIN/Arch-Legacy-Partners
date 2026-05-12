-- =============================================================================
-- 20260512090000_create_access_views.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 5 access layer: 20 SQL Views over canonical_facility.
--
-- All views are CREATE OR REPLACE so the migration is idempotent. None of
-- them touch base data; they are pure SELECT projections that:
--   - filter to in-scope rows (facility_type IS NOT NULL)
--   - slice by state and/or canonical category
--   - surface acceptance-flag positives once Phase 4 enrichment lands
--
-- Column shape for every view matches exports/facilities_primary.csv so
-- Austin's team gets the same 22 columns whether they pull via Table
-- Editor, ad-hoc SQL, or the monthly CSV refresh.
--
-- Every view carries a COMMENT ON VIEW so Supabase Table Editor renders
-- the description inline next to the view name in the sidebar.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Three scope views (in-scope = facility_type IS NOT NULL)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_all_in_scope AS
  SELECT id, name, facility_type, street, city, state, zip, county,
         latitude, longitude,
         accepts_septage, accepts_grease_trap, accepts_portable_toilet,
         pricing_notes, phone, email, website,
         frs_id, npdes_id, state_permit_id,
         first_seen_at, last_seen_at
    FROM canonical_facility
   WHERE facility_type IS NOT NULL;

COMMENT ON VIEW v_all_in_scope IS
  'Every canonical facility that maps to one of the seven v1 categories. '
  'Cross-state. Excludes the ~70K NULL-type rows (mostly ECHO industrial '
  'NPDES outside the v1 scope). Same row set as exports/facilities_primary.csv.';

CREATE OR REPLACE VIEW v_tx_in_scope AS
  SELECT * FROM v_all_in_scope WHERE state = 'TX';

COMMENT ON VIEW v_tx_in_scope IS
  'Texas in-scope canonical facilities. v_all_in_scope filtered to state=TX.';

CREATE OR REPLACE VIEW v_nc_in_scope AS
  SELECT * FROM v_all_in_scope WHERE state = 'NC';

COMMENT ON VIEW v_nc_in_scope IS
  'North Carolina in-scope canonical facilities. v_all_in_scope filtered to state=NC.';


-- -----------------------------------------------------------------------------
-- 2. Fourteen per-state-per-type views (2 states × 7 categories)
-- Naming pattern: v_<state>_<facility_type> in snake_case.
-- Categories match the slugs in config/facility_types.yaml.
-- -----------------------------------------------------------------------------

-- TX × 7 categories ---------------------------------------------------------

CREATE OR REPLACE VIEW v_tx_potw_receiving_station AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'potw_receiving_station';
COMMENT ON VIEW v_tx_potw_receiving_station IS
  'TX POTW Receiving Stations. Public-owned WWTPs that operate a hauler/'
  'septage receiving station. Bare POTWs without an acceptance signal are '
  'in v_tx_in_scope under facility_type=NULL until Phase 4 promotes them.';

CREATE OR REPLACE VIEW v_tx_county_manhole_program AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'county_manhole_program';
COMMENT ON VIEW v_tx_county_manhole_program IS
  'TX County/municipal manhole disposal programs. Empty until Phase 4.5 '
  'discovery crawl surfaces these; v1 sources do not publish them.';

CREATE OR REPLACE VIEW v_tx_land_application_site AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'land_application_site';
COMMENT ON VIEW v_tx_land_application_site IS
  'TX biosolids/septage land application sites. v1 gap on TX side — '
  'TCEQ keeps the registry in CRPUB (declined per locked decision 8.12). '
  'Empty until Phase 4 discovery / Phase 6 v2 widens scope.';

CREATE OR REPLACE VIEW v_tx_private_regional_septage_facility AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'private_regional_septage_facility';
COMMENT ON VIEW v_tx_private_regional_septage_facility IS
  'TX private/regional septage facilities. Sourced from TCEQ MSW '
  'liquid-waste subset (Physical Type 5GG, 5TL, 5GM per GI-613).';

CREATE OR REPLACE VIEW v_tx_composting_facility AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'composting_facility';
COMMENT ON VIEW v_tx_composting_facility IS
  'TX permitted composting facilities. Sourced from TCEQ MSW Physical '
  'Type 5RC (permitted) + 5RCX (NOI-tier) per GI-613.';

CREATE OR REPLACE VIEW v_tx_anaerobic_digester AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'anaerobic_digester';
COMMENT ON VIEW v_tx_anaerobic_digester IS
  'TX anaerobic digesters / beneficial gas recovery facilities. Sourced '
  'from TCEQ MSW Physical Type 9GR per GI-613. Only v1 cat-6 contribution.';

CREATE OR REPLACE VIEW v_tx_transfer_station AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'TX' AND facility_type = 'transfer_station';
COMMENT ON VIEW v_tx_transfer_station IS
  'TX waste transfer stations. Sourced from TCEQ MSW Physical Types '
  '5TS (permitted) + 5LV (NOI low-volume) + 5CC (citizens collection) per GI-613.';

-- NC × 7 categories ---------------------------------------------------------

CREATE OR REPLACE VIEW v_nc_potw_receiving_station AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'potw_receiving_station';
COMMENT ON VIEW v_nc_potw_receiving_station IS
  'NC POTW Receiving Stations. Sourced from NC ND PERMIT_TYPE matching '
  '"reclaimed water" + CWNS Treatment Plant rows that Phase 4 confirms.';

CREATE OR REPLACE VIEW v_nc_county_manhole_program AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'county_manhole_program';
COMMENT ON VIEW v_nc_county_manhole_program IS
  'NC County/municipal manhole disposal programs. Empty until Phase 4.5 '
  'discovery surfaces them; v1 NC sources do not publish them.';

CREATE OR REPLACE VIEW v_nc_land_application_site AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'land_application_site';
COMMENT ON VIEW v_nc_land_application_site IS
  'NC biosolids/septage land application sites. Sourced from NC ND '
  'PERMIT_TYPE matching "Residual Solids" (503 + 503-Exempt rules).';

CREATE OR REPLACE VIEW v_nc_private_regional_septage_facility AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'private_regional_septage_facility';
COMMENT ON VIEW v_nc_private_regional_septage_facility IS
  'NC private/regional septage facilities. Sourced primarily from the '
  'NC DEQ DWM Septage Firm list (Activity=Hauler, all 759 rows direct '
  'cat-4 hits) plus NC ND wastewater-irrigation permits.';

CREATE OR REPLACE VIEW v_nc_composting_facility AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'composting_facility';
COMMENT ON VIEW v_nc_composting_facility IS
  'NC permitted composting facilities. Sourced from NC DEQ DWM Solid '
  'Waste list (Activity=Compost).';

CREATE OR REPLACE VIEW v_nc_anaerobic_digester AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'anaerobic_digester';
COMMENT ON VIEW v_nc_anaerobic_digester IS
  'NC anaerobic digesters. v1 gap on NC side — no NC-specific public '
  'roster surfaced in audit. Empty until Phase 4 discovery widens.';

CREATE OR REPLACE VIEW v_nc_transfer_station AS
  SELECT * FROM v_all_in_scope
   WHERE state = 'NC' AND facility_type = 'transfer_station';
COMMENT ON VIEW v_nc_transfer_station IS
  'NC waste transfer stations. Sourced from NC DEQ DWM Solid Waste list '
  '(Activity=Trans). The 30 Activity=Collection rows (HHW citizen drop-'
  'offs) are EXCLUDED by the canonical resolver per the locked filter rule.';


-- -----------------------------------------------------------------------------
-- 3. Three acceptance-flag views (forward-compatible; mostly empty until Phase 4)
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_accepts_septage AS
  SELECT * FROM v_all_in_scope WHERE accepts_septage = 'Yes';

COMMENT ON VIEW v_accepts_septage IS
  'In-scope facilities that explicitly accept septage waste. Cross-state. '
  'The accepts_septage column is populated by Phase 4 Haiku enrichment; '
  'until then this view is mostly empty by design (no false-positive '
  'inferences from facility type alone — per the kickoff-brief acceptance-'
  'flag tri-state lock: Yes/No/Unknown only when sourced).';

CREATE OR REPLACE VIEW v_accepts_grease_trap AS
  SELECT * FROM v_all_in_scope WHERE accepts_grease_trap = 'Yes';

COMMENT ON VIEW v_accepts_grease_trap IS
  'In-scope facilities that explicitly accept grease trap waste. Cross-'
  'state. Phase 4 Haiku populates the column; until then this view is '
  'mostly empty by design.';

CREATE OR REPLACE VIEW v_accepts_portable_toilet AS
  SELECT * FROM v_all_in_scope WHERE accepts_portable_toilet = 'Yes';

COMMENT ON VIEW v_accepts_portable_toilet IS
  'In-scope facilities that explicitly accept portable-toilet (porta-potty) '
  'waste. Cross-state. Phase 4 Haiku populates the column; until then this '
  'view is mostly empty by design.';

COMMIT;

-- =============================================================================
-- End of 20260512090000_create_access_views.sql
-- 20 views total: 3 scope + 14 per-state-per-type + 3 acceptance-flag.
-- =============================================================================
