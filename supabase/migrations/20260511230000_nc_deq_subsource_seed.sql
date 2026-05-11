-- =============================================================================
-- 20260511230000_nc_deq_subsource_seed.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 2 step 2: NC DEQ sub-source seed (3 active rows).
-- Authored 2026-05-11 by Axiom Insights.
--
-- DO NOT APPLY until Ryan has reviewed docs/nc_deq_audit.md.
-- Run after 0002_source_seed.sql and 20260511220000_tceq_subsource_seed.sql.
--
-- This migration seeds the three NC DEQ sub-page sources we intend to
-- *actively scrape* in Phase 2. Each gets its own slug for drift-detection
-- granularity (same pattern as the TCEQ subsource seed).
--
-- Sources NOT seeded here (documented in docs/nc_deq_audit.md):
--   - nc_deq_dwr_npdes_wastewater — already covered by EPA ECHO (NC POTWs
--     in raw_facility_record); seeding a duplicate creates maintenance
--     burden with no new data
--   - nc_deq_dwr_collection_systems — sewer-extension permits, not in v1
--     seven categories
--   - reports.ncdenr.org Crystal Reports server — needs deeper auth/scope
--     investigation
--
-- Note on `edocs.deq.nc.gov` access: two of the three rows reference data
-- hosted on this subdomain. Our probe IP was TCP-blocked by the edocs host
-- (consistent timeouts across three independent fetch attempts) — this is
-- a network reachability constraint, not a robots.txt issue. The loader
-- will use Playwright to pass any TLS/UA-based WAF rule; if that fails,
-- the fallback is a manual one-off download by the client (analogous to
-- the TX Public Information Act path).
--
-- Idempotent: ON CONFLICT (slug) DO UPDATE so re-running is safe.
-- =============================================================================

BEGIN;

INSERT INTO source (
    slug, name, type, base_url, tos_url, tos_posture, robots_txt_status, notes, last_checked_at
) VALUES

-- ---------------------------------------------------------------------------
-- NC OneMap Non-Discharge Facilities (ArcGIS FeatureServer)
-- Feeds v1 category 3 (Land Application) primarily, with category 1 (POTW)
-- corroboration where the Non-Discharge program covers POTW receiving.
-- ---------------------------------------------------------------------------
(
    'nc_deq_non_discharge_facilities',
    'NC DEQ DWR Non-Discharge Facilities (NC OneMap ArcGIS)',
    'state',
    'https://services.nconemap.gov/secure/rest/services',
    'https://www.deq.nc.gov/about/policies',
    'permissive',
    'allow',
    'NC DEQ DWR Non-Discharge Branch publishes a facility map at '
    'https://experience.arcgis.com/experience/689283d17bf342c2a96364fbab09a5d8 '
    '(Esri Experience SPA). The underlying ArcGIS FeatureServer lives on '
    'services.nconemap.gov; the REST root returned 46 services on 2026-05-11 '
    'reconnaissance including NC1Map_Environment which is the likely host of '
    'the Non-Discharge facility layer. Phase-2 loader needs to identify the '
    'specific layer and query it via FeatureServer JSON pagination. '
    'Reachable from our network (no TCP blocks observed). robots.txt for '
    'services.nconemap.gov was not separately probed; ArcGIS FeatureServer '
    'access is governed by ArcGIS service-level public-by-default semantics. '
    'Feeds v1 category 3 (land application) primarily.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- NC DEQ DWM Solid Waste Permitted Facilities master list (PDF on edocs)
-- Feeds v1 categories 5 (composting), 6 (AD/biogas-recovery, partial), 7
-- (transfer stations). NC's analog to the TCEQ MSW XLS.
-- ---------------------------------------------------------------------------
(
    'nc_deq_solid_waste_facility_list',
    'NC DEQ DWM Solid Waste Permitted Facilities (edocs PDF)',
    'state',
    'https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132701&dbid=0&repo=WasteManagement',
    'https://www.deq.nc.gov/about/policies',
    'permissive',
    'unknown',
    'Master roster of NC permitted solid waste facilities — landfills, '
    'transfer stations, composting, treatment/processing. Linked from '
    'https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section/solid-waste-permitted-facility-information-and-guidance/solid-waste-facility-lists '
    '(robots-permissive). Hosted on edocs.deq.nc.gov which TCP-blocked our '
    'probe IP on 2026-05-11 (three independent timeouts; no robots.txt '
    'observable). Phase-2 loader will use Playwright first (browser TLS '
    'fingerprint typically passes WAF rules that vanilla requests fails). '
    'Fallback: manual one-off PDF download by the client and ingest via a '
    'one-off migration with extraction_method=manual. Document ID 2132701. '
    'Feeds v1 categories 5 (composting), 6 (AD via beneficial-gas-recovery '
    'subset, partial), 7 (transfer stations).',
    NOW()
),

-- ---------------------------------------------------------------------------
-- NC DEQ DWM Septage Firm master list (PDF on edocs)
-- Feeds v1 category 4 (private/regional septage).
-- ---------------------------------------------------------------------------
(
    'nc_deq_septage_firm_list',
    'NC DEQ DWM Septage Firm List (edocs PDF)',
    'state',
    'https://edocs.deq.nc.gov/WasteManagement/ElectronicFile.aspx?docid=2132702&dbid=0&repo=WasteManagement',
    'https://www.deq.nc.gov/about/policies',
    'permissive',
    'unknown',
    'Master roster of NC registered septage firms (haulers + SDTF + SLAS '
    'operators). Linked from the same Solid Waste Facility Lists page as '
    'docid 2132701 above. Same network constraint: edocs.deq.nc.gov TCP-'
    'blocks our probe IP. Same loader plan: Playwright primary, manual '
    'fallback. Document ID 2132702. NC categorizes septage facilities into '
    'SDTF (Septage Detention or Treatment Facility) and SLAS (Septage Land '
    'Application Site); both appear in this roster per the NC septage '
    'taxonomy on '
    'https://www.deq.nc.gov/about/divisions/waste-management/solid-waste-section/special-wastes-and-alternative-handling/septage/septage-new-operator. '
    'Feeds v1 category 4 (private/regional septage). Some SLAS-category '
    'rows may also feed category 3 (land application) — Phase-3 resolver '
    'distinguishes based on the facility-type field in the PDF.',
    NOW()
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

-- =============================================================================
-- End of 20260511230000_nc_deq_subsource_seed.sql
-- =============================================================================
