"""Reports and progress retain the frozen workflow source's access boundary."""

from unittest.mock import AsyncMock

import httpx
import pytest
from sio_workflow.activities import ActivityContext, generate_report
from sio_workflow.playbooks import Playbook, StepSpec
from sio_workflow.runner import RunOutcome
from sio_workflow.service import WorkflowService

from sio_core.config import Settings
from sio_schemas import RunStatus, WorkflowRun, WorkflowStep


async def test_unknown_source_does_not_fetch_tenant_events():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await generate_report(
            ActivityContext(
                api_url="http://authored-api",
                ingest_url="http://unused",
                tenant_id="test",
                run_id="report",
                zone_id=None,
                bearer_token="authored-token",
                client=client,
            ),
            "report",
        )
    assert requests == []
    assert result["report"] is None and "source zone is unknown" in result["error"]


@pytest.mark.parametrize("zone", ["saved-zone", None])
async def test_progress_and_summary_keep_saved_source_zone(tmp_path, monkeypatch, zone):
    service = WorkflowService(
        Settings(_env_file=None, data_dir=tmp_path, tenant_id="test", bus_backend="memory")
    )
    emitted = AsyncMock()
    monkeypatch.setattr(service, "_emit", emitted)
    playbook = Playbook("Test", "Authored", (StepSpec("one", "One", "notify_security"),))
    step = WorkflowStep(step_id="one", name="One", status=RunStatus.COMPLETED)
    run = WorkflowRun(tenant_id="test", run_id="wfr_test", playbook="Test", steps=[step])
    await service._publish_step(run, step, playbook, None, zone_id=zone)
    await service._publish_summary(RunOutcome(run), playbook, None, zone_id=zone)
    assert emitted.await_count == 2
    assert all(call.args[0].zone_id == zone for call in emitted.await_args_list)
