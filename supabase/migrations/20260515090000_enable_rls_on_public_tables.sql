-- Pre-handoff security cleanup: enable RLS on every public-schema table
-- with no policies (default-deny posture).
--
-- Why: Supabase's Advisors scanner flags `rls_disabled_in_public` on
-- any table exposed to the Data API without RLS. The publishable/anon
-- key can otherwise query the tables. RLS-enabled + no-policies =
-- default-deny for anon and authenticated roles.
--
-- Operational impact: zero. All build-phase tooling (scrapers, resolver,
-- enrichment, exporter, monthly cron) connects via direct Postgres with
-- the service_role key, which bypasses RLS by design.
--
-- To expose any table to a future Data API client, add explicit policies
-- via a new migration; see Supabase RLS documentation.

ALTER TABLE source ENABLE ROW LEVEL SECURITY;
ALTER TABLE scraper_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_signature ENABLE ROW LEVEL SECURITY;
ALTER TABLE raw_facility_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_facility ENABLE ROW LEVEL SECURITY;
ALTER TABLE facility_record_link ENABLE ROW LEVEL SECURITY;
ALTER TABLE field_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_facility_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE facility_type_lookup ENABLE ROW LEVEL SECURITY;
ALTER TABLE geocoding_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_enrichment_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovered_url ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_candidate_facility ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_review_queue ENABLE ROW LEVEL SECURITY;
