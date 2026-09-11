"""Versioned portable review rules with explicit zone mapping and previewed apply."""

from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator

from sio_core.tenancy import current_tenant

from .cases import actor, need
from .video_jobs import ACTIVE_JOB_STATES, video_mutation_lock
from .video_rules import ReviewConfiguration
from .workbench_store import WorkbenchConflict


class SlotSource(BaseModel):
    slot_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    source_zone_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class PresetSave(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1500)
    source_video_id: str = Field(min_length=1, max_length=80)
    source_revision: int = Field(ge=1)
    expected_revision: int = Field(default=0, ge=0)
    slots: list[SlotSource] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_slots(self):
        if len({s.slot_id for s in self.slots}) != len(self.slots) or len(
            {s.source_zone_id for s in self.slots}
        ) != len(self.slots):
            raise ValueError("Scope slots and their source zones must be unique")
        return self


class Target(BaseModel):
    video_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    zone_map: dict[str, str] = Field(min_length=1, max_length=12)


class PreviewRequest(BaseModel):
    preset_id: str = Field(pattern=r"^preset_[a-f0-9]{32}$")
    version_id: str = Field(pattern=r"^pv_[a-f0-9]{32}$")
    mode: Literal["append", "replace"] = "append"
    targets: list[Target] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({target.video_id for target in self.targets}) != len(self.targets):
            raise ValueError("Choose each target recording once")
        return self


class ApplyRequest(BaseModel):
    preview_id: str = Field(pattern=r"^pp_[a-f0-9]{32}$")


def mapped_rules(version: dict, target: dict, zone_map: dict[str, str], mode: str) -> dict:
    """Portable slots map to existing target polygons; geometry is never copied."""
    slots = {slot["slot_id"] for slot in version["slots"]}
    if set(zone_map) != slots:
        raise HTTPException(422, "Map every preset scope slot exactly once")
    if len(set(zone_map.values())) != len(zone_map):
        raise HTTPException(422, "Each logical scope must map to a different target zone")
    zone_ids = {zone["zone_id"] for zone in target.get("zones", [])}
    if not set(zone_map.values()).issubset(zone_ids):
        raise HTTPException(422, "Mapping must use this recording's existing zones")
    additions = []
    for rule in version["rules"]:
        rule_id = (
            "preset_"
            + hashlib.sha256(f"{version['version_id']}:{rule['rule_id']}".encode()).hexdigest()[:24]
        )
        additions.append(
            {
                **{key: value for key, value in rule.items() if key != "slot_id"},
                "rule_id": rule_id,
                "zone_id": zone_map[rule["slot_id"]],
            }
        )
    previous = target.get("rules", [])
    if mode == "append" and {row["rule_id"] for row in previous} & {
        row["rule_id"] for row in additions
    }:
        raise HTTPException(
            409,
            "This preset version is already present; append would duplicate rules. Review an explicit replacement instead.",
        )
    final = [*previous, *additions] if mode == "append" else additions
    try:
        configuration = ReviewConfiguration.model_validate(
            {"zones": target.get("zones", []), "rules": final}
        )
    except ValidationError as exc:
        raise HTTPException(
            422, "Combined rules exceed the recording limits or contain invalid references"
        ) from exc
    return {
        "before_rules": deepcopy(previous),
        "added_rules": additions,
        "final_rules": configuration.model_dump(mode="json")["rules"],
        "removed_count": len(previous) if mode == "replace" else 0,
    }


