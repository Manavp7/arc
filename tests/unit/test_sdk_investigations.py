"""Exercise public SDK operations against an HTTP transport, without service or hardware calls."""

import json

import httpx
import pytest
from pydantic import ValidationError

from sio_sdk import SioApiError, SioClient
from sio_sdk.investigations import (
    ActivationPreview,
    CalibrationPreview,
    CaseUpdate,
    RecordedSample,
    RecordingClock,
)

VIDEO = {
    "video_id": "vid-a",
    "title": "Fixture",
    "duration_s": 12,
    "status": "ready",
    "revision": 1,
}
CASE = {"case_id": "case-a", "title": "Fixture case", "status": "open", "revision": 2}
CALIBRATION = {
    "status": "pending_fusion",
    "source_id": "source-a",
    "fusion": {"acknowledged": False},
    "can_rollback": True,
    "note": "Awaiting fusion",
}


async def instrument(client, callback):
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(callback))


@pytest.mark.parametrize("token", ["", " "])
def test_empty_external_token_never_selects_development_identity(token):
    with pytest.raises(ValueError, match="must not be empty"):
        SioClient(token=token)


@pytest.mark.asyncio
async def test_typed_pages_preserve_cursors_and_authorized_metadata():
    requests = []

    def respond(request):
        requests.append(request)
        if "/analyses" in request.url.path:
            result = {
                "analyses": [
                    {"analysis_id": "old", "status": "completed", "evidence_zone_ids": ["yard"]}
                ],
                "next_cursor": "opaque+/=",
            }
        elif "recording-timeline" in request.url.path:
            result = {
                "recordings": [{**VIDEO, "clock": None, "interval": None}],
                "camera_ids": ["camera-a"],
                "next_cursor": None,
                "note": "Declared clocks",
            }
        else:
            result = {"videos": [VIDEO], "capabilities": {"upload": True}}
        return httpx.Response(200, json=result)

    async with SioClient(token="signed") as client:
        await instrument(client, respond)
        library = await client.investigations.videos()
        assert library.videos[0].title == "Fixture"
        analyses = await client.investigations.analyses("video/a", cursor="opaque+/=")
        assert analyses.analyses[0].model_extra["evidence_zone_ids"] == ["yard"]
        assert analyses.next_cursor == "opaque+/="
        timeline = await client.investigations.timeline(
            camera_id="camera-a", from_time="2026-09-30T00:00:00Z", cursor=analyses.next_cursor
        )
        assert timeline.recordings[0].interval is None
    assert all(request.headers["authorization"] == "Bearer signed" for request in requests)
    assert b"video%2Fa/analyses" in requests[1].url.raw_path
    assert requests[1].url.params["cursor"] == "opaque+/="
    assert requests[2].url.params["from"] == "2026-09-30T00:00:00Z"


@pytest.mark.asyncio
async def test_upload_preserves_mp4_bytes_and_explicit_access_zone():
    requests = []
    async with SioClient(token="signed") as client:
        await instrument(
            client, lambda request: (requests.append(request), httpx.Response(200, json=VIDEO))[1]
        )
        result = await client.investigations.upload(
            b"\x00mp4fixture", "camera one.mp4", access_zone_id="yard/a"
        )
        assert result.video_id == "vid-a"
    assert requests[0].content == b"\x00mp4fixture"
    assert requests[0].headers["content-type"] == "video/mp4"
    assert requests[0].headers["x-filename"] == "camera%20one.mp4"
    assert requests[0].url.params["access_zone_id"] == "yard/a"


@pytest.mark.asyncio
async def test_case_updates_can_clear_owner_without_supplying_actor_or_other_unset_fields():
    requests = []
    async with SioClient(token="signed") as client:
        await instrument(
            client, lambda request: (requests.append(request), httpx.Response(200, json=CASE))[1]
        )
        result = await client.investigations.update_case(
            "case/a", CaseUpdate(expected_revision=1, owner=None)
        )
        assert result.revision == 2
    assert json.loads(requests[0].content) == {"expected_revision": 1, "owner": None}
    with pytest.raises(ValidationError):
        CaseUpdate(expected_revision=1, actor="somebody-else")


@pytest.mark.asyncio
async def test_search_requires_explicit_consent_and_exactly_one_query_mode():
    requests = []
    async with SioClient(token="signed") as client:

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [],
                    "total_samples": 0,
                    "searched_recordings": 0,
                    "model_id": "fixture-model",
                },
            )

        await instrument(client, respond)
        for consent in (False, "yes", 1):
            with pytest.raises(ValueError):
                await client.investigations.index_recording(
                    "v", "a", consent_private_original=consent
                )
        with pytest.raises(ValueError):
            await client.investigations.search_recordings(text=" ")
        sample = RecordedSample(video_id="v", analysis_id="a", frame_index=4)
        with pytest.raises(ValueError):
            await client.investigations.search_recordings(text="person", sample=sample)
        assert not requests
        page = await client.investigations.search_recordings(sample=sample)
        assert page.results == []
    assert json.loads(requests[0].content)["sample"] == sample.model_dump()


