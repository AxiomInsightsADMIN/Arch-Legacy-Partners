"""ID-first matching — locked decision 8.10.

A NormalizedRaw may carry up to seven stable identifiers (see
docs/build_log.md -> Phase 3 prep -> Stable identifier formats):

  - npdes_id           cross-source (ECHO + CWNS)
  - frs_id             cross-source (ECHO + potential CWNS sub-table)
  - cwns_id            single-source (CWNS)
  - tceq_additional_id single-source (TCEQ per-permit)
  - tceq_rn            single-source (TCEQ per-entity)
  - nc_permit_number   single-source (NC ND, `WQ\\d{7}`)
  - nc_facility_id     single-source (NC SW, composite)
  - nc_septage_permit  single-source (NC SF, `NCS-\\d{5}`)

ID-first matching maintains a registry from each ID to its assigned
canonical_facility UUID. When a new raw arrives with an ID already in the
registry, the raw is linked to that canonical. ID-first overrides
score-based matching: even if the name/city/state look unrelated, a shared
stable ID means the same entity.

Match precedence within ID-first (when a raw has more than one ID):
  1. NPDES (cross-source, federally maintained)
  2. FRS   (cross-source, federally maintained)
  3. CWNS  (federal spine)
  4. TCEQ Additional ID  / RN  (state)
  5. NC PERMITNUMBER / Facility Id / Permit  (state)

First hit wins. If two IDs would point to DIFFERENT canonicals, that's a
merge candidate — the resolver logs a `id_first_conflict` and keeps the
earlier one for now. (Phase 4 review-queue will surface these for human
adjudication; the v1 resolver does not merge canonicals retroactively.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from resolver._normalize import NormalizedRaw


@dataclass(frozen=True)
class IdMatchResult:
    canonical_id: str | None  # UUID string, None when no ID hit
    matched_field: str | None  # which NormalizedRaw field hit (e.g. 'npdes_id')
    conflict: bool = False  # True when >1 ID resolved to different canonicals


# Order matters: federal-first, then state. Cross-source IDs first so they
# can collapse ECHO+CWNS rows that share an NPDES before either source's
# single-source IDs get a chance to anchor independently.
ID_PRECEDENCE: tuple[str, ...] = (
    "npdes_id",
    "frs_id",
    "cwns_id",
    "tceq_additional_id",
    "tceq_rn",
    "nc_permit_number",
    "nc_facility_id",
    "nc_septage_permit",
)


@dataclass
class IdRegistry:
    """In-memory map from each ID type to canonical UUID. Populated as the
    resolver walks raws in source order. Single-threaded; no locking."""

    npdes_id: dict[str, str] = field(default_factory=dict)
    frs_id: dict[str, str] = field(default_factory=dict)
    cwns_id: dict[str, str] = field(default_factory=dict)
    tceq_additional_id: dict[str, str] = field(default_factory=dict)
    tceq_rn: dict[str, str] = field(default_factory=dict)
    nc_permit_number: dict[str, str] = field(default_factory=dict)
    nc_facility_id: dict[str, str] = field(default_factory=dict)
    nc_septage_permit: dict[str, str] = field(default_factory=dict)

    def _bucket(self, field_name: str) -> dict[str, str]:
        return getattr(self, field_name)

    def lookup(self, raw: NormalizedRaw) -> IdMatchResult:
        """Walk ID_PRECEDENCE on the raw. First ID with a registry hit wins.
        Detect conflicts (different canonicals for different IDs on same
        raw) and report them as `conflict=True`; the first hit is still
        returned."""
        winning_canonical = None
        winning_field = None
        for field_name in ID_PRECEDENCE:
            value = getattr(raw, field_name)
            if not value:
                continue
            mapped = self._bucket(field_name).get(value)
            if mapped is None:
                continue
            if winning_canonical is None:
                winning_canonical = mapped
                winning_field = field_name
            elif mapped != winning_canonical:
                return IdMatchResult(
                    canonical_id=winning_canonical,
                    matched_field=winning_field,
                    conflict=True,
                )
        return IdMatchResult(
            canonical_id=winning_canonical,
            matched_field=winning_field,
            conflict=False,
        )

    def register(self, raw: NormalizedRaw, canonical_id: str) -> None:
        """Stash every ID on `raw` into the corresponding registry bucket.
        Idempotent: re-registering the same (id, canonical) is a no-op;
        re-registering with a DIFFERENT canonical is silently dropped (the
        first registration wins, matching the conflict-detection policy)."""
        for field_name in ID_PRECEDENCE:
            value = getattr(raw, field_name)
            if not value:
                continue
            bucket = self._bucket(field_name)
            if value not in bucket:
                bucket[value] = canonical_id

    def size(self) -> dict[str, int]:
        """Summary for the run report."""
        return {field_name: len(self._bucket(field_name)) for field_name in ID_PRECEDENCE}