class RulePresets:
    def __init__(self, store: Any):
        self.store = store
        self.save_lock = asyncio.Lock()

    async def preset(self, preset_id: str, principal):
        need(principal, "review.read")
        record = await self.store.get(current_tenant(), "rule_preset", preset_id)
        if record is None:
            raise HTTPException(404, "Rule preset not found")
        return record

    async def usable_video(self, video_id: str, principal, revision: int):
        video = await self.store.get(current_tenant(), "video", video_id)
        if not video:
            raise HTTPException(404, "Recording not found")
        need(principal, "review.write")
        for zone in video.get("zones", []):
            need(principal, "review.write", zone["zone_id"])
        if video["revision"] != revision:
            raise HTTPException(
                409, "Recording configuration changed. Refresh targets and preview again."
            )
        if (
            video.get("archived_at")
            or video.get("purged_at")
            or video.get("status")
            in {"purging", "purge_failed", "purged", "analyzing", "queued", "running", "cancelling"}
        ):
            raise HTTPException(
                409,
                "Recording is archived, purged or processing; restore it or wait for processing to finish",
            )
        if any(
            job.get("video_id") == video_id and job.get("status") in ACTIVE_JOB_STATES
            for job in await self.store.list(current_tenant(), "video_job", limit=5000)
        ):
            raise HTTPException(
                409,
                "Recording has an active processing job; wait or cancel it before changing rules",
            )
        return video

    async def save(self, body: PresetSave, principal, preset_id: str | None = None):
        tenant = current_tenant()
        async with self.save_lock, video_mutation_lock(self.store, tenant, body.source_video_id):
            source = await self.usable_video(body.source_video_id, principal, body.source_revision)
            configuration = ReviewConfiguration.model_validate(source)
            if not configuration.rules:
                raise HTTPException(
                    422, "Save at least one configured rule on the source recording first"
                )
            source_slots = {slot.source_zone_id: slot for slot in body.slots}
            if set(source_slots) != {rule.zone_id for rule in configuration.rules}:
                raise HTTPException(
                    422, "Assign a logical slot to each source zone used by the saved rules"
                )
            if (
                not preset_id
                and len(await self.store.list(tenant, "rule_preset", limit=200)) >= 200
            ):
                raise HTTPException(409, "This tenant is limited to 200 named rule presets")
            record = await self.preset(preset_id, principal) if preset_id else None
            if body.expected_revision != (record or {}).get("revision", 0):
                raise HTTPException(
                    409, "Preset changed. Reload its current revision before saving."
                )
            if record and len(record["versions"]) >= 32:
                raise HTTPException(
                    409, "This preset retains 32 versions; save a new named preset to continue"
                )
            preset_id = preset_id or "preset_" + uuid4().hex
            version = {
                "version_id": "pv_" + uuid4().hex,
                "number": len((record or {}).get("versions", [])) + 1,
                "slots": [{"slot_id": slot.slot_id, "name": slot.name} for slot in body.slots],
                "rules": [
                    {
                        **rule.model_dump(mode="json", exclude={"zone_id"}),
                        "slot_id": source_slots[rule.zone_id].slot_id,
                    }
                    for rule in configuration.rules
                ],
                "created_at": datetime.now(UTC).isoformat(),
                "created_by": principal.subject,
                "source": {"video_id": body.source_video_id, "revision": body.source_revision},
            }
            return await self.store.put(
                tenant,
                "rule_preset",
                preset_id,
                {
                    "preset_id": preset_id,
                    "name": body.name,
                    "description": body.description,
                    "shared": True,
                    "versions": [*(record or {}).get("versions", []), version],
                    "current_version_id": version["version_id"],
                    "updated_by": principal.subject,
                },
                expected_revision=body.expected_revision,
            )

    async def preview(self, body: PreviewRequest, principal):
        tenant = current_tenant()
        need(principal, "review.write")
        preset = await self.preset(body.preset_id, principal)
        version = next(
            (item for item in preset["versions"] if item["version_id"] == body.version_id), None
        )
        if version is None:
            raise HTTPException(404, "Requested preset version is unavailable")
        results = []
        for target in body.targets:
            try:
                async with video_mutation_lock(self.store, tenant, target.video_id):
                    video = await self.usable_video(target.video_id, principal, target.revision)
                    plan = mapped_rules(version, video, target.zone_map, body.mode)
                    results.append(
                        {**target.model_dump(), "title": video["title"], "status": "ready", **plan}
                    )
            except HTTPException as exc:
                results.append(
                    {
                        "video_id": target.video_id,
                        "status": "rejected",
                        "code": exc.status_code,
                        "message": str(exc.detail),
                    }
                )
        preview_id = "pp_" + uuid4().hex
        return await self.store.put(
            tenant,
            "rule_preset_preview",
            preview_id,
            {
                "preview_id": preview_id,
                "preset_id": body.preset_id,
                "preset_name": preset["name"],
                "version_id": body.version_id,
                "version_number": version["number"],
                "mode": body.mode,
                "results": results,
                "created_by": principal.subject,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "note": "Applies saved rule configuration only. Analysis is not started; existing recorded results retain their original configuration.",
            },
            expected_revision=0,
        )

    async def apply(self, preview_id: str, principal):
        tenant = current_tenant()
        need(principal, "review.write")
        preview = await self.store.get(tenant, "rule_preset_preview", preview_id)
        if not preview or preview["created_by"] != principal.subject:
            raise HTTPException(404, "Rule preset preview not found for this reviewer")
        if datetime.fromisoformat(preview["expires_at"]) <= datetime.now(UTC):
            raise HTTPException(409, "Preview expired. Refresh targets and preview again.")
        results = []
        for target in preview["results"]:
            if target["status"] != "ready":
                results.append(target)
                continue
            video_id = target["video_id"]
            try:
                async with video_mutation_lock(self.store, tenant, video_id):
                    current = await self.store.get(tenant, "video", video_id)
                    # A retry of the same apply cannot append the same rules twice.
                    if current and preview_id in current.get("preset_applications", []):
                        await self.usable_video(video_id, principal, current["revision"])
                        results.append(
                            {
                                "video_id": video_id,
                                "title": target["title"],
                                "status": "already_applied",
                                "revision": current["revision"],
                            }
                        )
                        continue
                    video = await self.usable_video(video_id, principal, target["revision"])
                    configuration = ReviewConfiguration.model_validate(
                        {"zones": video["zones"], "rules": target["final_rules"]}
                    )
                    updated = await self.store.put(
                        tenant,
                        "video",
                        video_id,
                        {
                            **video,
                            **configuration.model_dump(mode="json"),
                            "preset_applications": [
                                *video.get("preset_applications", []),
                                preview_id,
                            ][-50:],
                            "last_rule_preset": {
                                "preset_id": preview["preset_id"],
                                "version_id": preview["version_id"],
                                "preview_id": preview_id,
                                "mode": preview["mode"],
                                "applied_by": principal.subject,
                                "applied_at": datetime.now(UTC).isoformat(),
                            },
                        },
                        expected_revision=target["revision"],
                    )
                    results.append(
                        {
                            "video_id": video_id,
                            "title": target["title"],
                            "status": "applied",
                            "revision": updated["revision"],
                        }
                    )
            except (HTTPException, WorkbenchConflict) as exc:
                results.append(
                    {
                        "video_id": video_id,
                        "status": "rejected",
                        "code": exc.status_code if isinstance(exc, HTTPException) else 409,
                        "message": str(exc.detail)
                        if isinstance(exc, HTTPException)
                        else "Recording changed; preview again.",
                    }
                )
        return {
            "preview_id": preview_id,
            "version_id": preview["version_id"],
            "mode": preview["mode"],
            "results": results,
            "analysis_started": False,
        }


def install_rule_preset_routes(app: FastAPI, store: Any) -> RulePresets:
    presets = RulePresets(store)
    prefix = "/api/review/rule-presets"

    @app.get(prefix)
    async def listing(request: Request):
        need(actor(request), "review.read")
        rows = await store.list(current_tenant(), "rule_preset", limit=200)
        return {"presets": rows, "limit": 200}

    @app.post(prefix, status_code=201)
    async def create(body: PresetSave, request: Request):
        return await presets.save(body, actor(request))

    @app.post(prefix + "/preview")
    async def preview(body: PreviewRequest, request: Request):
        return await presets.preview(body, actor(request))

    @app.post(prefix + "/apply")
    async def apply(body: ApplyRequest, request: Request):
        return await presets.apply(body.preview_id, actor(request))

    @app.get(prefix + "/{preset_id}")
    async def detail(preset_id: str, request: Request):
        return await presets.preset(preset_id, actor(request))

    @app.put(prefix + "/{preset_id}")
    async def update(preset_id: str, body: PresetSave, request: Request):
        return await presets.save(body, actor(request), preset_id)

    return presets
