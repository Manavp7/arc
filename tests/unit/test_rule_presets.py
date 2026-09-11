"""Portable rules require exact snapshots, explicit geometry mapping and reviewed applies."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sio_api.rule_presets import PreviewRequest, install_rule_preset_routes, mapped_rules
from sio_api.workbench_store import WorkbenchConflict

from sio_core.authn import DevJwtAuth
from sio_core.config import Settings
from sio_core.guard import install_governance


class Store:
    def __init__(self):
        self.rows = {}

    async def get(self, tenant, kind, record_id):
        return deepcopy(self.rows.get((tenant, kind, record_id)))

    async def list(self, tenant, kind, limit=500):
        return [
            deepcopy(row)
            for (owner, category, _), row in self.rows.items()
            if owner == tenant and category == kind
        ][:limit]

    async def put(self, tenant, kind, record_id, data, expected_revision=None):
        old = self.rows.get((tenant, kind, record_id), {})
        revision = old.get("revision", 0)
        if expected_revision is not None and expected_revision != revision:
            raise WorkbenchConflict("Stale")
        result = {**deepcopy(data), "revision": revision + 1, "record_id": record_id}
        self.rows[(tenant, kind, record_id)] = result
        return deepcopy(result)


def video(video_id="vid_a", zone_id="gate"):
    return {
        "video_id": video_id,
        "title": video_id,
        "status": "ready",
        "zones": [{"zone_id": zone_id, "name": "Gate", "points": [[0, 0], [1, 0], [1, 1]]}],
        "rules": [
            {
                "rule_id": "existing",
                "name": "Gate dwell",
                "zone_id": zone_id,
                "event_type": "dwell",
                "target_class": "motion",
                "threshold_s": 4,
                "cooldown_s": 12,
                "enabled": False,
            }
        ],
    }


def version():
    rule = video()["rules"][0]
    return {
        "version_id": "pv_" + "a" * 32,
        "slots": [{"slot_id": "entrance", "name": "Main entrance"}],
        "rules": [
            {
                **{key: value for key, value in rule.items() if key != "zone_id"},
                "slot_id": "entrance",
            }
        ],
    }


def test_mapping_preserves_target_polygons_existing_rules_and_rule_configuration():
    target = video("vid_b", "yard")
    original = deepcopy(target)
    mapped = mapped_rules(version(), target, {"entrance": "yard"}, "append")
    assert target == original
    assert mapped["final_rules"][0] == original["rules"][0]
    added = mapped["added_rules"][0]
    assert added["zone_id"] == "yard"
    assert added["threshold_s"] == 4
    assert added["cooldown_s"] == 12
    assert added["enabled"] is False
    assert added["target_class"] == "motion"
    assert added["rule_id"] != "existing"
    assert mapped["removed_count"] == 0


@pytest.mark.parametrize(
    "mapping",
    [{}, {"entrance": "unknown"}, {"wrong": "gate"}, {"entrance": "gate", "extra": "gate"}],
)
def test_invalid_or_incomplete_scope_maps_are_rejected(mapping):
    with pytest.raises(HTTPException) as error:
        mapped_rules(version(), video(), mapping, "append")
    assert error.value.status_code == 422


def test_two_logical_scopes_cannot_silently_collapse_into_one_target_zone():
    preset = version()
    preset["slots"].append({"slot_id": "exit", "name": "Exit"})
    with pytest.raises(HTTPException, match="different target zone"):
        mapped_rules(preset, video(), {"entrance": "gate", "exit": "gate"}, "append")


def test_duplicate_preset_append_is_rejected_and_replace_is_deliberate():
    preset, target = version(), video()
    first = mapped_rules(preset, target, {"entrance": "gate"}, "append")
    target["rules"] = first["final_rules"]
    with pytest.raises(HTTPException) as error:
        mapped_rules(preset, target, {"entrance": "gate"}, "append")
    assert error.value.status_code == 409
    replaced = mapped_rules(preset, target, {"entrance": "gate"}, "replace")
    assert len(replaced["final_rules"]) == 1
    assert replaced["removed_count"] == 2


def test_append_respects_the_recording_rule_limit():
    target = video()
    target["rules"] = [{**target["rules"][0], "rule_id": f"old_{i}"} for i in range(24)]
    with pytest.raises(HTTPException) as error:
        mapped_rules(version(), target, {"entrance": "gate"}, "append")
    assert error.value.status_code == 422


def test_duplicate_and_oversized_bulk_target_requests_are_invalid():
    target = {"video_id": "a", "revision": 1, "zone_map": {"entrance": "gate"}}
    for targets in [[target, target], [{**target, "video_id": str(i)} for i in range(21)]]:
        with pytest.raises(ValidationError):
            PreviewRequest.model_validate(
                {
                    "preset_id": "preset_" + "a" * 32,
                    "version_id": "pv_" + "a" * 32,
                    "targets": targets,
                }
            )


@pytest.fixture
async def client(tmp_path):
    store = Store()
    config = Settings(_env_file=None, data_dir=tmp_path, auth_mode="dev", auth_required=True)
    issuer = DevJwtAuth(config)
    app = FastAPI()
    install_rule_preset_routes(app, store)
    install_governance(app, service="api", settings=config, authenticator=issuer)

    @app.exception_handler(WorkbenchConflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    for name, zone in [("vid_a", "gate"), ("vid_b", "yard"), ("vid_c", "door")]:
        await store.put("tenant-a", "video", name, video(name, zone))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:

        def headers(tenant="tenant-a", role="operator", subject="reviewer", zones=()):
            return {
                "Authorization": "Bearer "
                + issuer.issue(
                    tenant_id=tenant, roles=(role,), subject=subject, clearance=3, zones=zones
                )
            }

        yield http, store, headers


def save_body(**changes):
    return {
        "name": "Gate monitoring",
        "description": "Reusable gate behavior",
        "source_video_id": "vid_a",
        "source_revision": 1,
        "slots": [{"slot_id": "entrance", "name": "Entrance", "source_zone_id": "gate"}],
        **changes,
    }


async def create(http, headers):
    response = await http.post("/api/review/rule-presets", headers=headers(), json=save_body())
    assert response.status_code == 201, response.text
    return response.json()


async def preview(http, headers, preset, targets=None, **changes):
    body = {
        "preset_id": preset["preset_id"],
        "version_id": preset["current_version_id"],
        "targets": targets
        or [{"video_id": "vid_b", "revision": 1, "zone_map": {"entrance": "yard"}}],
        **changes,
    }
    response = await http.post("/api/review/rule-presets/preview", headers=headers(), json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_saved_preset_versions_contain_no_source_geometry_and_retain_older_rules(client):
    http, store, headers = client
    preset = await create(http, headers)
    captured = deepcopy(preset["versions"][0])
    assert "points" not in str(captured)
    assert "source_zone_id" not in str(captured)
    assert "zone_id" not in str(captured["rules"])
    source = await store.get("tenant-a", "video", "vid_a")
    source["rules"][0]["threshold_s"] = 9
    await store.put("tenant-a", "video", "vid_a", source)
    updated = await http.put(
        f"/api/review/rule-presets/{preset['preset_id']}",
        headers=headers(),
        json=save_body(source_revision=2, expected_revision=1),
    )
    assert updated.status_code == 200
    assert updated.json()["versions"][0] == captured
    assert updated.json()["versions"][1]["rules"][0]["threshold_s"] == 9
    stale = await http.put(
        f"/api/review/rule-presets/{preset['preset_id']}",
        headers=headers(),
        json=save_body(source_revision=2, expected_revision=1),
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_preview_is_nonmutating_and_apply_is_exact_and_retry_safe(client):
    http, store, headers = client
    preset = await create(http, headers)
    before = await store.get("tenant-a", "video", "vid_b")
    plan = await preview(http, headers, preset)
    assert plan["mode"] == "append"
    assert await store.get("tenant-a", "video", "vid_b") == before
    result = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert result.status_code == 200
    assert result.json()["results"][0]["status"] == "applied"
    after = await store.get("tenant-a", "video", "vid_b")
    assert after["zones"] == before["zones"]
    assert after["rules"] == plan["results"][0]["final_rules"]
    assert result.json()["analysis_started"] is False
    assert await store.list("tenant-a", "analysis") == []
    again = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert again.json()["results"][0]["status"] == "already_applied"
    assert await store.get("tenant-a", "video", "vid_b") == after


@pytest.mark.asyncio
async def test_stale_target_yields_explicit_partial_bulk_results(client):
    http, store, headers = client
    preset = await create(http, headers)
    targets = [
        {"video_id": "vid_b", "revision": 1, "zone_map": {"entrance": "yard"}},
        {"video_id": "vid_c", "revision": 1, "zone_map": {"entrance": "door"}},
    ]
    plan = await preview(http, headers, preset, targets)
    changed = await store.get("tenant-a", "video", "vid_b")
    await store.put("tenant-a", "video", "vid_b", {**changed, "title": "Another editor"})
    result = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert [row["status"] for row in result.json()["results"]] == ["rejected", "applied"]
    assert result.json()["results"][0]["code"] == 409
    assert len((await store.get("tenant-a", "video", "vid_b"))["rules"]) == 1
    assert len((await store.get("tenant-a", "video", "vid_c"))["rules"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"archived_at": "now"},
        {"purged_at": "now"},
        {"status": "running"},
        {"status": "purge_failed"},
    ],
)
async def test_unavailable_targets_are_rejected_in_preview(client, change):
    http, store, headers = client
    preset = await create(http, headers)
    target = await store.get("tenant-a", "video", "vid_b")
    await store.put("tenant-a", "video", "vid_b", {**target, **change})
    plan = await preview(
        http,
        headers,
        preset,
        [{"video_id": "vid_b", "revision": 2, "zone_map": {"entrance": "yard"}}],
    )
    assert plan["results"][0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_job_start_after_preview_rejects_apply_even_if_video_revision_is_unchanged(client):
    http, store, headers = client
    preset = await create(http, headers)
    plan = await preview(http, headers, preset)
    await store.put("tenant-a", "video_job", "job", {"video_id": "vid_b", "status": "queued"})
    result = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert result.json()["results"][0]["status"] == "rejected"
    assert len((await store.get("tenant-a", "video", "vid_b"))["rules"]) == 1


@pytest.mark.asyncio
async def test_tenant_zone_role_and_preview_actor_boundaries(client):
    http, _, headers = client
    preset = await create(http, headers)
    plan = await preview(http, headers, preset)
    for kwargs in [{"tenant": "tenant-b"}, {"subject": "colleague"}]:
        response = await http.post(
            "/api/review/rule-presets/apply",
            headers=headers(**kwargs),
            json={"preview_id": plan["preview_id"]},
        )
        assert response.status_code == 404
    viewer = await http.post(
        "/api/review/rule-presets", headers=headers(role="viewer"), json=save_body()
    )
    assert viewer.status_code == 403
    visible = await http.get("/api/review/rule-presets", headers=headers(role="viewer"))
    assert len(visible.json()["presets"]) == 1
    invisible = await http.get("/api/review/rule-presets", headers=headers(tenant="tenant-b"))
    assert invisible.json()["presets"] == []
    denied = await http.post(
        "/api/review/rule-presets/apply",
        headers=headers(zones=("gate",)),
        json={"preview_id": plan["preview_id"]},
    )
    assert denied.json()["results"][0]["code"] == 403


@pytest.mark.asyncio
async def test_expired_previews_cannot_be_reused(client):
    http, store, headers = client
    preset = await create(http, headers)
    plan = await preview(http, headers, preset)
    saved = await store.get("tenant-a", "rule_preset_preview", plan["preview_id"])
    await store.put(
        "tenant-a",
        "rule_preset_preview",
        plan["preview_id"],
        {**saved, "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    result = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_preview_remains_pinned_when_a_new_preset_version_is_saved(client):
    http, store, headers = client
    preset = await create(http, headers)
    plan = await preview(http, headers, preset)
    source = await store.get("tenant-a", "video", "vid_a")
    source["rules"][0]["threshold_s"] = 20
    await store.put("tenant-a", "video", "vid_a", source)
    updated = await http.put(
        f"/api/review/rule-presets/{preset['preset_id']}",
        headers=headers(),
        json=save_body(source_revision=2, expected_revision=1),
    )
    assert updated.status_code == 200
    assert updated.json()["current_version_id"] != plan["version_id"]
    applied = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert applied.json()["results"][0]["status"] == "applied"
    target = await store.get("tenant-a", "video", "vid_b")
    assert target["rules"][-1]["threshold_s"] == 4
    assert target["last_rule_preset"]["version_id"] == plan["version_id"]


@pytest.mark.asyncio
async def test_preview_reports_invalid_target_without_preventing_other_targets(client):
    http, _, headers = client
    preset = await create(http, headers)
    plan = await preview(
        http,
        headers,
        preset,
        targets=[
            {"video_id": "vid_b", "revision": 1, "zone_map": {"entrance": "missing"}},
            {"video_id": "vid_c", "revision": 1, "zone_map": {"entrance": "door"}},
        ],
    )
    assert [row["status"] for row in plan["results"]] == ["rejected", "ready"]
    applied = await http.post(
        "/api/review/rule-presets/apply", headers=headers(), json={"preview_id": plan["preview_id"]}
    )
    assert [row["status"] for row in applied.json()["results"]] == ["rejected", "applied"]