@pytest.mark.asyncio
async def test_reviewed_actions_send_only_bound_tickets_and_revision_not_preview_config():
    requests = []
    async with SioClient(token="signed") as client:

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json=CALIBRATION
                if "/calibration/" in request.url.path
                else {"status": "rolled_back", "source_id": "source-a"},
            )

        await instrument(client, respond)
        activation = ActivationPreview(
            preview_id="ticket-a",
            source_id="source/a",
            action="rollback",
            expires_at="2026-09-30T09:00:00Z",
            requires_fresh_observation=True,
            after={"credential": "not forwarded"},
        )
        await client.investigations.apply_activation(activation)
        preview = CalibrationPreview(
            preview_id="ticket-b",
            operation="rollback",
            setup_id="setup/a",
            setup_revision=7,
            source_id="source-a",
            expires_at="2026-09-30T09:00:00Z",
            proposed_pose={"lat": 42},
            note="Preview",
        )
        status = await client.investigations.apply_calibration(preview)
        assert status.fusion.acknowledged is False
    assert json.loads(requests[0].content) == {"preview_id": "ticket-a", "timeout_s": 20}
    assert requests[0].extensions["timeout"]["read"] == 90
    assert json.loads(requests[1].content) == {"preview_id": "ticket-b", "expected_revision": 7}
    assert b"setup%2Fa/calibration/rollback" in requests[1].url.raw_path


@pytest.mark.asyncio
async def test_delivery_pagination_history_and_manual_retry_preserve_reason():
    requests = []
    async with SioClient(token="signed") as client:

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/history"):
                body = {
                    "history": [
                        {
                            "kind": "manual_retry",
                            "at": "2026-09-30T08:00:00Z",
                            "attempt": 1,
                            "actor": "authenticated-user",
                        }
                    ],
                    "next_cursor": "older",
                }
            elif request.url.path.endswith("/retry"):
                body = {
                    "delivery_id": "delivery-a",
                    "alert_id": "alert-a",
                    "status": "pending",
                    "attempts": 5,
                    "can_retry": False,
                }
            else:
                body = {
                    "deliveries": [],
                    "next_cursor": "next",
                    "configured": True,
                    "max_attempts": 5,
                }
            return httpx.Response(200, json=body)

        await instrument(client, respond)
        assert (
            await client.investigations.deliveries(status="failed", cursor="next+/=")
        ).next_cursor == "next"
        assert (await client.investigations.delivery_history("delivery/a", cursor="older")).history[
            0
        ].actor == "authenticated-user"
        assert (
            await client.investigations.retry_delivery("delivery/a", "  Receiver restored  ")
        ).status == "pending"
    assert requests[0].url.params["cursor"] == "next+/="
    assert json.loads(requests[2].content) == {"reason": "Receiver restored"}


@pytest.mark.asyncio
async def test_external_token_401_never_falls_back_to_development_identity():
    requests = []
    async with SioClient(token="expired-provider-token") as client:
        await instrument(
            client,
            lambda request: (
                requests.append(request),
                httpx.Response(401, json={"detail": "expired"}),
            )[1],
        )
        with pytest.raises(SioApiError) as failure:
            await client.investigations.videos()
        assert failure.value.status == 401
    assert len(requests) == 1
    assert requests[0].url.path == "/api/review/videos"


@pytest.mark.asyncio
async def test_clock_save_uses_only_editable_fields():
    requests = []
    clock = RecordingClock(
        revision=1,
        camera_id="camera-a",
        capture_started_at="2026-09-30T08:00:00Z",
        note="Surveyed clock",
    )
    async with SioClient(token="signed") as client:
        await instrument(
            client,
            lambda request: (
                requests.append(request),
                httpx.Response(
                    200, json={**clock.model_dump(), "revision": 2, "declared_by": "signed-user"}
                ),
            )[1],
        )
        result = await client.investigations.save_clock("video/a", clock)
        assert result.revision == 2
        assert result.declared_by == "signed-user"
    assert "declared_by" not in json.loads(requests[0].content)


@pytest.mark.asyncio
async def test_job_history_pagination_preserves_server_side_view():
    requests = []
    payload = {
        "jobs": [
            {
                "job_id": "job-old",
                "video_id": "v",
                "video_title": "Fixture",
                "analysis_id": "a",
                "revision": 2,
                "status": "completed",
                "progress": 1,
                "attempt": 1,
                "max_attempts": 2,
                "queued_at": "2026-09-30T00:00:00Z",
            }
        ],
        "next_cursor": "older+/=",
        "capabilities": {"max_attempts": 2, "max_pending": 4, "worker_concurrency": 1},
    }
    async with SioClient(token="signed") as client:
        await instrument(
            client, lambda request: (requests.append(request), httpx.Response(200, json=payload))[1]
        )
        first = await client.investigations.jobs(view="finished", limit=1)
        older = await client.investigations.jobs(view="finished", limit=1, cursor=first.next_cursor)
        assert older.jobs[0].status == "completed"
    assert all(
        request.url.path == "/api/review/jobs" and request.url.params["view"] == "finished"
        for request in requests
    )
    assert requests[1].url.params["cursor"] == "older+/="
    assert requests[1].url.params["limit"] == "1"
