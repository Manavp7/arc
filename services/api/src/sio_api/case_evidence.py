"""Resolve exact platform evidence references to indexed, sanitized frames."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi.encoders import jsonable_encoder

SOURCE_FIELDS = (
    "title",
    "alert_id",
    "video_id",
    "analysis_id",
    "event_id",
    "annotation_set_id",
    "annotation_id",
    "evaluation_report_id",
    "source_id",
    "zone_id",
    "at_s",
    "evidence_zone_ids",
    "evidence",
)


def recorded_evidence(item: dict[str, Any]) -> bool:
    return (item.get("evidence") or {}).get("source") in {"recorded_file", "reviewer_annotation"}


def source_evidence_zones(item: dict[str, Any]) -> set[str]:
    """Every zone named by an immutable source snapshot, including its complete rule layout."""
    zones = {str(zone) for zone in item.get("evidence_zone_ids") or [] if zone}
    if item.get("zone_id"):
        zones.add(str(item["zone_id"]))

    def collect(value: Any):
        if isinstance(value, dict):
            if value.get("zone_id"):
                zones.add(str(value["zone_id"]))
            for key, nested in value.items():
                if key == "evidence_zone_ids" and isinstance(nested, list):
                    zones.update(str(zone) for zone in nested if isinstance(zone, str) and zone)
                elif isinstance(nested, (dict, list)):
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(item.get("evidence") or {})
    return zones


def case_evidence_zones(record: dict[str, Any]) -> set[str]:
    return set().union(
        *(
            source_evidence_zones(item)
            for item in [record, *(record.get("evidence_attachments") or [])]
        )
    )


def source_identity(item: dict[str, Any]) -> tuple[str, ...] | None:
    if item.get("alert_id"):
        return ("alert", str(item["alert_id"]))
    if all(
        item.get(key) for key in ("video_id", "analysis_id", "annotation_set_id", "annotation_id")
    ):
        return (
            "annotation",
            *(
                str(item[key])
                for key in ("video_id", "analysis_id", "annotation_set_id", "annotation_id")
            ),
        )
    if all(item.get(key) for key in ("video_id", "analysis_id", "event_id")):
        return ("video", *(str(item[key]) for key in ("video_id", "analysis_id", "event_id")))
    return None


def evidence_timestamp(value: Any) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else float("inf")
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def case_evidence_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One timeline of source snapshots without inventing absolute times for recordings."""
    original = {
        **{key: deepcopy(record.get(key)) for key in SOURCE_FIELDS},
        "attachment_id": "original",
        "attached_at": record.get("created_at")
        or (record.get("evidence") or {}).get("captured_at")
        or "",
        "attached_by": record.get("created_by") or "Unknown",
        "note": "Original case evidence",
    }
    rows = [original, *deepcopy(record.get("evidence_attachments") or [])]
    for row in rows:
        evidence = row.get("evidence") or {}
        alert = evidence.get("alert") or {}
        source_at = alert.get("ts") or alert.get("first_ts")
        if evidence.get("source") == "platform_alert" and evidence_timestamp(source_at) < float(
            "inf"
        ):
            row["timeline_at"] = source_at
            row["time_basis"] = "platform_source_time"
        else:
            row["timeline_at"] = row.get("attached_at") or evidence.get("captured_at") or ""
            row["time_basis"] = (
                "attachment_time_recording_clock_unknown"
                if recorded_evidence(row)
                else "attachment_time_source_clock_unknown"
            )
    return sorted(
        rows,
        key=lambda row: (evidence_timestamp(row["timeline_at"]), str(row.get("attachment_id", ""))),
    )


def safe_frame_key(key: str, tenant: str) -> bool:
    parts = key.split("/")
    if (
        not key
        or key.startswith(("/", "raw/", "pending/"))
        or ".." in parts
        or ":" in key
        or "\\" in key
    ):
        return False
    if key.startswith("tenants/") and (len(parts) < 3 or parts[1] != tenant):
        return False
    return key.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))


