"""Bounded, read-only summaries of observations retained by one recorded analysis.

Track IDs are local geometric associations, not identities. Counts describe saved
samples; gaps and rejected observations never establish an unobserved crossing.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any
from urllib.parse import quote

from .video_rules import ZoneInput, contains, segments_cross

MAX_SAMPLES = 360
MAX_OBJECTS = 32
MAX_GAP_S = 1.1
LINE_TOLERANCE = 0.005


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _text(value: Any, limit: int = 256) -> str:
    return value if isinstance(value, str) and len(value) <= limit else ""


def _frame_url(video: dict, analysis: dict, index: int) -> str:
    video_id = quote(_text(video.get("video_id")), safe="")
    analysis_id = quote(_text(analysis.get("analysis_id")), safe="")
    return f"/api/review/videos/{video_id}/frames/{analysis_id}/{index}"


def _zones(analysis: dict) -> list[dict]:
    """Only the configuration frozen in this analysis can describe its zones."""
    result: list[dict[str, Any]] = []
    seen = set()
    source = analysis.get("zones", [])
    if not isinstance(source, list):
        return result
    for value in source[:12]:
        if not isinstance(value, dict):
            continue
        try:
            zone = ZoneInput.model_validate(value)
        except (ValueError, TypeError):
            continue
        if zone.zone_id not in seen:
            result.append(zone.model_dump())
            seen.add(zone.zone_id)
    return result


def _objects(values: list) -> list[dict] | None:
    """Reject malformed samples and deduplicate a track/class within one sample.

    Discarding only a malformed object could turn corrupted detections into a
    false zero or an understated occupancy, so the whole sample becomes unknown.
    """
    result: dict[tuple[str, str], dict] = {}
    for value in values[:MAX_OBJECTS]:
        if not isinstance(value, dict):
            return None
        track = _text(value.get("track_id"), 128)
        label = _text(value.get("class_name"), 64)
        box = value.get("bbox")
        if (
            not track.strip()
            or not label.strip()
            or not isinstance(box, (list, tuple))
            or len(box) != 4
        ):
            return None
        box = [_number(number) for number in box]
        if any(number is None or not 0 <= number <= 1 for number in box):
            return None
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        confidence = _number(value.get("confidence"))
        if label == "motion" or confidence is None or not 0 <= confidence <= 1:
            confidence = None
        obj = {
            "track_id": track,
            "class_name": label,
            "confidence": confidence,
            "bbox": box,
        }
        key = (track, label)
        previous = result.get(key)
        # Highest known score wins; coordinates break ties independently of input order.
        rank = (-(confidence if confidence is not None else -1), tuple(box))
        if previous is None or rank < (
            -(previous["confidence"] if previous["confidence"] is not None else -1),
            tuple(previous["bbox"]),
        ):
            result[key] = obj
    return [result[key] for key in sorted(result)]


def _samples(video: dict, analysis: dict) -> list[dict]:
    source = analysis.get("detections", [])
    if not isinstance(source, list):
        return []
    samples = []
    duration = _number(video.get("duration_s"))
    seen_indices = set()
    seen_times = set()
    for value in source[:MAX_SAMPLES]:
        if not isinstance(value, dict):
            continue
        at = _number(value.get("at_s"))
        index = value.get("frame_index")
        if (
            at is None
            or at < 0
            or (duration is not None and duration >= 0 and at > duration)
            or type(index) is not int
            or not 0 <= index < MAX_SAMPLES
            or index in seen_indices
            or at in seen_times
            or not isinstance(value.get("objects"), list)
        ):
            continue
        objects = _objects(value["objects"])
        if objects is None:
            continue
        samples.append(
            {
                "at_s": at,
                "frame_index": index,
                "frame_url": _frame_url(video, analysis, index),
                "objects": objects,
            }
        )
        seen_indices.add(index)
        seen_times.add(at)
    return sorted(samples, key=lambda sample: (sample["at_s"], sample["frame_index"]))


def _memberships(obj: dict, zones: list[dict]) -> list[dict]:
    box = obj["bbox"]
    point = ((box[0] + box[2]) / 2, box[3])
    return [zone for zone in zones if contains(point, zone["points"])]


def _matches(obj: dict, zones: list[dict], label: str, zone_id: str, threshold: float | None):
    if label and obj["class_name"] != label:
        return False
    if threshold is not None and (obj["confidence"] is None or obj["confidence"] < threshold):
        return False
    return not zone_id or any(zone["zone_id"] == zone_id for zone in _memberships(obj, zones))


def object_tracks(
    video: dict,
    analysis: dict,
    *,
    class_name: str = "",
    zone_id: str = "",
    min_confidence: float | None = None,
    start_s: float = 0,
    end_s: float | None = None,
) -> list[dict]:
    """Group matching observations without extending a track beyond the filters."""
    start = _number(start_s)
    end = _number(end_s) if end_s is not None else None
    threshold = _number(min_confidence) if min_confidence is not None else None
    if (
        start is None
        or start < 0
        or (end_s is not None and (end is None or end < start))
        or (min_confidence is not None and (threshold is None or not 0 <= threshold <= 1))
    ):
        return []
    zones = _zones(analysis)
    tracks: dict[tuple[str, str], dict] = {}
    for sample in _samples(video, analysis):
        at = sample["at_s"]
        if at < start or (end is not None and at > end):
            continue
        for obj in sample["objects"]:
            if not _matches(obj, zones, class_name, zone_id, threshold):
                continue
            key = (obj["track_id"], obj["class_name"])
            if key not in tracks:
                tracks[key] = {
                    "video_id": _text(video.get("video_id")),
                    "video_title": _text(video.get("title"), 1000),
                    "analysis_id": _text(analysis.get("analysis_id")),
                    "track_id": obj["track_id"],
                    "class_name": obj["class_name"],
                    "first_s": at,
                    "last_s": at,
                    "at_s": at,
                    "sample_count": 0,
                    "confidence_min": None,
                    "confidence_max": None,
                    "frame_url": sample["frame_url"],
                    "zone_ids": [],
                    "zone_names": [],
                    "observations": [],
                }
            track = tracks[key]
            track["last_s"] = at
            track["sample_count"] += 1
            confidence = obj["confidence"]
            if confidence is not None:
                track["confidence_min"] = (
                    confidence
                    if track["confidence_min"] is None
                    else min(track["confidence_min"], confidence)
                )
                track["confidence_max"] = (
                    confidence
                    if track["confidence_max"] is None
                    else max(track["confidence_max"], confidence)
                )
            track["observations"].append(
                {
                    "at_s": at,
                    "frame_index": sample["frame_index"],
                    "frame_url": sample["frame_url"],
                    "confidence": confidence,
                    "bbox": obj["bbox"],
                }
            )
            for zone in _memberships(obj, zones):
                if zone["zone_id"] not in track["zone_ids"]:
                    track["zone_ids"].append(zone["zone_id"])
                    track["zone_names"].append(zone["name"])
    return sorted(
        tracks.values(),
        key=lambda track: (track["first_s"], track["class_name"], track["track_id"]),
    )


def _line(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    points = []
    for name in ("start", "end"):
        point = value.get(name)
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        point = [_number(coordinate) for coordinate in point]
        if any(coordinate is None or not 0 <= coordinate <= 1 for coordinate in point):
            return None
        points.append(point)
    if math.dist(*points) <= 1e-9:
        return None
    return {"name": _text(value.get("name"), 100), "start": points[0], "end": points[1]}


def _signed_distance(point: tuple, line: dict) -> float:
    start, end = line["start"], line["end"]
    return (
        (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])
    ) / math.dist(start, end)


def _side(distance: float, tolerance: float) -> int:
    # Image y increases downward: negative is visually left (A), positive right (B).
    if abs(distance) <= tolerance + 1e-12:
        return 0
    return -1 if distance < 0 else 1


def movement_summary(video: dict, analysis: dict, configuration: dict) -> dict:
    """Sampled occupancy and directed finite-line crossings of local tracks."""
    line = _line(configuration.get("line"))
    label = _text(configuration.get("class_name", ""), 64)
    zone_id = _text(configuration.get("zone_id", ""), 64)
    raw_threshold = configuration.get("min_confidence")
    threshold = _number(raw_threshold) if raw_threshold is not None else None
    valid_threshold = raw_threshold is None or (threshold is not None and 0 <= threshold <= 1)
    zones = _zones(analysis)
    samples = _samples(video, analysis)
    occupancy = []
    crossings = []
    state: dict[tuple[str, str], dict] = {}
    counts: Counter = Counter()
    class_tracks: dict[str, set] = {}
    class_peaks: Counter = Counter()
    for sample in samples:
        objects = [
            obj
            for obj in sample["objects"]
            if valid_threshold and _matches(obj, zones, label, zone_id, threshold)
        ]
        occupancy.append(
            {key: sample[key] for key in ("at_s", "frame_index", "frame_url")}
            | {"count": len(objects)}
        )
        current_counts = Counter(obj["class_name"] for obj in objects)
        counts.update(current_counts)
        for name, count in current_counts.items():
            class_peaks[name] = max(class_peaks[name], count)
        next_state = {}
        for obj in objects:
            key = (obj["track_id"], obj["class_name"])
            class_tracks.setdefault(obj["class_name"], set()).add(obj["track_id"])
            if line is None:
                continue
            box = obj["bbox"]
            point = ((box[0] + box[2]) / 2, box[3])
            distance = _signed_distance(point, line)
            side = _side(distance, LINE_TOLERANCE)
            raw_side = _side(distance, 0)
            previous = state.get(key)
            if previous is not None and (
                sample["frame_index"] != previous["frame_index"] + 1
                or not 0 < sample["at_s"] - previous["at_s"] <= MAX_GAP_S
            ):
                previous = None
            stable_side = previous["stable_side"] if previous else side
            from_s = previous["from_s"] if previous else sample["at_s"]
            balance = previous["balance"] if previous else 0
            last_raw_side = previous["last_raw_side"] if previous else raw_side
            touches_line = previous["touches_line"] if previous else False
            if previous:
                intersects = segments_cross(previous["point"], point, line["start"], line["end"])
                if raw_side == 0:
                    touches_line |= intersects
                else:
                    # Tangential touches do not cross. Exact on-line samples can bridge
                    # opposite raw sides, and a return within the band cancels its crossing.
                    if last_raw_side and raw_side != last_raw_side and (touches_line or intersects):
                        balance += raw_side
                    touches_line = False
                    last_raw_side = raw_side
            if side:
                if stable_side and side != stable_side and balance * side > 0:
                    crossings.append(
                        {
                            "at_s": sample["at_s"],
                            "from_s": from_s,
                            "to_s": sample["at_s"],
                            "direction": "a_to_b" if stable_side == -1 else "b_to_a",
                            "track_id": obj["track_id"],
                            "class_name": obj["class_name"],
                            "frame_url": sample["frame_url"],
                            "frame_index": sample["frame_index"],
                        }
                    )
                stable_side, from_s, balance = side, sample["at_s"], 0
            next_state[key] = {
                "point": point,
                "at_s": sample["at_s"],
                "frame_index": sample["frame_index"],
                "stable_side": stable_side,
                "from_s": from_s,
                "balance": balance,
                "last_raw_side": last_raw_side,
                "touches_line": touches_line,
            }
        # A missing or filtered observation ends continuity even within the time limit.
        state = next_state
    peak = max((point["count"] for point in occupancy), default=0)
    totals = {
        direction: sum(row["direction"] == direction for row in crossings)
        for direction in ("a_to_b", "b_to_a")
    }
    totals["total"] = len(crossings)
    return {
        "schema_version": 1,
        "video_id": _text(video.get("video_id")),
        "analysis_id": _text(analysis.get("analysis_id")),
        "configuration": {
            "line": line,
            "class_name": label,
            "min_confidence": threshold if valid_threshold else None,
            "zone_id": zone_id,
        },
        "sample_count": len(samples),
        "coverage": {
            "start_s": samples[0]["at_s"] if samples else None,
            "end_s": samples[-1]["at_s"] if samples else None,
        },
        "occupancy": occupancy,
        "peak_count": peak,
        "peak_at_s": next((point["at_s"] for point in occupancy if point["count"] == peak), None),
        "mean_sampled_occupancy": sum(point["count"] for point in occupancy) / len(occupancy)
        if occupancy
        else None,
        "class_counts": [
            {
                "class_name": name,
                "observation_count": count,
                "track_count": len(class_tracks[name]),
                "peak_count": class_peaks[name],
            }
            for name, count in sorted(counts.items())
        ],
        "crossings": crossings,
        "totals": totals,
        "limitations": [
            "Occupancy counts matching objects in saved samples, not continuous or verified ground-truth occupancy.",
            "Crossings are observed local-track transitions, not unique people; returning tracks may count again.",
            "Missing or filtered observations, nonconsecutive frames, or gaps over 1.1 seconds break crossing continuity.",
            "Malformed samples are excluded; missing samples are unknown, while valid empty samples count as zero.",
            "Boxes use their bottom centre; a 0.005 normalized-distance band suppresses near-line jitter.",
        ],
    }
