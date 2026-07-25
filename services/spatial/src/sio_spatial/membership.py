"""Zone membership with hysteresis.

The naive version — "if the point is inside the polygon and it was not before, emit `zone_entered`" —
is unusable, and it fails in a way that only appears with real data. A GPS fix carries a couple of
metres of error, a camera-derived position rather more; an entity parked on a zone boundary will
report inside, outside, inside, outside indefinitely. That is not a truck driving in and out of a
dock forty times a minute, but it is exactly what the events table would say, and every rule
downstream (dwell, unauthorised entry, occupancy) would inherit the nonsense.

So membership uses hysteresis, which is the same answer a thermostat uses for the same reason:

* **entry** needs the point to be inside by more than a margin, and to stay inside for a minimum
  time. A vehicle clipping a corner while turning has not entered the dock.
* **exit** needs the point to be outside by more than the same margin, and to stay outside — with a
  *longer* grace period, because a brief loss of position (an occluded camera, a dropped fix) must not
  be reported as a departure. An entity that has genuinely left will stay outside; one whose signal
  flickered will not.

The asymmetry is deliberate. A false exit is worse than a late one: it closes the dwell clock, ends
the bitemporal edge, and can fire a "left without authorisation" rule on a truck that never moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sio_core import get_logger
from sio_schemas import Geo

from .geometry import ZoneIndex

log = get_logger("sio.spatial.membership")


@dataclass
class Membership:
    """One entity's confirmed presence in one zone."""

    entity_id: str
    zone_id: str
    entered_ts: datetime
    last_inside_ts: datetime
    confirmed: bool = False
    """False while the entry is still provisional: seen inside, but not yet long enough."""
    first_inside_ts: datetime | None = None
    outside_since: datetime | None = None
    """When the entity was first seen outside. Reset on any confirmed sighting inside."""

    @property
    def dwell_s(self) -> float:
        return (self.last_inside_ts - self.entered_ts).total_seconds()


@dataclass
class MembershipChange:
    """A confirmed transition, ready to become an event and a bitemporal edge."""

    entity_id: str
    zone_id: str
    kind: str
    """``entered`` or ``exited``."""
    ts: datetime
    dwell_s: float = 0.0
    geo: Geo | None = None
    zone_name: str = ""
    restricted: bool = False


