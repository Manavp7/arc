"""Authorize the actual stored recording and exact retained version before use."""

from __future__ import annotations

from fastapi import HTTPException

from sio_core.source_scope import require_zone, zones_for

from .case_evidence import source_evidence_zones
from .cases import need, review_video_available


def review_scope(principal, *records, write=False):
    need(principal, "review.read")
    need(principal, "media.read")
    if write:
        need(principal, "review.write")
    if not records and zones_for(principal) is not None:
        require_zone(principal, None)
    for record in records:
        zones = source_evidence_zones({"evidence": record})
        # Each stored source/version must independently establish access. A known
        # neighboring snapshot or a proposed configuration cannot grant it a zone.
        if not zones and zones_for(principal) is not None:
            require_zone(principal, None)
        for zone in zones:
            require_zone(principal, zone)
            need(principal, "review.read", zone)
            need(principal, "media.read", zone)
            if write:
                need(principal, "review.write", zone)


async def review_source(
    store, tenant, video_id, principal, *, analysis_id=None, write=False, active=True
):
    video = await store.get(tenant, "video", video_id)
    if not video or video.get("purged_at"):
        raise HTTPException(404, "Recording not found")
    analysis = None
    if analysis_id:
        analysis = await store.get(tenant, "analysis", analysis_id)
        if not analysis or analysis.get("video_id") != video_id:
            raise HTTPException(404, "Retained analysis not found for this recording")
    review_scope(principal, video, *([analysis] if analysis else []), write=write)
    if active and not review_video_available(video):
        raise HTTPException(409, "Restore an available recording before using it")
    return video, analysis
