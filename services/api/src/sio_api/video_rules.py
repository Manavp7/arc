"""Bounded image-space rules for recorded footage; no geolocation or identity inference."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ZoneInput(BaseModel):
    zone_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    points: list[tuple[float, float]] = Field(min_length=3, max_length=20)

    @field_validator("points")
    @classmethod
    def valid_polygon(cls, points):
        if any(not math.isfinite(v) or v < 0 or v > 1 for point in points for v in point):
            raise ValueError("Zone coordinates must be finite values from 0 to 1")
        if len(set(points)) != len(points):
            raise ValueError("Zone vertices must be distinct; do not repeat the closing point")
        area = abs(
            sum(
                a[0] * b[1] - b[0] * a[1]
                for a, b in zip(points, points[1:] + points[:1], strict=True)
            )
        )
        if area < 0.0002:
            raise ValueError("Zone must have a visible area")
        for i, a in enumerate(points):
            b = points[(i + 1) % len(points)]
            for j in range(i + 2, len(points)):
                if i == 0 and j == len(points) - 1:
                    continue
                c, d = points[j], points[(j + 1) % len(points)]
                if segments_cross(a, b, c, d):
                    raise ValueError("Zone edges must not intersect")
        return points


def segments_cross(a, b, c, d):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    # Orientation is zero for every collinear pair, including separated segments.
    # Their extents must also overlap before touching can count as intersection.
    for axis in (0, 1):
        if max(min(a[axis], b[axis]), min(c[axis], d[axis])) > min(
            max(a[axis], b[axis]), max(c[axis], d[axis])
        ):
            return False
    return side(a, b, c) * side(a, b, d) <= 0 and side(c, d, a) * side(c, d, b) <= 0


class RuleInput(BaseModel):
    rule_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    zone_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    event_type: Literal["entry", "dwell"]
    target_class: str = Field(default="any", min_length=1, max_length=64)
    threshold_s: float = Field(default=2, ge=0, le=180, allow_inf_nan=False)
    cooldown_s: float = Field(default=5, ge=0, le=180, allow_inf_nan=False)
    enabled: bool = True


class ReviewConfiguration(BaseModel):
    zones: list[ZoneInput] = Field(default_factory=list, max_length=12)
    rules: list[RuleInput] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def linked_rules(self):
        ids = {zone.zone_id for zone in self.zones}
        if len(ids) != len(self.zones) or len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("Zone and rule IDs must be unique")
        if any(rule.zone_id not in ids for rule in self.rules):
            raise ValueError("Every rule must reference a configured zone")
        return self


class ConfigurationUpdate(ReviewConfiguration):
    revision: int = Field(ge=1)


def contains(point, polygon):
    """Ray crossing, with polygon boundaries considered inside."""
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
        if (
            abs(cross) < 1e-9
            and min(a[0], b[0]) <= x <= max(a[0], b[0])
            and min(a[1], b[1]) <= y <= max(a[1], b[1])
        ):
            return True
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def evaluate_rules(
    analysis: dict[str, Any], configuration: ReviewConfiguration
) -> list[dict[str, Any]]:
    """Evaluate bottom-centre box occupancy; entry includes first observation inside.

    Dwell needs observed continuity: a gap over 1.1 seconds ends a visit. Local
    track IDs describe region association, never a person's identity.
    """
    zones = {zone.zone_id: zone for zone in configuration.zones}
    events = []
    for rule in configuration.rules:
        if not rule.enabled:
            continue
        state: dict[str, dict[str, Any]] = {}
        last_event: dict[str, float] = {}
        for frame in analysis.get("detections", []):
            at = frame["at_s"]
            seen = set()
            for obj in frame["objects"]:
                if rule.target_class not in ("any", obj["class_name"]):
                    continue
                tid = obj["track_id"]
                seen.add(tid)
                box = obj["bbox"]
                inside = contains(((box[0] + box[2]) / 2, box[3]), zones[rule.zone_id].points)
                previous = state.get(tid)
                if not inside:
                    state.pop(tid, None)
                    continue
                if previous is None or at - previous["last"] > 1.1:
                    previous = {"start": at, "last": at, "emitted": False}
                    state[tid] = previous
                previous["last"] = at
                elapsed = at - previous["start"]
                if previous["emitted"] or (
                    rule.event_type == "dwell" and elapsed + 1e-6 < rule.threshold_s
                ):
                    continue
                if at - last_event.get(tid, -math.inf) < rule.cooldown_s:
                    continue
                previous["emitted"] = True
                last_event[tid] = at
                fingerprint = json.dumps(
                    [
                        analysis["analysis_id"],
                        rule.model_dump(),
                        zones[rule.zone_id].model_dump(),
                        tid,
                        previous["start"],
                    ],
                    sort_keys=True,
                )
                events.append(
                    {
                        "event_id": "ve_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
                        "video_id": analysis["video_id"],
                        "analysis_id": analysis["analysis_id"],
                        "source": "recorded_file",
                        "event_type": rule.event_type,
                        "at_s": at,
                        "end_s": at,
                        "started_at_s": previous["start"],
                        "zone_id": rule.zone_id,
                        "rule_id": rule.rule_id,
                        "title": f"{rule.name}: {obj['class_name']} observed in {zones[rule.zone_id].name}",
                        "track_id": tid,
                        "confidence": obj["confidence"],
                        "frame_url": frame["frame_url"],
                    }
                )
            for tid in list(state):
                if tid not in seen and at - state[tid]["last"] > 1.1:
                    state.pop(tid)
    return sorted(events, key=lambda event: (event["at_s"], event["event_id"]))[:500]
