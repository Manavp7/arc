"""Case attachments retain every media source through cleanup and offline backup."""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from sio_api.video_storage import ArchiveBody, PurgeBody
from test_review_backup import backup, purged_documents
from test_video_jobs_storage import queue as queue
from test_video_jobs_storage import seed_video


@pytest.mark.parametrize("include_video_id", [True, False])
async def test_additional_case_evidence_protects_media_via_video_or_analysis(
    queue, include_video_id
):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    attachment = {"analysis_id": "ana_retained", "evidence": {"source": "recorded_file"}}
    if include_video_id:
        attachment["video_id"] = video["video_id"]
    await store.put(
        "tenant-a",
        "analysis",
        "ana_retained",
        {
            "analysis_id": "ana_retained",
            "video_id": video["video_id"],
            "status": "completed",
        },
    )
    await store.put(
        "tenant-a",
        "case",
        "case-existing",
        {
            "alert_id": "original-alert",
            "evidence_attachments": [attachment],
        },
    )
    row = (await manager.storage.candidates("tenant-a", [video["video_id"]], manual=True))[0]
    assert not row["eligible"] and "case_linked" in row["reasons"]
    with pytest.raises(HTTPException) as error:
        await manager.storage.archive(
            "tenant-a",
            ArchiveBody(
                videos=[
                    {
                        "video_id": video["video_id"],
                        "revision": video["revision"],
                    }
                ]
            ).videos,
            "admin",
        )
    assert error.value.status_code == 409
    assert (manager.directory("tenant-a", video["video_id"]) / "original.mp4").exists()


async def test_attachment_added_after_cleanup_preview_invalidates_it(queue):
    manager, store, _ = queue
    video = await seed_video(manager, store)
    await manager.storage.archive(
        "tenant-a",
        ArchiveBody(
            videos=[
                {
                    "video_id": video["video_id"],
                    "revision": video["revision"],
                }
            ]
        ).videos,
        "admin",
    )
    preview = await manager.storage.purge_preview("tenant-a", [video["video_id"]], "admin")
    # Model a concurrently retained reference. Public attachment routes reject archived sources.
    await store.put(
        "tenant-a",
        "case",
        "late-reference",
        {
            "evidence_attachments": [{"video_id": video["video_id"]}],
        },
    )
    with pytest.raises(HTTPException) as error:
        await manager.storage.purge(
            "tenant-a",
            PurgeBody(
                preview_token=preview["preview_token"],
                confirmed_video_ids=[video["video_id"]],
            ),
            "admin",
        )
    assert error.value.status_code == 409
    assert (manager.directory("tenant-a", video["video_id"]) / "original.mp4").exists()


def attachment_documents():
    return [
        {"tenant_id": "tenant-a", "kind": "video", "record_id": "vid_b", "payload": {}},
        {
            "tenant_id": "tenant-a",
            "kind": "analysis",
            "record_id": "ana_b",
            "payload": {
                "video_id": "vid_b",
                "detections": [{"frame_index": 1}],
            },
        },
        {
            "tenant_id": "tenant-a",
            "kind": "case",
            "record_id": "case_a",
            "payload": {
                "alert_id": "original-alert",
                "evidence": {},
                "evidence_attachments": [
                    {
                        "attachment_id": "additional",
                        "video_id": "vid_b",
                        "analysis_id": "ana_b",
                        "evidence": {
                            "resolved_frames": [{"media_url": "/media/tenants/tenant-a/extra.jpg"}]
                        },
                    }
                ],
            },
        },
    ]


def test_backup_retains_additional_video_analysis_and_platform_frames():
    documents = attachment_documents()
    before = deepcopy(documents)
    refs = backup.media_references(documents)
    assert not refs["missing_records"] and not refs["unresolved_references"]
    assert any(path.endswith("/vid_b/ana_b/1.jpg") for path in refs["paths"])
    assert "blobs/tenants/tenant-a/extra.jpg" in refs["paths"]
    assert documents == before


