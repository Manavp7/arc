# spatial (M6)

Where things are, and when that changes.

```
entities ──► spatial ──► events        (zone_entered / zone_exited / unauthorized_entry)
                     └─► entities      (located_in, a bitemporal edge)
```

This service is the **single authority on site geometry**. Nothing else does point-in-polygon, so
there is exactly one implementation to be right or wrong about.

## Hysteresis, or why this is not a one-line rule

The obvious implementation — "inside the polygon now, outside before, so emit `zone_entered`" — is
unusable, and it fails in a way that never shows up in a demo. A GPS fix carries a couple of metres of
error and a camera-derived position rather more; an entity parked *on* a boundary reports inside,
outside, inside, outside indefinitely. That is not a truck entering a dock forty times a minute, but it
is exactly what the events table would say, and every rule downstream — dwell, unauthorised entry,
occupancy — would inherit the nonsense.

So membership uses hysteresis, for the same reason a thermostat does:

| | rule | why |
|---|---|---|
| **entry** | inside by > 2 m, held for 2 s | a vehicle clipping a corner while turning has not entered |
| **exit** | outside by > 2 m for 15 s | a dropped fix or an occluded camera is not a departure |

The asymmetry is deliberate. **A false exit is worse than a late one:** it closes the dwell clock, ends
the bitemporal edge, and can fire a "left without authorisation" rule on a truck that never moved.

A provisional entry that goes clearly outside is discarded *immediately*, without the grace period —
the grace protects confirmed memberships, and applying it to unconfirmed ones would keep a
corner-clipping vehicle pencilled in, so a second clip within the window would confirm an entry
back-dated to the first and assert a dwell that never happened.

Two smaller decisions in the same spirit:

- **An entry is timestamped when it happened, not when it was confirmed.** The confirmation delay is an
  artefact of how we decide; letting it leak in would make every dwell measurement short by exactly the
  confirmation window — a systematic bias, which is worse than noise because averaging cannot remove it.
- **An entity that goes silent has its memberships closed at its last confirmed sighting.** It has not
  necessarily left, but leaving them open forever makes occupancy drift upward permanently, and the
  last time we actually knew is the honest thing to record.

## Two facts, not one

Every confirmed transition is asserted twice, because the two answer different questions:

- an **event** (`zone_entered` / `zone_exited`, or `unauthorized_entry` for a restricted zone) —
  something *happened*, append-only, with an explanation naming the geometry and the thresholds;
- a **`located_in` relationship**, opened on entry and closed on exit — something *is true* for an
  interval.

"What happened at 14:32?" reads events. "Where was the truck at 14:32?" reads the edge. Deriving either
from the other after the fact is possible and miserable, and the edge is what makes UC5 replay work.

If an exit arrives with no open edge (the entry predates this process), the edge is skipped rather than
invented — fabricating a start time would corrupt the very history bitemporal storage exists to protect.

## Two implementations that must agree

Membership on the hot path is decided in memory: zone polygons are loaded into shapely with an R-tree,
because a PostGIS round trip per entity update is a query per entity per second. PostGIS remains the
source of truth for ad-hoc queries, and `GET /spatial/contains/{zone}` returns **both** answers plus
whether they agree — when they differ it is nearly always an entity mid-confirmation on a boundary, and
hiding that would make a puzzling timeline impossible to explain.

Nested zones return innermost first (a restricted cage inside a yard), ordered by area. Returning just
one would make "is this person in a restricted area?" depend on insertion order.

## H3

Zones answer "how busy is the dock". H3 cells answer "where *exactly* in the yard do people cluster",
without pre-defining a zone for every question. H3 rather than a raw lat/lon grid because its cells
have uniform area and six equidistant neighbours, so "expand by one ring" means something at any
latitude.

Resolution 12 (~307 m², ~19 m across) is chosen so a cell holds roughly one vehicle. Coarser puts the
whole dock apron in one bucket and answers nothing; finer scatters a single truck across a dozen cells
and turns every count into noise.

## Coverage and blind spots

A camera's field of view is modelled as a circular **sector**, not a triangle. A triangle understates
the far edge of a wide lens by the difference between a chord and an arc — about 8 % of range at 70° —
which fabricates blind spots that do not exist.

`GET /spatial/blind_spots` returns the site area minus the union of every footprint, as both a fraction
and polygons, because "83 % covered" tells an operator nothing about *where to walk*.

## Endpoints

| | |
|---|---|
| `GET /spatial` | membership stats, including how much the hysteresis is suppressing |
| `GET /spatial/zones` | zones with confirmed occupancy and over-capacity flags |
| `GET /spatial/within?lat&lon&radius_m&entity_type` | "trucks within 500 m" |
| `GET /spatial/nearest?lat&lon&entity_type` | "nearest hospital" (KNN, index-ordered) |
| `GET /spatial/contains/{zone_id}` | occupants per PostGIS *and* per the tracker, with agreement |
| `GET /spatial/zones_at?lat&lon` | which zones contain a point, innermost first, both ways |
| `GET /spatial/coverage/{source_id}` | what a camera can see |
| `GET /spatial/cameras_covering/{zone_id}` | "cameras covering Gate B" |
| `GET /spatial/blind_spots` | where nothing can see |
| `GET /spatial/density?resolution` | entity counts per H3 cell |
| `GET /spatial/membership/{entity_id}` | which zones an entity is in, and for how long |
