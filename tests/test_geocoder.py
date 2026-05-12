"""Tests for orchestration.geocoder — state-consistency warning policy
(Checkpoint-2 decision A1.6) plus the Phase-3 Census Geocoder client.

The state-consistency tests run offline. The Census client tests stub out
the HTTP call via monkeypatch so the unit suite never touches the network.
End-to-end exercise of the live Census endpoint is covered by the resolver
integration run, not by this file."""

from __future__ import annotations

import pytest

from orchestration import geocoder as geo
from orchestration.geocoder import (
    STATE_BOUNDS,
    GeocoderResult,
    coords_consistent_with_state,
    geocode_with_state_check,
)


class TestStateBounds:
    def test_tx_and_nc_present(self):
        assert "TX" in STATE_BOUNDS
        assert "NC" in STATE_BOUNDS

    def test_tx_envelope_shape(self):
        lo_lat, hi_lat, lo_lng, hi_lng = STATE_BOUNDS["TX"]
        assert lo_lat < hi_lat
        assert lo_lng < hi_lng
        # Texas is south of 37N, north of 25N, east of 107W, west of 93W
        assert lo_lat >= 25.0 and hi_lat <= 37.0
        assert lo_lng >= -107.0 and hi_lng <= -93.0

    def test_nc_envelope_shape(self):
        lo_lat, hi_lat, lo_lng, hi_lng = STATE_BOUNDS["NC"]
        assert lo_lat < hi_lat
        assert lo_lng < hi_lng
        # NC is south of 37N, north of 33N, east of 85W, west of 75W
        assert lo_lat >= 33.0 and hi_lat <= 37.0
        assert lo_lng >= -85.0 and hi_lng <= -75.0


class TestCoordsConsistentWithState:
    # Known city anchors well inside each state envelope
    @pytest.mark.parametrize(
        "lat,lng,state",
        [
            (30.2672, -97.7431, "TX"),  # Austin
            (29.7604, -95.3698, "TX"),  # Houston
            (32.7767, -96.7970, "TX"),  # Dallas
            (35.7796, -78.6382, "NC"),  # Raleigh
            (35.2271, -80.8431, "NC"),  # Charlotte
            (35.5951, -82.5515, "NC"),  # Asheville
        ],
    )
    def test_inside(self, lat, lng, state):
        assert coords_consistent_with_state(lat=lat, lng=lng, state=state) == "inside"

    @pytest.mark.parametrize(
        "lat,lng,state",
        [
            (30.2672, -97.7431, "NC"),  # Austin coords w/ NC state
            (35.7796, -78.6382, "TX"),  # Raleigh coords w/ TX state
            (40.7128, -74.0060, "TX"),  # NYC w/ TX
            (34.0522, -118.2437, "NC"),  # LA w/ NC
        ],
    )
    def test_outside(self, lat, lng, state):
        assert coords_consistent_with_state(lat=lat, lng=lng, state=state) == "outside"

    @pytest.mark.parametrize(
        "lat,lng,state",
        [
            (None, None, "TX"),
            (30.2672, None, "TX"),
            (None, -97.7431, "TX"),
            (30.2672, -97.7431, None),
            (30.2672, -97.7431, ""),
        ],
    )
    def test_unknown_on_null_inputs(self, lat, lng, state):
        assert coords_consistent_with_state(lat=lat, lng=lng, state=state) == "unknown"

    def test_unknown_state_without_envelope(self):
        # CA has no envelope yet; check returns 'unknown' so the loader
        # logs state_bounds_missing rather than crashing.
        assert coords_consistent_with_state(lat=34.05, lng=-118.24, state="CA") == "unknown"

    def test_state_case_insensitive(self):
        # USPS codes in the wild can arrive lowercase; the helper upper-cases.
        assert coords_consistent_with_state(lat=30.27, lng=-97.74, state="tx") == "inside"
        assert coords_consistent_with_state(lat=30.27, lng=-97.74, state="Tx") == "inside"


class TestGeocodeWithStateCheck:
    """Phase-3 Census Geocoder client. HTTP is monkeypatched so these run
    offline."""

    def _stub_census(self, monkeypatch, payload):
        """Replace _call_census with a function that returns the given payload."""
        monkeypatch.setattr(geo, "_call_census", lambda address: payload)

    def test_match_inside_state(self, monkeypatch):
        self._stub_census(
            monkeypatch,
            {
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "100 CONGRESS AVE, AUSTIN, TX, 78701",
                            "coordinates": {"x": -97.7431, "y": 30.2672},
                        }
                    ]
                }
            },
        )
        result = geocode_with_state_check(
            address="100 Congress Ave, Austin, TX", state="TX", conn=None
        )
        assert isinstance(result, GeocoderResult)
        assert result.lat == 30.2672
        assert result.lng == -97.7431
        assert result.confidence == "high"
        assert result.consistency == "inside"
        assert result.review_flag is False
        assert result.notes is None

    def test_match_outside_state_downgrades(self, monkeypatch):
        # NYC coords claimed to be in TX — should downgrade + review_flag.
        self._stub_census(
            monkeypatch,
            {
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "200 W 34TH ST, NEW YORK, NY",
                            "coordinates": {"x": -73.9857, "y": 40.7484},
                        }
                    ]
                }
            },
        )
        result = geocode_with_state_check(
            address="200 W 34th St, Austin, TX", state="TX", conn=None
        )
        assert result.confidence == "low"
        assert result.consistency == "outside"
        assert result.review_flag is True
        assert result.notes == "state_coord_mismatch"

    def test_no_match_returns_failed(self, monkeypatch):
        self._stub_census(monkeypatch, {"result": {"addressMatches": []}})
        result = geocode_with_state_check(address="999 Nonexistent St", state="TX", conn=None)
        assert result.lat is None
        assert result.lng is None
        assert result.confidence == "failed"
        assert result.notes == "census_no_match"

    def test_transport_error_returns_failed_uncached(self, monkeypatch):
        self._stub_census(monkeypatch, None)
        result = geocode_with_state_check(
            address="100 Congress Ave, Austin, TX",
            state="TX",
            conn=None,
            retries=0,
        )
        assert result.confidence == "failed"
        assert result.notes == "census_transport_error"

    def test_empty_address_short_circuits(self):
        result = geocode_with_state_check(address="", state="TX", conn=None)
        assert result.lat is None and result.lng is None
        assert result.confidence == "failed"
        assert result.notes == "empty_address"

    def test_state_bounds_missing_marks_review(self, monkeypatch):
        # CA coords w/ CA state — STATE_BOUNDS has no CA envelope yet.
        self._stub_census(
            monkeypatch,
            {
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "1 INFINITE LOOP, CUPERTINO, CA",
                            "coordinates": {"x": -122.0312, "y": 37.3318},
                        }
                    ]
                }
            },
        )
        result = geocode_with_state_check(
            address="1 Infinite Loop, Cupertino, CA", state="CA", conn=None
        )
        assert result.confidence == "medium"
        assert result.consistency == "unknown"
        assert result.review_flag is True
        assert result.notes == "state_bounds_missing"
