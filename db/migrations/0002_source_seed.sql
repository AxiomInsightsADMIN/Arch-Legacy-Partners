-- =============================================================================
-- 0002_source_seed.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Source registry seed v0.1. Authored 2026-05-11 by Axiom Insights.
--
-- DO NOT APPLY until Ryan has reviewed. Run after 0001_initial.sql.
--
-- ToS posture and robots.txt status are populated from a Day-1 audit
-- (recorded in /docs/source_audit_phase0.md). Re-audit during Phase 6.
--
-- Idempotent: ON CONFLICT (slug) DO UPDATE so re-running the migration is
-- safe and reflects updated audit values without orphaning rows.
-- =============================================================================

BEGIN;

INSERT INTO source (
    slug, name, type, base_url, tos_url, tos_posture, robots_txt_status, notes, last_checked_at
) VALUES

-- ---------------------------------------------------------------------------
-- Federal sources
-- ---------------------------------------------------------------------------
(
    'epa_echo',
    'EPA Enforcement and Compliance History Online (ECHO)',
    'federal',
    'https://echo.epa.gov/',
    'https://echo.epa.gov/resources/general-info/terms-of-service',
    'permissive',
    'allow',
    'Public US-gov data. robots.txt allows root crawling with Crawl-delay: 10 but disallows '
    '/facilities/facility-search/results/ and /detailed-facility-report/. Use the bulk-data '
    'exporter at https://echo.epa.gov/tools/data-downloads — that is the supported access path '
    'and is explicitly allowed. Do not scrape search-results pages.',
    NOW()
),
(
    'epa_cwns_2022',
    'EPA Clean Watersheds Needs Survey (CWNS) 2022',
    'federal',
    'https://www.epa.gov/cwns',
    'https://www.epa.gov/web-policies-and-procedures/epa-disclaimers',
    'permissive',
    'allow',
    'Public US-gov data. Downloadable Excel and Access database releases. Static dataset; '
    'no on-page scraping required. '
    'NOTE (Checkpoint-2 / B2): the 2022 CWNS Data Dashboard at '
    'sdwis.epa.gov/ords/sfdw_pub/r/sfdw/cwns_pub exposes a session-scoped '
    '`/download-state-zip?p2_location_id=<STATE>&session=<S>&cs=<CS>` endpoint. '
    'A Phase-1 spike (target: 30 min, executed at the federal data-load step) '
    'will validate the two-request flow (GET /about -> capture session+cs; '
    'GET /download-state-zip). If it works, the CWNS loader uses it and '
    'skips Playwright. If not, fall back to Playwright automation against '
    'the APEX app and document the negative result on the build_log.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- State NPDES registry placeholder (future states; TX/NC NPDES is handled
-- via TCEQ and NC DEQ DWR rows below).
-- ---------------------------------------------------------------------------
(
    'state_npdes',
    'State NPDES Registry (placeholder, future states)',
    'registry',
    NULL,
    NULL,
    'unknown',
    'unknown',
    'Placeholder for future expansion-state NPDES interfaces (e.g. NY SPDES, CA SWRCB). '
    'For v1 TX coverage, NPDES role is served by TCEQ Domestic Wastewater. For v1 NC '
    'coverage, NPDES role is served by NC DEQ DWR. No scraping wired to this row in v1. '
    'No Terms of Service URL applicable; this is a placeholder or internal source.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- Texas — TCEQ
-- ---------------------------------------------------------------------------
(
    'tceq_central_registry',
    'TCEQ Central Registry (CRPUB)',
    'state',
    'https://www15.tceq.texas.gov/crpub/',
    'https://www.tceq.texas.gov/help/policies/index.html',
    'permissive',
    'disallow',
    'IMPORTANT: www15.tceq.texas.gov/robots.txt is "User-agent: * / Disallow: /" — a total '
    'crawler ban. Per locked decision 8.12 (ToS audit per source), we will NOT scrape CRPUB '
    'directly. Records are publicly available; we obtain them via TCEQ Public Data Lookup '
    'downloads (slug: tceq_public_data_lookup), which republish CRPUB data as structured '
    'CSV/XLSX. The loader for this source pulls from those downloads. '
    'ToS note: TCEQ does not publish a single document titled "Terms of Service"; the '
    'closest equivalent is the Website Policies index '
    '(https://www.tceq.texas.gov/help/policies/index.html), which aggregates the Site '
    'Disclaimer, Public Domain and Linking Policy, Privacy, and Accessibility policies. '
    'The Public Domain and Linking Policy (/help/policies/linking_policy.html) states '
    'TCEQ web content is public domain unless otherwise noted. The earlier candidate URL '
    'main_terms.html returns 404 (verified 2026-05-11).',
    NOW()
),
(
    'tceq_public_data_lookup',
    'TCEQ Public Data Lookup',
    'state',
    'https://www.tceq.texas.gov/agency/data/lookup-data',
    'https://www.tceq.texas.gov/help/policies/index.html',
    'permissive',
    'allow',
    'Umbrella catalogue of TCEQ public data downloads — the supported access path that '
    'replaces direct scraping of CRPUB (which is robots-disallowed). Day-1 reconnaissance '
    'found six relevant lookup paths including: '
    '"Waste Management Permit Applications, Permits, Registrations, and Facilities" '
    '(landfills, transfer stations, MSW, composting) and "Status of Stormwater and '
    'Wastewater Applications and Specifications" (TPDES / domestic wastewater). '
    'robots.txt on www.tceq.texas.gov allows our paths.',
    NOW()
),
(
    'tceq_domestic_wastewater',
    'TCEQ Domestic Wastewater Permits',
    'state',
    'https://www.tceq.texas.gov/permitting/wastewater/municipal',
    'https://www.tceq.texas.gov/help/policies/index.html',
    'permissive',
    'allow',
    'Public records. Program landing page for Texas municipal wastewater permits and '
    'sewage sludge/biosolids land application. Facility-list downloads are reached via '
    'tceq_public_data_lookup; this row covers the program-landing content (process docs, '
    'forms, sub-page indices). robots.txt allows our paths (only /search and a few admin '
    'paths are disallowed on www.tceq.texas.gov). Mix of HTML pages and PDF documents.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- North Carolina — NC DEQ split across two divisions per section 9
-- ---------------------------------------------------------------------------
(
    'nc_deq_dwr',
    'NC DEQ Division of Water Resources (DWR)',
    'state',
    'https://www.deq.nc.gov/about/divisions/water-resources',
    'https://www.deq.nc.gov/about/policies',
    'permissive',
    'allow',
    'Public records. Covers POTW permits and biosolids land application programs. robots.txt '
    'allows all of our paths.',
    NOW()
),
(
    'nc_deq_dwm',
    'NC DEQ Division of Waste Management (DWM)',
    'state',
    'https://www.deq.nc.gov/about/divisions/waste-management',
    'https://www.deq.nc.gov/about/policies',
    'permissive',
    'allow',
    'Public records. Covers solid waste, transfer stations, and composting permits. '
    'robots.txt allows all of our paths.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- Placeholders — added so child rows can be associated; concrete sources
-- (per-county, per-state registry, per-operator) attach via NOTES or via
-- additional migrations as they are identified.
-- ---------------------------------------------------------------------------
(
    'county_health_placeholder',
    'County Health Departments (placeholder)',
    'county',
    NULL,
    NULL,
    'unknown',
    'unknown',
    'Placeholder for per-county health-department onsite-wastewater / hauler programs. '
    'Per section 9, this category is "Practical" for private/regional septage and "Practical" '
    'for county manhole programs. Top-N counties to be enumerated during Phase 2 (Days 3-5) '
    'and attached as additional source rows (slug pattern: county_health_<state>_<county>). '
    'No Terms of Service URL applicable; this is a placeholder or internal source.',
    NOW()
),
(
    'state_registries_placeholder',
    'State-Specific Registries (placeholder)',
    'registry',
    NULL,
    NULL,
    'unknown',
    'unknown',
    'Placeholder for state-specific registries that fall outside DEQ/TCEQ (e.g. agriculture '
    'department biosolids registries). Concrete entries added during Phase 2. '
    'No Terms of Service URL applicable; this is a placeholder or internal source.',
    NOW()
),
(
    'operator_sites_placeholder',
    'Private Operator Websites (placeholder)',
    'operator_site',
    NULL,
    NULL,
    'unknown',
    'unknown',
    'Placeholder for individual operator websites (e.g. Synagro, Denali, regional haulers). '
    'Each concrete operator site gets its own source row when added, with its own ToS audit. '
    'Default assumption is restrictive until proven otherwise. '
    'No Terms of Service URL applicable; this is a placeholder or internal source.',
    NOW()
),

-- ---------------------------------------------------------------------------
-- Discovery crawl (Phase 4.5)
-- ---------------------------------------------------------------------------
(
    'discovery_crawl',
    'Discovery Crawl (Brave Search + Haiku extraction)',
    'discovery_crawl',
    NULL,
    NULL,
    'permissive',
    'none',
    'Internal source category for Phase 4.5 discovery: Brave Search API issues bounded queries '
    'per (category × state), Playwright/BeautifulSoup/pdfplumber fetches candidate URLs (honoring '
    'each target site''s robots.txt at fetch time), and Haiku extracts structured candidates. '
    'Net-new entities are gated through discovery_review_queue before promotion to canonical_facility. '
    'No Terms of Service URL applicable; this is a placeholder or internal source.',
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
-- End of 0002_source_seed.sql
-- =============================================================================
