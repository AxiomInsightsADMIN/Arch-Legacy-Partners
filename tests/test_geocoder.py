"""Tests for orchestration.geocoder — specifically the state-consistency
warning policy (Checkpoint-2 decision A1.6). The Census Geocoder client
itself is a Phase-1 stub and is exercised indirectly via the loaders."""

from __future__ import annotations

import pytest

from orchestration.geocoder import (
    STATE_BOUNDS,
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


class TestGeocodeWithStateCheckStub:
    """`geocode_with_state_check` is a Phase-1 stub. It must raise
    NotImplementedError with a non-empty message so callers fail loudly
    rather than silently."""

    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError) as excinfo:
            geocode_with_state_check(address="100 Congress Ave, Austin, TX", state="TX")
        assert "Phase 1" in str(excinfo.value)
