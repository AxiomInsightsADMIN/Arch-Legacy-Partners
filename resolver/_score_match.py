"""Score-based matching using RapidFuzz, with a 200m proximity tiebreak.

Locked thresholds from the kickoff brief:
  - >= 92      auto-merge
  - 75..91     hold-for-review; v1 resolver bumps via proximity tiebreak
               (within 200m -> auto-merge); otherwise creates a new canonical
               and stores the borderline score in facility_record_link so
               the review surface can find it
  - < 75       reject; create a new canonical

Match key: composite of (name, city, state). RapidFuzz `WRatio` over the
joined string. Bucketing is by (state, city) so we never compare a Houston
facility against a Charlotte one.

Proximity tiebreak: haversine distance between raw lat/lng and candidate
canonical lat/lng. <= 200m bumps a 75..91 score up to "auto-merge". If
either side is missing coords, no tiebreak applies (the score stands).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from resolver._normalize import NormalizedRaw

# Score thresholds (locked decision per kickoff brief)
AUTO_MERGE_THRESHOLD: float = 92.0
HOLD_FOR_REVIEW_THRESHOLD: float = 75.0
PROXIMITY_TIEBREAK_METERS: float = 200.0


@dataclass
class CandidateCanonical:
    """In-memory shape of an already-created canonical_facility, used for
    score-based matching of incoming raws. Populated by the resolver as it
    walks raws (new canonicals are added, merges update fields)."""

    canonical_id: str  # UUID
    name: str | None
    city: str | None
    state: str | None
    latitude: float | None
    longitude: float | None

    def match_key(self) -> str:
        parts = [self.name or "", self.city or "", self.state or ""]
        return " | ".join(parts).strip()


@dataclass
class ScoreMatchResult:
    canonical_id: str | None  # UUID of merge target; None means "no merge"
    score: float  # 0..100; the best score seen even if no merge
    tiebreak_applied: bool  # True when proximity bumped a borderline to merge
    decision: str  # 'auto_merge' | 'tiebreak_merge' | 'hold' | 'reject'


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters. WGS84 radius 6,371,008 m."""
    R = 6_371_008.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


@dataclass
class CanonicalIndex:
    """(state, city) -> list[CandidateCanonical]. State and city are normalized
    to upper / strip beforehand. Adding a new canonical appends to its
    bucket; updating an existing canonical mutates in place."""

    _by_state_city: dict[tuple[str, str], list[CandidateCanonical]] = field(default_factory=dict)
    _by_id: dict[str, CandidateCanonical] = field(default_factory=dict)

    @staticmethod
    def _key(state: str | None, city: str | None) -> tuple[str, str]:
        return ((state or "").upper(), (city or "").upper())

    def add(self, cand: CandidateCanonical) -> None:
        self._by_id[cand.canonical_id] = cand
        self._by_state_city.setdefault(self._key(cand.state, cand.city), []).append(cand)

    def lookup_bucket(self, state: str | None, city: str | None) -> list[CandidateCanonical]:
        return self._by_state_city.get(self._key(state, city), [])

    def get(self, canonical_id: str) -> CandidateCanonical | None:
        return self._by_id.get(canonical_id)

    def size(self) -> int:
        return len(self._by_id)


def find_best_match(*, raw: NormalizedRaw, index: CanonicalIndex) -> ScoreMatchResult:
    """Find best (state, city)-bucket candidate. Score via `fuzz.WRatio`
    on the (name | city | state) composite. Apply thresholds + proximity
    tiebreak."""
    if not raw.name:
        # No name -> no score-based match possible. Always create new.
        return ScoreMatchResult(
            canonical_id=None,
            score=0.0,
            tiebreak_applied=False,
            decision="reject",
        )

    bucket = index.lookup_bucket(raw.state, raw.city)
    if not bucket:
        return ScoreMatchResult(
            canonical_id=None,
            score=0.0,
            tiebreak_applied=False,
            decision="reject",
        )

    raw_key = f"{raw.name} | {raw.city or ''} | {raw.state or ''}".strip()

    best_score = -1.0
    best_cand: CandidateCanonical | None = None
    for cand in bucket:
        s = fuzz.WRatio(raw_key, cand.match_key())
        if s > best_score:
            best_score = s
            best_cand = cand

    assert best_cand is not None
    score = float(best_score)

    if score >= AUTO_MERGE_THRESHOLD:
        return ScoreMatchResult(
            canonical_id=best_cand.canonical_id,
            score=score,
            tiebreak_applied=False,
            decision="auto_merge",
        )

    if score >= HOLD_FOR_REVIEW_THRESHOLD:
        # Try proximity tiebreak.
        tiebreak = False
        if (
            raw.latitude is not None
            and raw.longitude is not None
            and best_cand.latitude is not None
            and best_cand.longitude is not None
        ):
            d = _haversine_m(raw.latitude, raw.longitude, best_cand.latitude, best_cand.longitude)
            if d <= PROXIMITY_TIEBREAK_METERS:
                tiebreak = True
        if tiebreak:
            return ScoreMatchResult(
                canonical_id=best_cand.canonical_id,
                score=score,
                tiebreak_applied=True,
                decision="tiebreak_merge",
            )
        return ScoreMatchResult(
            canonical_id=None,  # hold-for-review: create new canonical, flag score
            score=score,
            tiebreak_applied=False,
            decision="hold",
        )

    return ScoreMatchResult(
        canonical_id=None,
        score=score,
        tiebreak_applied=False,
        decision="reject",
    )
