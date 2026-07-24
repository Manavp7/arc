"""Cross-camera association: is the truck at Gate A the truck now at Dock 3?

The PRD lists cross-camera tracking as stubbed in the MVP, with DeepStream MV3DT as the GPU answer
(M4, R3). This is a real but deliberately **conservative** implementation, because the failure modes
are asymmetric: a missed link costs a little (two tracks that fusion may later merge on position),
while a wrong link costs a lot (two different vehicles fused into one entity, whose journey history is
then fiction).

So a link requires three things to agree:

1. **appearance** — cosine similarity above ``reid_threshold`` on the smoothed ReID vectors;
2. **time** — the two sightings within a plausible transit window; a truck cannot be at both ends of
   the yard in the same second, and one seen an hour apart is a different visit;
3. **class** — the same detected class, which is nearly free and rules out obvious nonsense.

Links are proposed as ``same_as`` hypotheses with a confidence, not asserted as fact. Fusion decides
what to do with them, which is where positional evidence also lives.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sio_core import get_logger
from sio_schemas import Track as TrackEnvelope

from .bytetrack import Track as InternalTrack

log = get_logger("sio.tracking.crosscam")


@dataclass
class Sighting:
    """One camera's view of one object, kept for cross-camera comparison."""

    source_id: str
    track_id: str
    label: str
    embedding: np.ndarray
    last_seen: float
    hits: int

    def age(self, now: float) -> float:
        return now - self.last_seen


@dataclass
class Link:
    """A proposed identity link between two camera-local tracks."""

    left: str
    right: str
    similarity: float
    seconds_apart: float
    created_at: float = field(default_factory=time.monotonic)

    def describe(self) -> dict[str, Any]:
        return {
            "tracks": [self.left, self.right],
            "similarity": round(self.similarity, 3),
            "seconds_apart": round(self.seconds_apart, 1),
        }


class CrossCameraAssociator:
    """Proposes ``same_as`` links between tracks on different cameras."""

    MIN_HITS = 5
    """Do not offer a track for cross-camera matching until its appearance vector has settled.

    An embedding smoothed over one or two frames is dominated by whichever crop happened to be first,
    and matching on it produces confident nonsense.
    """

    MAX_TRANSIT_S = 300.0
    """Beyond this, two sightings are a different visit rather than the same journey."""

    MIN_TRANSIT_S = 1.0
    """Below this, the same object cannot plausibly be in two camera views — it is a double-count."""

    def __init__(
        self, *, reid_threshold: float = 0.75, enabled: bool = True, max_sightings: int = 500
    ) -> None:
        self.reid_threshold = reid_threshold
        self.enabled = enabled
        self.max_sightings = max_sightings
        self.sightings: dict[str, Sighting] = {}
        self.links: list[Link] = []
        self._proposed: set[frozenset[str]] = set()
        """Pairs already proposed, so a persistent track does not re-propose every frame."""
        self.link_count = 0
        self.duplicate_proposals = 0
        self.rejected_time = 0
        self.rejected_similarity = 0

    def observe(
        self, source_id: str, internal: InternalTrack, envelope: TrackEnvelope
    ) -> list[str]:
        """Record a track and return the ids of tracks on *other* cameras believed to be the same."""
        if not self.enabled or internal.embedding is None or internal.hits < self.MIN_HITS:
            return []

        now = time.monotonic()
        self._evict(now)

        vector = np.asarray(internal.embedding, dtype=np.float32)
        matches: list[str] = []

        for sighting in self.sightings.values():
            if sighting.source_id == source_id:
                continue  # same camera: that is ByteTrack's job, not this one's
            if sighting.label != internal.label:
                continue
            apart = abs(sighting.age(now))
            if apart < self.MIN_TRANSIT_S or apart > self.MAX_TRANSIT_S:
                self.rejected_time += 1
                continue
            similarity = float(np.dot(vector, sighting.embedding))
            if similarity < self.reid_threshold:
                self.rejected_similarity += 1
                continue
            matches.append(sighting.track_id)

            # Propose each pair once. A persistent track re-observed every frame would otherwise
            # re-propose the same hypothesis on every frame: 16 sightings produced 714 "links",
            # which is not evidence, it is noise that fusion has to filter. The *match* is still
            # returned every time, so the track's `cross_camera_of` stays populated — only the
            # proposal is deduplicated.
            pair = frozenset((envelope.track_id, sighting.track_id))
            if pair in self._proposed:
                continue
            self._proposed.add(pair)

            link = Link(
                left=envelope.track_id,
                right=sighting.track_id,
                similarity=similarity,
                seconds_apart=apart,
            )
            self.links.append(link)
            self.link_count += 1
            log.info(
                "crosscam.link",
                left=link.left,
                right=link.right,
                similarity=round(similarity, 3),
                seconds_apart=round(apart, 1),
                note="proposed as a same_as hypothesis; fusion decides",
            )

        self.sightings[envelope.track_id] = Sighting(
            source_id=source_id,
            track_id=envelope.track_id,
            label=internal.label,
            embedding=vector,
            last_seen=now,
            hits=internal.hits,
        )
        return matches

    def _evict(self, now: float) -> None:
        """Drop sightings older than the transit window, and cap the table."""
        stale = [
            key
            for key, sighting in self.sightings.items()
            if sighting.age(now) > self.MAX_TRANSIT_S
        ]
        for key in stale:
            del self.sightings[key]
        if len(self.sightings) > self.max_sightings:
            oldest = sorted(self.sightings.items(), key=lambda item: item[1].last_seen)
            for key, _ in oldest[: len(self.sightings) - self.max_sightings]:
                del self.sightings[key]
        # Links are a diagnostic, not a store; the world model holds the real relationships.
        if len(self.links) > 200:
            del self.links[:-200]
        # Forget proposals for tracks that have aged out, so a returning vehicle can be linked again
        # on a later visit rather than being suppressed forever by a set that only grows.
        if len(self._proposed) > 5_000:
            live = set(self.sightings)
            self._proposed = {
                pair for pair in self._proposed if any(member in live for member in pair)
            }

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reid_threshold": self.reid_threshold,
            "tracked_sightings": len(self.sightings),
            "links_proposed": self.link_count,
            "distinct_pairs_tracked": len(self._proposed),
            "rejected_on_time": self.rejected_time,
            "rejected_on_similarity": self.rejected_similarity,
            "recent_links": [link.describe() for link in self.links[-10:]],
            "note": (
                "links are same_as hypotheses with a confidence, not assertions. A missed link costs "
                "little; a wrong one fuses two vehicles into one entity with a fictional journey."
            ),
        }

    def candidates_for(self, label: str, exclude_source: str) -> Iterable[Sighting]:
        return (
            sighting
            for sighting in self.sightings.values()
            if sighting.label == label and sighting.source_id != exclude_source
        )
