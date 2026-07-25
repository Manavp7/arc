"""Zone heatmaps by H3 aggregation (PRD M19, Phase 6).

Aggregating positions into hexagons before they reach the browser, rather than shipping every observation and
letting deck.gl bin them.

**The reason is not rendering cost, it is privacy.** A heatmap built client-side needs every individual
position, so the browser receives a complete movement record for every person on site — which the platform then
has no way to redact, because redaction happens at the API boundary and the boundary has already been crossed.
Aggregation on the server means the browser receives counts per hexagon and cannot reconstruct a track.

That has a corollary worth stating: **a hexagon containing one person is not aggregated, it is a location**. So
cells below a minimum count are suppressed, which is the standard disclosure control and is the difference
between a heatmap and a surveillance tool.

H3 rather than a square grid because hexagons have uniform adjacency — every neighbour is the same distance
away — so a diffusion or a nearest-cell question does not depend on which direction it is asked in. Squares
have two different neighbour distances and produce visible artefacts along diagonals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Default H3 resolution for a yard-scale heatmap.
#:
#: Resolution 11 has an edge length of about 25 m, so a cell is roughly a truck bay. Resolution 12 (the
#: platform's `h3_resolution` default for indexing) is ~9 m, which is finer than any position is accurate and
#: produces a heatmap of measurement noise. Indexing precision and display precision are different questions
#: and deliberately have different answers.
DISPLAY_RESOLUTION = 11

#: Cells with fewer than this many distinct entities are suppressed.
#:
#: Five is the conventional floor for small-cell disclosure control. It is not a rendering decision: a hexagon
#: containing one person is not an aggregate, it is that person's location, and shipping it to a browser hands
#: out a movement record the API can no longer redact.
MIN_CELL_COUNT = 5


@dataclass
class HexCell:
    """One aggregated cell."""

    h3_index: str
    lat: float
    lon: float
    observations: int
    entities: int
    zone_id: str | None = None
    types: dict[str, int] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "h3": self.h3_index,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "observations": self.observations,
            "entities": self.entities,
            "zone_id": self.zone_id,
            "types": self.types,
        }


@dataclass
class Heatmap:
    """Cells, plus an honest account of what was left out."""

    resolution: int
    cells: list[HexCell] = field(default_factory=list)
    suppressed_cells: int = 0
    suppressed_observations: int = 0
    total_observations: int = 0

    def describe(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution,
            "edge_length_m": edge_length_m(self.resolution),
            "cells": [cell.describe() for cell in self.cells],
            "total_observations": self.total_observations,
            # Reported, not hidden. A heatmap that silently drops 40 % of its data looks like a quiet site,
            # and an operator comparing it against the entity count will conclude the map is broken — which is
            # a better outcome than believing it, but worse than being told.
            "suppressed": {
                "cells": self.suppressed_cells,
                "observations": self.suppressed_observations,
                "why": (
                    f"cells with fewer than {MIN_CELL_COUNT} distinct entities are withheld: a hexagon "
                    f"containing one person is that person's location, not an aggregate"
                ),
            },
            "max_observations": max((cell.observations for cell in self.cells), default=0),
        }


def edge_length_m(resolution: int) -> float:
    """Approximate hexagon edge length, for labelling the map.

    From H3's published table rather than computed, because the exact value varies slightly with latitude and a
    label reading "about 25 m" is more useful than one reading "24.87 m" that is wrong at the poles.
    """
    return {7: 1220.0, 8: 461.0, 9: 174.0, 10: 65.9, 11: 24.9, 12: 9.4, 13: 3.6}.get(
        resolution, 0.0
    )


def aggregate(
    positions: list[dict[str, Any]],
    *,
    resolution: int = DISPLAY_RESOLUTION,
    min_count: int = MIN_CELL_COUNT,
) -> Heatmap:
    """Bin positions into H3 cells, suppressing cells too small to be aggregates.

    `positions` is a list of `{lat, lon, entity_id, type, zone_id}`. Distinct **entities** are counted for the
    suppression threshold rather than observations: a hundred observations of one truck is still one truck, and
    counting observations would let a stationary vehicle unlock a cell that discloses only itself.
    """
    try:
        import h3
    except ImportError:
        # No h3, no heatmap — and an empty one that says why beats an exception that reaches an operator as a
        # 500 on a dashboard tile.
        return Heatmap(resolution=resolution, total_observations=len(positions))

    buckets: dict[str, dict[str, Any]] = {}
    for position in positions:
        lat, lon = position.get("lat"), position.get("lon")
        if lat is None or lon is None:
            continue
        try:
            index = h3.latlng_to_cell(float(lat), float(lon), resolution)
        except (ValueError, TypeError):
            continue
        bucket = buckets.setdefault(
            index,
            {"observations": 0, "entities": set(), "types": {}, "zone_id": position.get("zone_id")},
        )
        bucket["observations"] += 1
        if position.get("entity_id"):
            bucket["entities"].add(position["entity_id"])
        kind = str(position.get("type") or "unknown")
        bucket["types"][kind] = bucket["types"].get(kind, 0) + 1

    heatmap = Heatmap(resolution=resolution, total_observations=len(positions))
    for index, bucket in buckets.items():
        entities = len(bucket["entities"])
        if entities < min_count:
            heatmap.suppressed_cells += 1
            heatmap.suppressed_observations += bucket["observations"]
            continue
        lat, lon = h3.cell_to_latlng(index)
        heatmap.cells.append(
            HexCell(
                h3_index=index,
                lat=lat,
                lon=lon,
                observations=bucket["observations"],
                entities=entities,
                zone_id=bucket["zone_id"],
                types=dict(sorted(bucket["types"].items(), key=lambda pair: -pair[1])),
            )
        )
    # Busiest first, so a client rendering a subset renders the part that matters.
    heatmap.cells.sort(key=lambda cell: -cell.observations)
    return heatmap


__all__ = [
    "DISPLAY_RESOLUTION",
    "MIN_CELL_COUNT",
    "Heatmap",
    "HexCell",
    "aggregate",
    "edge_length_m",
]
