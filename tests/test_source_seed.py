"""Static-text tests for db/migrations/0002_source_seed.sql.

These are *file-level* assertions — they read the SQL as text without
applying it. The actual schema-apply happens in CI in the `schema` job
(see .github/workflows/ci.yml), which verifies the row count, slugs, and
robots-disallow flag against a throwaway Postgres."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parent.parent / "db" / "migrations" / "0002_source_seed.sql"

EXPECTED_SLUGS = [
    "epa_echo",
    "epa_cwns_2022",
    "state_npdes",
    "tceq_central_registry",
    "tceq_public_data_lookup",
    "tceq_domestic_wastewater",
    "nc_deq_dwr",
    "nc_deq_dwm",
    "county_health_placeholder",
    "state_registries_placeholder",
    "operator_sites_placeholder",
    "discovery_crawl",
]


@pytest.fixture(scope="module")
def seed_sql() -> str:
    return SEED.read_text(encoding="utf-8")


def test_seed_exists(seed_sql):
    assert "INSERT INTO source" in seed_sql


def test_all_expected_slugs_present(seed_sql):
    for slug in EXPECTED_SLUGS:
        assert f"'{slug}'" in seed_sql, f"slug missing from seed: {slug}"


def test_no_extra_slugs(seed_sql):
    """All slug literals appearing as the first column of an INSERT tuple
    must be in the expected set. (Prevents drift between this test and
    the seed.)"""
    found = re.findall(
        r"^\s*'([a-z_0-9]+)',\s*$\n\s*'[^']+',\s*\n\s*'(?:federal|state|county|registry|operator_site|discovery_crawl)',",
        seed_sql,
        flags=re.M,
    )
    assert sorted(found) == sorted(EXPECTED_SLUGS), (
        f"unexpected slug set — got {sorted(found)}, expected {sorted(EXPECTED_SLUGS)}"
    )


def test_tceq_central_registry_marked_disallow(seed_sql):
    """Checkpoint-2 A2.2 / 8.12 locked decision."""
    # The disallow string must be in the tceq_central_registry tuple. Look for
    # the slug, then check the row block up to the next closing paren.
    m = re.search(r"'tceq_central_registry',(.*?)\)\s*,", seed_sql, flags=re.S)
    assert m, "tceq_central_registry row not found"
    block = m.group(1)
    assert "'disallow'" in block, "tceq_central_registry must carry robots_txt_status='disallow'"


def test_idempotent_on_conflict(seed_sql):
    assert "ON CONFLICT (slug) DO UPDATE" in seed_sql


def test_placeholder_rows_have_explicit_no_tos_note(seed_sql):
    """Checkpoint-2 A2.4 — 5 placeholder/internal rows carry the explicit
    'No Terms of Service URL applicable' note."""
    needle = "No Terms of Service URL applicable; this is a placeholder or internal source."
    count = seed_sql.count(needle)
    assert count == 5, f"expected 5 placements of the no-ToS note, found {count}"


def test_tceq_rows_use_policies_index(seed_sql):
    """Checkpoint-2 A2.4 — all three TCEQ rows carry the Website Policies
    index URL as their tos_url. (main_terms.html returns 404.)"""
    needle = "https://www.tceq.texas.gov/help/policies/index.html"
    count = seed_sql.count(needle)
    # Each of 3 TCEQ rows carries it as tos_url; plus the URL is referenced
    # once in the central_registry notes block. So we expect >= 3.
    assert count >= 3, f"expected the TCEQ policies-index URL >=3 times, found {count}"
