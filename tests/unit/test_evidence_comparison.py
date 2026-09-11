"""Saved alignment is explicit, versioned and scoped to immutable case sources."""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sio_api.case_helpers import CaseCreate, CasePatch, EvidenceAttach
from sio_api.evidence_comparison import ComparisonSave, EvidenceComparison
from test_case_attachments import ADMIN, TENANT
from test_case_attachments import work as work

from sio_core.authn import Principal
from sio_core.tenancy import tenant_scope


async def prepare(work):
    casework, first, second = work
    case = await casework.create(CaseCreate(**first), ADMIN)
    case = await casework.attach(
        case["case_id"], EvidenceAttach(expected_revision=case["revision"], **second), ADMIN
    )
    manager = EvidenceComparison(casework.store, casework.pool)
    body = ComparisonSave(
        revision=0,
        case_revision=case["revision"],
        left_attachment_id="original",
        right_attachment_id=case["evidence_attachments"][0]["attachment_id"],
        offset_s=1.5,
        note="Reviewer aligned the visible entry; camera clocks are unknown.",
    )
    return manager, case, body


async def test_save_reload_preserves_pinned_sources_and_original_case(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        before = deepcopy(await manager.casework.load(case["case_id"], ADMIN))
        blank = await manager.get(case["case_id"], ADMIN)
        assert blank["revision"] == 0 and blank["left_attachment_id"] is None
        result = await manager.save(case["case_id"], body, ADMIN)
        assert result["revision"] == 1 and result["updated_by"] == ADMIN.subject
        assert result["offset_s"] == 1.5
        fresh = EvidenceComparison(manager.store, manager.casework.pool)
        assert (await fresh.get(case["case_id"], ADMIN))["note"] == body.note
        assert await manager.casework.load(case["case_id"], ADMIN) == before
        raw = await manager.store.get(TENANT, "case_comparison", case["case_id"])
        assert [ref["analysis_id"] for ref in raw["source_refs"]] == ["ana_1", "ana_2"]


async def test_stale_alignment_cannot_replace_a_colleagues_saved_offset(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        await manager.save(case["case_id"], body, ADMIN)
        with pytest.raises(HTTPException) as error:
            await manager.save(case["case_id"], body.model_copy(update={"offset_s": -2}), ADMIN)
        assert error.value.status_code == 409
        assert (await manager.get(case["case_id"], ADMIN))["offset_s"] == 1.5


async def test_case_revision_is_rechecked_before_alignment_is_saved(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        await manager.casework.patch(
            case["case_id"], CasePatch(expected_revision=case["revision"], title="Edited"), ADMIN
        )
        with pytest.raises(HTTPException) as error:
            await manager.save(case["case_id"], body, ADMIN)
        assert error.value.status_code == 409


async def test_case_change_during_source_reads_invalidates_save(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        original = manager.validate_source
        calls = 0

        async def source(item, principal):
            nonlocal calls
            await original(item, principal)
            calls += 1
            if calls == 2:
                await manager.casework.patch(
                    case["case_id"],
                    CasePatch(expected_revision=case["revision"], summary="New review"),
                    ADMIN,
                )

        manager.validate_source = source
        with pytest.raises(HTTPException) as error:
            await manager.save(case["case_id"], body, ADMIN)
        assert error.value.status_code == 409
        assert await manager.store.get(TENANT, "case_comparison", case["case_id"]) is None


async def test_only_the_cases_recording_sources_can_be_selected(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        with pytest.raises(HTTPException) as error:
            await manager.save(
                case["case_id"], body.model_copy(update={"right_attachment_id": "unrelated"}), ADMIN
            )
        assert error.value.status_code == 422


@pytest.mark.parametrize("change", [{"archived_at": "2026-09-11T10:00:00Z"}, {"status": "purged"}])
async def test_unavailable_recordings_cannot_gain_saved_alignments(work, change):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        video = await manager.store.get(TENANT, "video", "vid_2")
        await manager.store.put(TENANT, "video", "vid_2", {**video, **change})
        with pytest.raises(HTTPException) as error:
            await manager.save(case["case_id"], body, ADMIN)
        assert error.value.status_code == 409


async def test_current_scope_and_role_are_checked_on_every_access(work):
    with tenant_scope(TENANT):
        manager, case, body = await prepare(work)
        viewer = Principal(
            subject="observer", tenant_id=TENANT, roles=frozenset({"viewer"}), clearance=3
        )
        assert (await manager.get(case["case_id"], viewer))["case_id"] == case["case_id"]
        with pytest.raises(HTTPException) as error:
            await manager.save(case["case_id"], body, viewer)
        assert error.value.status_code == 403
    with tenant_scope("different-tenant"):
        with pytest.raises(HTTPException) as error:
            await manager.get(case["case_id"], ADMIN)
        assert error.value.status_code == 404


@pytest.mark.parametrize("offset", [float("nan"), float("inf"), 181, -181])
def test_invalid_time_offsets_are_rejected(offset):
    with pytest.raises(ValidationError):
        ComparisonSave(
            revision=0,
            case_revision=1,
            left_attachment_id="original",
            right_attachment_id="other",
            offset_s=offset,
        )


def test_source_pair_must_be_distinct():
    with pytest.raises(ValidationError):
        ComparisonSave(
            revision=0,
            case_revision=1,
            left_attachment_id="original",
            right_attachment_id="original",
        )
