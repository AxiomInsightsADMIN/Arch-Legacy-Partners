"""Tests for config/facility_types.yaml — the controlled vocabulary that
every loader normalizes through (locked decision 8.9). These tests are the
fast guardrail against accidental schema drift in the YAML."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "facility_types.yaml"

EXPECTED_CANONICAL_TYPES = {
    "potw_receiving_station",
    "county_manhole_program",
    "land_application_site",
    "private_regional_septage_facility",
    "composting_facility",
    "anaerobic_digester",
    "transfer_station",
}


@pytest.fixture(scope="module")
def vocab() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


class TestStructure:
    def test_file_exists(self):
        assert CONFIG_PATH.exists(), f"missing: {CONFIG_PATH}"

    def test_top_level_keys(self, vocab):
        assert "version" in vocab
        assert "types" in vocab
        assert isinstance(vocab["types"], dict)

    def test_exactly_seven_canonical_types(self, vocab):
        keys = set(vocab["types"].keys())
        assert keys == EXPECTED_CANONICAL_TYPES, (
            f"unexpected canonical types — missing={EXPECTED_CANONICAL_TYPES - keys}, "
            f"extra={keys - EXPECTED_CANONICAL_TYPES}"
        )

    @pytest.mark.parametrize("ctype", sorted(EXPECTED_CANONICAL_TYPES))
    def test_each_type_has_required_fields(self, vocab, ctype):
        body = vocab["types"][ctype]
        for field in ("label", "description", "synonyms", "regex_rules", "not_synonyms"):
            assert field in body, f"{ctype} missing {field}"
        assert body["synonyms"], f"{ctype} has empty synonyms"
        assert body["regex_rules"], f"{ctype} has empty regex_rules"
        # not_synonyms can in principle be empty for a category that has no
        # natural false-positive risk — but every current category has at
        # least one entry, so assert non-empty as a regression guard.
        assert body["not_synonyms"], f"{ctype} has empty not_synonyms"


class TestRegexCompiles:
    def test_all_regex_patterns_compile(self, vocab):
        failures: list[tuple[str, str, str]] = []
        for ctype, body in vocab["types"].items():
            for pat in body.get("regex_rules", []):
                try:
                    re.compile(pat)
                except re.error as e:
                    failures.append((ctype, pat, str(e)))
        assert not failures, f"regex compile failures: {failures}"


class TestPolicyConstraints:
    """Guardrails for the locked architectural decisions."""

    def test_land_application_denies_composting(self, vocab):
        denies = [d.lower() for d in vocab["types"]["land_application_site"]["not_synonyms"]]
        assert any("composting" in d for d in denies)

    def test_composting_denies_land_application(self, vocab):
        denies = [d.lower() for d in vocab["types"]["composting_facility"]["not_synonyms"]]
        assert any("land application" in d for d in denies)

    def test_transfer_station_denies_drinking_water(self, vocab):
        """Checkpoint-2 decision A3.6 — drinking-water terms must be in the
        transfer-station deny list to prevent the broad
        `\\btransfer\\s*station\\b` regex from over-matching drinking-water
        infrastructure."""
        denies = [d.lower() for d in vocab["types"]["transfer_station"]["not_synonyms"]]
        required = {
            "water transfer station",
            "drinking water",
            "raw water transfer",
            "treated water transfer",
            "potable water",
        }
        missing = required - set(denies)
        assert not missing, f"transfer_station deny list missing: {missing}"

    def test_potw_synonyms_are_receiving_station_specific(self, vocab):
        """Checkpoint-2 decision A3.3 — bare facility-type strings like
        'WWTP' or 'POTW' must NOT appear as standalone synonyms; every
        synonym must qualify the bare type with a receiving-station-like
        suffix or wrap it in parentheses."""
        synonyms = vocab["types"]["potw_receiving_station"]["synonyms"]
        for syn in synonyms:
            low = syn.lower()
            # Either the synonym explicitly mentions a receiving/headworks/hauler
            # context, OR the synonym is itself an obviously receiving-specific
            # phrase ('septage receiving facility (POTW)' etc.).
            assert any(
                token in low
                for token in (
                    "receiving",
                    "hauler",
                    "headworks",
                    "(potw)",
                    "(hauler",
                )
            ), f"non-receiving-specific synonym slipped in: {syn!r}"