@dataclass
class MembershipTracker:
    """Tracks which entities are in which zones, with hysteresis on both edges."""

    index: ZoneIndex
    margin_m: float = 2.0
    """How far inside (or outside) a boundary a point must be to count. Set from the worst position
    uncertainty we expect to trust — a GPS fix is good to a couple of metres."""
    enter_confirm_s: float = 2.0
    """How long a point must remain inside before entry is asserted. Filters corner-clipping."""
    exit_grace_s: float = 15.0
    """How long a point must remain outside before exit is asserted. Longer than entry on purpose: a
    false exit closes the dwell clock and can fire rules about leaving."""

    memberships: dict[tuple[str, str], Membership] = field(default_factory=dict)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "observations": 0,
            "entered": 0,
            "exited": 0,
            "provisional_entries": 0,
            "entries_discarded": 0,
            "exits_deferred": 0,
        }
    )

    def observe(self, entity_id: str, geo: Geo, ts: datetime) -> list[MembershipChange]:
        """Fold one position into the membership state, returning any confirmed transitions."""
        self.stats["observations"] += 1
        changes: list[MembershipChange] = []

        inside_now: dict[str, float] = {}
        for zone in self.index.zones_containing(geo):
            inside_now[zone.zone_id] = zone.distance_to_boundary_m(geo)

        # --- zones the entity is currently inside -------------------------------
        for zone_id, depth in inside_now.items():
            key = (entity_id, zone_id)
            existing = self.memberships.get(key)
            if existing is None:
                if depth < self.margin_m:
                    continue  # inside, but only just: not yet a confident entry
                self.memberships[key] = Membership(
                    entity_id=entity_id,
                    zone_id=zone_id,
                    entered_ts=ts,
                    last_inside_ts=ts,
                    first_inside_ts=ts,
                )
                self.stats["provisional_entries"] += 1
                continue

            existing.last_inside_ts = ts
            existing.outside_since = None
            if not existing.confirmed:
                held_for = (ts - (existing.first_inside_ts or ts)).total_seconds()
                if held_for >= self.enter_confirm_s:
                    existing.confirmed = True
                    zone = self.index.get(zone_id)
                    self.stats["entered"] += 1
                    changes.append(
                        MembershipChange(
                            entity_id=entity_id,
                            zone_id=zone_id,
                            kind="entered",
                            # The entry is timestamped when the entity FIRST went inside, not when
                            # confirmation completed. The confirmation delay is an artefact of how we
                            # decide, and letting it leak into the timestamp would make every dwell
                            # measurement short by the confirmation window.
                            ts=existing.entered_ts,
                            geo=geo,
                            zone_name=zone.name if zone else zone_id,
                            restricted=bool(zone and zone.restricted),
                        )
                    )

        # --- zones the entity was in but is no longer ----------------------------
        for key, membership in list(self.memberships.items()):
            if key[0] != entity_id or membership.zone_id in inside_now:
                continue
            zone = self.index.get(membership.zone_id)
            depth = zone.distance_to_boundary_m(geo) if zone else -999.0
            if depth > -self.margin_m:
                # Outside, but within the margin: treat as still inside rather than flapping.
                membership.last_inside_ts = ts
                continue
            if not membership.confirmed:
                # A provisional entry that has gone clearly outside is discarded at once, without the
                # grace period. The grace exists to protect a *confirmed* membership from a dropped
                # fix; applying it here would keep a corner-clipping vehicle pencilled in for fifteen
                # seconds, and if it clipped the corner again in that window the entry would be
                # confirmed and back-dated to the first clip — asserting a dwell that never happened.
                del self.memberships[key]
                self.stats["entries_discarded"] += 1
                continue

            if membership.outside_since is None:
                membership.outside_since = ts
            if (ts - membership.outside_since).total_seconds() < self.exit_grace_s:
                self.stats["exits_deferred"] += 1
                continue

            del self.memberships[key]
            self.stats["exited"] += 1
            changes.append(
                MembershipChange(
                    entity_id=entity_id,
                    zone_id=membership.zone_id,
                    kind="exited",
                    ts=membership.last_inside_ts,
                    dwell_s=membership.dwell_s,
                    geo=geo,
                    zone_name=zone.name if zone else membership.zone_id,
                    restricted=bool(zone and zone.restricted),
                )
            )
        return changes

    def forget(self, entity_id: str, ts: datetime) -> list[MembershipChange]:
        """Close out an entity that has disappeared entirely.

        An entity that stops being observed has not necessarily left — but it has stopped being
        tracked, and leaving its membership open forever would make occupancy counts drift upward
        permanently. Emitting the exit at the last confirmed sighting keeps the record honest about
        what was actually known.
        """
        changes: list[MembershipChange] = []
        for key, membership in list(self.memberships.items()):
            if key[0] != entity_id:
                continue
            del self.memberships[key]
            if not membership.confirmed:
                continue
            zone = self.index.get(membership.zone_id)
            self.stats["exited"] += 1
            changes.append(
                MembershipChange(
                    entity_id=entity_id,
                    zone_id=membership.zone_id,
                    kind="exited",
                    ts=membership.last_inside_ts,
                    dwell_s=membership.dwell_s,
                    zone_name=zone.name if zone else membership.zone_id,
                    restricted=bool(zone and zone.restricted),
                )
            )
        return changes

    def expire_stale(self, now: datetime, max_silence_s: float = 180.0) -> list[MembershipChange]:
        """Close memberships for entities nothing has reported on for a while."""
        stale_entities = {
            key[0]
            for key, membership in self.memberships.items()
            if (now - membership.last_inside_ts) > timedelta(seconds=max_silence_s)
        }
        changes: list[MembershipChange] = []
        for entity_id in stale_entities:
            changes.extend(self.forget(entity_id, now))
        return changes

    # ------------------------------------------------------------------- queries
    def occupancy(self) -> dict[str, list[str]]:
        """Confirmed occupants per zone. Provisional entries are excluded on purpose."""
        result: dict[str, list[str]] = {}
        for (entity_id, zone_id), membership in self.memberships.items():
            if membership.confirmed:
                result.setdefault(zone_id, []).append(entity_id)
        return result

    def zones_of(self, entity_id: str) -> list[str]:
        return [
            zone_id
            for (candidate, zone_id), membership in self.memberships.items()
            if candidate == entity_id and membership.confirmed
        ]

    def dwell_of(self, entity_id: str, zone_id: str) -> float | None:
        membership = self.memberships.get((entity_id, zone_id))
        return membership.dwell_s if membership and membership.confirmed else None