async def resolve_frames(
    pool: Any, tenant: str, alert: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """No nearest-frame guesses: every result has an exact stored provenance chain.

    Older deployments did not persist all observations or detections. Those references remain
    explicitly unresolved; a nearby frame must never be presented as evidence of the event.
    """
    refs: list[dict[str, Any]] = []
    for item in [alert, *events]:
        for ref in [
            *item.get("evidence", []),
            *(item.get("explanation") or {}).get("evidence", []),
        ]:
            if ref.get("kind") in ("observation", "detection", "frame") and ref.get("ref"):
                identity = (ref["kind"], ref["ref"])
                if identity not in {(old["kind"], old["ref"]) for old in refs}:
                    refs.append({"kind": ref["kind"], "ref": ref["ref"]})
    refs = refs[:32]
    if not refs:
        return {
            "resolved_frames": [],
            "unresolved_media": [],
            "media_note": "No image-bearing evidence references were recorded.",
        }
    # Rule-engine versions sometimes labelled detection IDs as observations. Resolve the exact
    # ID in either table, retaining the actual detection-to-observation mapping in the output.
    ids = [str(ref["ref"]) for ref in refs]
    detection_rows = await pool.fetch(
        "SELECT detection_id, observation_id, source_id, model_name FROM detections WHERE tenant_id = %s AND detection_id = ANY(%s)",
        (tenant, ids),
    )
    detection_map = {row["detection_id"]: dict(row) for row in detection_rows}
    observation_ids = list(
        dict.fromkeys(
            [
                *ids,
                *(
                    str(row["observation_id"])
                    for row in detection_rows
                    if row.get("observation_id")
                ),
            ]
        )
    )
    observation_rows = await pool.fetch(
        "SELECT observation_id, source_id, raw_ref, payload FROM observations WHERE tenant_id = %s AND observation_id = ANY(%s)",
        (tenant, observation_ids),
    )
    observation_map = {row["observation_id"]: dict(row) for row in observation_rows}
    candidates: dict[str, set[str]] = {}
    for reference in ids:
        linked = {reference}
        observation_id = (detection_map.get(reference) or {}).get("observation_id", reference)
        if observation_id:
            linked.add(str(observation_id))
        observation = observation_map.get(observation_id) or {}
        if observation.get("raw_ref"):
            linked.add(str(observation["raw_ref"]))
        payload = observation.get("payload") or {}
        frame_id = payload.get("frame_id") or (payload.get("payload") or {}).get("frame_id")
        if frame_id:
            linked.add(str(frame_id))
        candidates[reference] = linked
    frame_ids = sorted(set().union(*candidates.values()))
    rows = await pool.fetch(
        "SELECT frame_id, source_id, ts, object_key, redacted, width, height FROM frames WHERE tenant_id = %s AND redacted = true AND (frame_id = ANY(%s) OR object_key = ANY(%s)) ORDER BY ts LIMIT 64",
        (tenant, frame_ids, frame_ids),
    )
    result: list[dict[str, Any]] = []
    found: set[str] = set()
    for frame in rows:
        key = str(frame.get("object_key") or "")
        if not frame.get("redacted") or not safe_frame_key(key, tenant):
            continue
        matching = [
            ref
            for ref, linked in candidates.items()
            if frame["frame_id"] in linked or key in linked
        ]
        if not matching:
            continue
        found.update(matching)
        result.append(
            {
                "frame_id": frame["frame_id"],
                "source_id": frame["source_id"],
                "ts": jsonable_encoder(frame.get("ts")),
                "media_url": "/media/" + quote(key, safe="/"),
                "redacted": True,
                "width": frame.get("width"),
                "height": frame.get("height"),
                "evidence_refs": matching,
                "detections": [detection_map[ref] for ref in matching if ref in detection_map],
            }
        )
    return {
        "resolved_frames": result,
        "unresolved_media": [
            {**ref, "reason": "No exact indexed sanitized frame is available"}
            for ref in refs
            if ref["ref"] not in found
        ],
        "media_note": "Only exact frame, observation or detection links with a sanitized frame index are resolved. Missing historic provenance is not inferred from timestamps.",
    }