def annotation_documents():
    documents = attachment_documents()
    source = documents[2]["payload"]["evidence_attachments"][0]
    source.update(
        annotation_set_id="ann_frozen", annotation_id="label_miss", evaluation_report_id="eval_miss"
    )
    source["evidence"].update(
        source="reviewer_annotation", annotation_set={"annotation_hash": "frozen-hash"}
    )
    documents.extend(
        [
            {
                "tenant_id": "tenant-a",
                "kind": "evaluation_annotations",
                "record_id": "ann_frozen",
                "payload": {
                    "video_id": "vid_b",
                    "annotation_hash": "frozen-hash",
                    "annotations": [{"annotation_id": "label_miss"}],
                },
            },
            {
                "tenant_id": "tenant-a",
                "kind": "evaluation_report",
                "record_id": "eval_miss",
                "payload": {
                    "video_ids": ["vid_b"],
                    "analysis_ids": ["ana_b"],
                    "candidates": [
                        {
                            "clips": [
                                {
                                    "video_id": "vid_b",
                                    "analysis_id": "ana_b",
                                    "annotation_set_id": "ann_frozen",
                                    "misses": [{"annotation_id": "label_miss"}],
                                }
                            ]
                        }
                    ],
                },
            },
        ]
    )
    return documents


def test_backup_checks_frozen_annotation_and_miss_report_references():
    refs = backup.media_references(annotation_documents())
    assert not refs["missing_records"] and not refs["unresolved_references"]


@pytest.mark.parametrize(
    "broken", ["set", "label", "report", "hash", "foreign_set", "unmatched_report"]
)
def test_backup_refuses_missing_or_mismatched_annotation_provenance(broken):
    documents = annotation_documents()
    source = documents[2]["payload"]["evidence_attachments"][0]
    if broken == "set":
        documents = [row for row in documents if row["kind"] != "evaluation_annotations"]
    elif broken == "label":
        source["annotation_id"] = "not-frozen"
    elif broken == "report":
        source["evaluation_report_id"] = "not-retained"
    elif broken == "hash":
        source["evidence"]["annotation_set"]["annotation_hash"] = "changed"
    elif broken == "foreign_set":
        documents[3]["tenant_id"] = "other-tenant"
    else:
        documents[4]["payload"]["candidates"][0]["clips"][0]["misses"] = []
    assert backup.media_references(documents)["missing_records"]


def test_backup_checks_comparison_sources_and_retained_case():
    documents = annotation_documents()
    source = documents[2]["payload"]["evidence_attachments"][0]
    documents.append(
        {
            "tenant_id": "tenant-a",
            "kind": "case_comparison",
            "record_id": "case_a",
            "payload": {"case_id": "case_a", "source_refs": [source]},
        }
    )
    assert not backup.media_references(documents)["missing_records"]
    documents[2]["record_id"] = "missing-case"
    assert "case_comparison:case_a:case" in backup.media_references(documents)["missing_records"]


@pytest.mark.parametrize("broken", ["video", "analysis", "foreign_tenant", "mismatched_video"])
def test_backup_rejects_missing_or_mismatched_attachment_records(broken):
    documents = attachment_documents()
    if broken in {"video", "analysis"}:
        documents = [row for row in documents if row["kind"] != broken]
    elif broken == "foreign_tenant":
        documents[0]["tenant_id"] = "tenant-b"
    else:
        documents[1]["payload"]["video_id"] = "different-video"
    refs = backup.media_references(documents)
    assert any(error.startswith("case:case_a:attachment:0:") for error in refs["missing_records"])


def test_backup_rejects_attachment_to_purged_media_including_analysis_only():
    documents = [
        *purged_documents(),
        {
            "tenant_id": "tenant-a",
            "kind": "case",
            "record_id": "case_a",
            "payload": {"evidence_attachments": [{"analysis_id": "ana_a"}]},
        },
    ]
    assert (
        "case:case_a:attachment:0:purged_video"
        in backup.media_references(documents)["missing_records"]
    )


def test_backup_rejects_external_attachment_frame_reference():
    documents = attachment_documents()
    documents[-1]["payload"]["evidence_attachments"][0]["evidence"]["resolved_frames"][0][
        "media_url"
    ] = "https://example.invalid/frame.jpg"
    assert backup.media_references(documents)["unresolved_references"] == [
        "case:case_a:attachment:0:unsupported_frame_reference",
    ]


def test_package_retains_references_to_all_frozen_case_evidence():
    documents = attachment_documents()[:2]
    documents.append(
        {
            "tenant_id": "tenant-a",
            "kind": "evidence_package",
            "record_id": "package_a",
            "payload": {
                "video_id": "vid_b",
                "analysis_id": "ana_b",
                "status": "queued",
                "case_evidence_refs": [
                    {"video_id": "another_video", "analysis_id": "another_analysis"}
                ],
            },
        }
    )
    missing = backup.media_references(documents)["missing_records"]
    assert "evidence_package:package_a:case_evidence:0:video" in missing
    assert "evidence_package:package_a:case_evidence:0:analysis" in missing
