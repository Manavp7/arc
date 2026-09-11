"""Deterministic event evaluation restricted to explicitly reviewed label scopes."""

from __future__ import annotations

import hashlib
import json
import statistics
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Interval(BaseModel):
    start_s: float = Field(ge=0, le=180, allow_inf_nan=False)
    end_s: float = Field(gt=0, le=180, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_s <= self.start_s:
            raise ValueError("An interval must end after it starts")
        return self


class Scope(BaseModel):
    zone_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    event_type: Literal["entry", "dwell"]


class Annotation(Interval, Scope):
    annotation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    note: str = Field(default="", max_length=1000)


class AnnotationDraft(BaseModel):
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=120)
    partition: Literal["development", "validation", "held_out"]
    footage_origin: Literal["real", "authored", "unknown"] = "unknown"
    scopes: list[Scope] = Field(min_length=1, max_length=48)
    coverage: list[Interval] = Field(min_length=1, max_length=100)
    annotations: list[Annotation] = Field(default_factory=list, max_length=500)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def consistent(self):
        scopes = {(scope.zone_id, scope.event_type) for scope in self.scopes}
        if len(scopes) != len(self.scopes):
            raise ValueError("Reviewed scopes must be unique")
        if len({a.annotation_id for a in self.annotations}) != len(self.annotations):
            raise ValueError("Annotation IDs must be unique")
        coverage = merge_intervals([item.model_dump() for item in self.coverage])
        for item in self.annotations:
            if (item.zone_id, item.event_type) not in scopes:
                raise ValueError("Every annotation must belong to a reviewed label scope")
            if not any(start <= item.start_s and item.end_s <= end for start, end in coverage):
                raise ValueError("Every annotation interval must be fully inside reviewed coverage")
        return self


class FreezeRequest(BaseModel):
    revision: int = Field(ge=1)


class EvaluationInput(BaseModel):
    annotation_set_id: str = Field(pattern=r"^ann_[a-f0-9]{32}$")
    analysis_ids: list[str] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.analysis_ids)) != len(self.analysis_ids):
            raise ValueError("Choose each analysis version once per clip")
        return self


class EvaluationRequest(BaseModel):
    title: str = Field(default="Recorded footage evaluation", min_length=1, max_length=120)
    inputs: list[EvaluationInput] = Field(min_length=1, max_length=20)
    tolerance_s: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def comparable(self):
        if len({len(item.analysis_ids) for item in self.inputs}) != 1:
            raise ValueError("Every clip must have the same number of candidate versions")
        if len({item.annotation_set_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("Choose one frozen annotation set per clip")
        return self


def merge_intervals(intervals: list[dict]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for item in sorted(intervals, key=lambda row: (row["start_s"], row["end_s"])):
        start, end = item["start_s"], item["end_s"]
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def measures(tp: int, fp: int, fn: int, reviewed_s: float, delays: list[float]) -> dict:
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_alarms_per_reviewed_hour": fp * 3600 / reviewed_s if reviewed_s else None,
        "reviewed_seconds": reviewed_s,
        "median_signed_delay_s": statistics.median(delays) if delays else None,
        "matched_delay_samples": len(delays),
    }


def evaluate(annotation_set: dict, analysis: dict, tolerance_s: float) -> dict:
    """Maximum-cardinality bipartite match with stable closest-first tie order.

    Only emitted events inside certified coverage and matching a reviewed scope
    can be judged. A label is one incident, not a per-frame positive. Prediction
    duplicates remain distinct and unmatched duplicates count as false alarms.
    """
    coverage = merge_intervals(annotation_set["coverage"])
    scopes = {(scope["zone_id"], scope["event_type"]) for scope in annotation_set["scopes"]}
    labels = sorted(
        annotation_set["annotations"], key=lambda row: (row["start_s"], row["annotation_id"])
    )
    events = sorted(analysis.get("events", []), key=lambda row: (row["at_s"], row["event_id"]))
    eligible, excluded = [], []
    for event in events:
        scoped = (event.get("zone_id"), event.get("event_type")) in scopes
        reviewed = any(start <= event["at_s"] <= end for start, end in coverage)
        (eligible if scoped and reviewed else excluded).append(event)
    edges: dict[int, list[int]] = {}
    for i, label in enumerate(labels):
        edges[i] = sorted(
            [
                j
                for j, event in enumerate(eligible)
                if (event.get("zone_id"), event.get("event_type"))
                == (label["zone_id"], label["event_type"])
                and label["start_s"] - tolerance_s <= event["at_s"] <= label["end_s"] + tolerance_s
            ],
            key=lambda j: (abs(eligible[j]["at_s"] - label["start_s"]), eligible[j]["event_id"]),
        )
    assigned: dict[int, int] = {}

    def assign(i: int, visited: set[int]) -> bool:
        for j in edges[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in assigned or assign(assigned[j], visited):
                assigned[j] = i
                return True
        return False

    for i in range(len(labels)):
        assign(i, set())
    matched_labels = set(assigned.values())
    matches = [
        {
            "annotation": labels[i],
            "event": eligible[j],
            "signed_delay_s": eligible[j]["at_s"] - labels[i]["start_s"],
        }
        for j, i in sorted(assigned.items())
    ]
    false_alarms = [event for j, event in enumerate(eligible) if j not in assigned]
    misses = [label for i, label in enumerate(labels) if i not in matched_labels]
    reviewed_s = sum(end - start for start, end in coverage)
    return {
        "analysis_id": analysis["analysis_id"],
        "video_id": analysis["video_id"],
        "annotation_set_id": annotation_set["annotation_set_id"],
        "metrics": measures(
            len(matches),
            len(false_alarms),
            len(misses),
            reviewed_s,
            [row["signed_delay_s"] for row in matches],
        ),
        "matches": matches,
        "false_alarms": false_alarms,
        "misses": misses,
        "excluded_events": excluded,
        "unreviewed_seconds": max(0, annotation_set["duration_s"] - reviewed_s),
        "provenance": {
            "analysis_id": analysis["analysis_id"],
            "model": analysis.get("model"),
            "zones": analysis.get("zones", []),
            "rules": analysis.get("rules", []),
            "analysis_created_at": analysis.get("created_at"),
            "annotation_hash": annotation_set["annotation_hash"],
            "configuration_hash": fingerprint(
                {key: analysis.get(key) for key in ("zones", "rules", "model")}
            ),
        },
    }
