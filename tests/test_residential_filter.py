"""Tests for resolver._residential_filter — the SFR over-merge
mitigation introduced in Phase 4 follow-on to the design pin at
commit b18283b."""

from __future__ import annotations

import pytest

from resolver._normalize import NormalizedRaw
from resolver._residential_filter import (
    _SFR_PATTERN,
    NC_ND_RESIDENTIAL_PERMIT_TYPE,
    NC_ND_SOURCE_SLUG,
    check_residential,
)


def _make_raw(
    *,
    source_slug: str = NC_ND_SOURCE_SLUG,
    name: str | None = None,
    permit_type: str | None = None,
) -> NormalizedRaw:
    """Minimal NormalizedRaw with only the fields the SFR filter reads."""
    return NormalizedRaw(
        raw_id=0,
        source_slug=source_slug,
        source_record_id="WQ0000001",
        name=name,
        raw_facility_type_string=permit_type,
    )


class TestSfrPattern:
    """Verify the bare regex catches what we want and rejects what we don't.
    These are the boundary cases the Phase 4 design pin enumerates."""

    @pytest.mark.parametrize(
        "name",
        [
            # Pure SFR suffix
            "972 New Elam Church Rd. SFR",
            "2716 Weaver Hill Dr. SFR",
            "1038 King Dr. SFR",
            "72 Anfield Rd. SFR",
            # Single-letter / SFD / SFW variants
            "100 Main St SFD",
            "55 Oak Lane SFW",
            # Full word "Residence"
            "200 Park Ave Residence",
            "501 First St. RESIDENCE",
            # RESIDENTIAL spelled out
            "800 Cedar Lane Residential",
            # RES. with dot (the abbreviated form)
            "300 Maple Ave Res.",
            # S.F.R. dotted form
            "150 Pine St. S.F.R.",
            # HOME suffix
            "888 River Rd Home",
            # Case insensitivity
            "999 Lakeside dr. sfr",
        ],
    )
    def test_matches(self, name: str) -> None:
        assert _SFR_PATTERN.match(name) is not None, f"expected match: {name!r}"

    @pytest.mark.parametrize(
        "name",
        [
            # Business names — should never match
            "Twelve Mile Creek WWTP",
            "Lloyds Portable Toilet Rentals",
            "Bradsher & Son Septic Tank Cleaning Inc.",
            "EMA Resources Class A Residuals Program",  # 'Residuals' not 'Residential'
            "Blue Diamond Portable Restrooms",
            "RES Energy Group",  # RES is part of the business name, no leading digit
            "ABC Septic Solutions",
            # POTW names with numeric prefixes but no residential suffix
            "10th Street WWTP",
            # No leading digit
            "Smith Family Property SFR",
            # Empty / single-token names
            "",
            "House",
            "1 SFR",  # missing the street-name middle word
        ],
    )
    def test_no_match(self, name: str) -> None:
        assert _SFR_PATTERN.match(name) is None, f"expected NO match: {name!r}"


class TestCheckResidential:
    """End-to-end filter verdict (regex + source_slug + PERMIT_TYPE)."""

    def test_nc_nd_residential_high_confidence(self) -> None:
        """Regex matches AND PERMIT_TYPE confirms residential ->
        matched=True, high_confidence=True (the 589-row bulk case)."""
        raw = _make_raw(
            name="972 New Elam Church Rd. SFR",
            permit_type=NC_ND_RESIDENTIAL_PERMIT_TYPE,
        )
        r = check_residential(raw)
        assert r.matched is True
        assert r.high_confidence is True

    def test_nc_nd_residential_medium_confidence(self) -> None:
        """Regex matches BUT PERMIT_TYPE does NOT confirm -> matched=True,
        high_confidence=False (the borderline / review case)."""
        raw = _make_raw(
            name="2716 Weaver Hill Dr. SFR",
            permit_type="Wastewater Irrigation",  # commercial, not residential
        )
        r = check_residential(raw)
        assert r.matched is True
        assert r.high_confidence is False

    def test_nc_nd_business_name_no_match(self) -> None:
        """Business name on NC ND -> matched=False, high_confidence=False."""
        raw = _make_raw(
            name="EMA Resources Class A Residuals Program",
            permit_type="Land Application of Residual Solids (503)",
        )
        r = check_residential(raw)
        assert r.matched is False
        assert r.high_confidence is False

    def test_nc_nd_null_name_no_match(self) -> None:
        raw = _make_raw(name=None, permit_type=NC_ND_RESIDENTIAL_PERMIT_TYPE)
        r = check_residential(raw)
        assert r.matched is False
        assert r.high_confidence is False

    @pytest.mark.parametrize(
        "other_slug",
        [
            "epa_echo",
            "epa_cwns_2022",
            "tceq_msw_facilities_xls",
            "nc_deq_solid_waste_facility_list",
            "nc_deq_septage_firm_list",
        ],
    )
    def test_non_nc_nd_sources_no_match(self, other_slug: str) -> None:
        """The filter is NC-ND-only. A name like '100 Main St. SFR' on
        ECHO or TCEQ MSW must NOT trigger the filter — that's a real
        business name in those data spaces."""
        raw = _make_raw(
            source_slug=other_slug,
            name="100 Main St. SFR",
            permit_type=None,
        )
        r = check_residential(raw)
        assert r.matched is False, (
            f"filter must not fire for non-NC-ND sources; failed on {other_slug}"
        )
        assert r.high_confidence is False
