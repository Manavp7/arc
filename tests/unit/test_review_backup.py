"""Offline backup checks paths, integrity, quiescence and isolated restore targets."""

import hashlib
import importlib.util
import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "review_backup", Path(__file__).resolve().parents[2] / "scripts/review_backup.py"
)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


def settings(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    return SimpleNamespace(
        data_dir=data,
        run_dir=data / "run",
        blob_backend="file",
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_user="sio",
        pg_password="do-not-print",
        pg_database="sio",
    )


def fixture_backup(tmp_path):
    root = tmp_path / "backup"
    root.mkdir(mode=0o700)
    (root / "media" / "blobs").mkdir(parents=True)
    (root / "media/blobs/frame.jpg").write_bytes(b"authored fixture")
    (root / "database.dump").write_bytes(b"PGDMP-test-only")
    refs = {
        "paths": ["blobs/frame.jpg"],
        "missing_records": [],
        "unresolved_references": [],
        "document_count": 0,
    }
    backup.write_json(root / "references.json", refs)
    manifest = {
        "format": "sio-offline-local-backup",
        "version": 1,
        "blob_backend": "file",
        "source_database": "sio",
        "directories": ["media", "media/blobs"],
        "files": {
            name: backup.hash_file(root / name)
            for name in ["database.dump", "references.json", "media/blobs/frame.jpg"]
        },
    }
    backup.write_json(root / "manifest.json", manifest)
    return root


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "media/../escape",
        "media//blobs/frame",
        "media\\blobs",
        "media/./blobs",
        "",
        "media/\x00name",
    ],
)
def test_manifest_paths_reject_traversal_and_ambiguous_names(path):
    with pytest.raises(backup.BackupError):
        backup.safe_relative(path)


def test_verify_checks_coherent_manifest_and_rejects_hash_corruption(tmp_path):
    root = fixture_backup(tmp_path)
    assert backup.verify_backup(root)["version"] == 1
    (root / "media/blobs/frame.jpg").write_bytes(b"changed fixture")
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.verify_backup(root)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_verify_rejects_unexpected_files_directories_and_symlinks(tmp_path, kind):
    root = fixture_backup(tmp_path)
    path = root / "unexpected"
    if kind == "file":
        path.write_text("unexpected")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(root / "database.dump")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(root)


def test_manifest_cannot_trick_restore_into_copying_outside_media(tmp_path):
    root = fixture_backup(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["media/../../private"] = {"bytes": 0, "sha256": "0" * 64}
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.verify_backup(root)


def test_inventory_never_follows_media_root_or_nested_symlinks(tmp_path):
    config = settings(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_text("private")
    (config.data_dir / "blobs").symlink_to(external, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="symlink"):
        backup.media_inventory(config.data_dir)
    (config.data_dir / "blobs").unlink()
    (config.data_dir / "blobs").mkdir()
    (config.data_dir / "blobs/link").symlink_to(external / "secret")
    with pytest.raises(backup.BackupError, match="regular"):
        backup.media_inventory(config.data_dir)


def test_stopped_guard_rejects_live_supervisor_even_with_confirmation(tmp_path):
    config = settings(tmp_path)
    config.run_dir.mkdir()
    (config.run_dir / "supervisor.json").write_text(
        json.dumps({"supervisor_pid": os.getpid(), "processes": {}})
    )
    with pytest.raises(backup.BackupError, match="still running"):
        backup.require_stopped(config, True)
    with pytest.raises(backup.BackupError, match="confirm-writers-stopped"):
        backup.require_stopped(config, False)


def test_local_storage_and_explicit_standalone_confirmation_are_required(tmp_path):
    config = settings(tmp_path)
    backup.require_stopped(config, True)
    config.blob_backend = "minio"
    with pytest.raises(backup.BackupError, match="local file"):
        backup.require_stopped(config, True)


def test_copy_refuses_changed_source_and_never_overwrites_destination(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"before")
    state = backup.file_state(source)
    source.write_bytes(b"after")
    with pytest.raises(backup.BackupError, match="changed"):
        backup.copy_file(source, tmp_path / "target", state)
    destination = tmp_path / "target"
    destination.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        backup.copy_file(source, destination)
    assert destination.read_bytes() == b"preserve"


def test_created_directories_and_files_are_private(tmp_path):
    root = tmp_path / "private"
    backup.private_directory(root)
    source = tmp_path / "source"
    source.write_bytes(b"bytes")
    result = backup.copy_file(source, root / "copy")
    backup.write_json(root / "manifest.json", result)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "copy").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "manifest.json").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "name",
    ["sio", "postgres", "template0", "template1", "Uppercase", "x; DROP DATABASE sio", "../db", ""],
)
def test_restore_database_names_cannot_target_live_system_or_injected_names(name):
    with pytest.raises(backup.BackupError):
        backup.validate_database_name(name, "sio")
    backup.validate_database_name("sio_restore_test", "sio")


def test_restore_never_uses_existing_nonempty_or_live_data_targets(tmp_path, monkeypatch):
    config = settings(tmp_path)
    root = fixture_backup(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_text("keep")
    calls = []
    monkeypatch.setattr(backup, "connect", lambda *args: calls.append(args))
    with pytest.raises(backup.BackupError, match="new or empty"):
        backup.restore_backup(config, root, target, "restore_test", confirmed=True)
    with pytest.raises(backup.BackupError, match="separate"):
        backup.restore_backup(
            config, root, config.data_dir / "nested", "restore_test", confirmed=True
        )
    assert calls == []
    assert (target / "keep").read_text() == "keep"


class FakeConnection:
    def __init__(self, existing=False):
        self.existing = existing

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        if "pg_database" in str(query):
            return SimpleNamespace(fetchone=lambda: (1,) if self.existing else None)
        if "pg_stat_activity" in str(query):
            return SimpleNamespace(fetchone=lambda: (0,))
        raise AssertionError(f"Unexpected query: {query}")


def test_restore_existing_database_is_rejected_before_creating_target(tmp_path, monkeypatch):
    config = settings(tmp_path)
    root = fixture_backup(tmp_path)
    target = tmp_path / "target"
    monkeypatch.setattr(backup, "pg_tool", lambda name: name)
    monkeypatch.setattr(backup, "connect", lambda *args: FakeConnection(existing=True))
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_backup(config, root, target, "restore_test", confirmed=True)
    assert not target.exists()


def test_restore_failure_preserves_only_explicit_incomplete_new_target(tmp_path, monkeypatch):
    config = settings(tmp_path)
    root = fixture_backup(tmp_path)
    target = tmp_path / "target"
    monkeypatch.setattr(backup, "pg_tool", lambda name: name)
    monkeypatch.setattr(backup, "connect", lambda *args: FakeConnection())
    created = []
    monkeypatch.setattr(backup, "create_database", lambda cfg, name: created.append(name))
    monkeypatch.setattr(
        backup, "pg_run", lambda *args: (_ for _ in ()).throw(backup.BackupError("restore failed"))
    )
    with pytest.raises(backup.BackupError, match="restore failed"):
        backup.restore_backup(config, root, target, "restore_test", confirmed=True)
    assert created == ["restore_test"]
    assert (target / "RESTORE_INCOMPLETE.json").exists()
    assert not (target / "RESTORE_COMPLETE.json").exists()
    assert list(config.data_dir.iterdir()) == []


def test_create_backup_excludes_runtime_secrets_and_detects_changed_media(tmp_path, monkeypatch):
    config = settings(tmp_path)
    (config.data_dir / "blobs").mkdir()
    (config.data_dir / "blobs/frame.jpg").write_bytes(b"fixture")
    (config.data_dir / "env.sh").write_text("secret")
    (config.data_dir / "postgres").mkdir()
    (config.data_dir / "postgres/data").write_text("cluster")
    monkeypatch.setattr(backup, "pg_tool", lambda name: name)
    monkeypatch.setattr(backup, "connect", lambda *args: FakeConnection())
    monkeypatch.setattr(backup, "database_records", lambda connection: ([], []))
    monkeypatch.setattr(
        backup,
        "pg_run",
        lambda name, args, *_: Path(args[args.index("--file") + 1]).write_bytes(b"PGDMP-fixture"),
    )
    root = tmp_path / "complete"
    manifest = backup.create_backup(config, root, confirmed=True)
    assert set(manifest["files"]) == {"database.dump", "references.json", "media/blobs/frame.jpg"}
    assert not (root / "INCOMPLETE.json").exists()
    original = backup.copy_file

    def change_during_copy(source, destination, expected=None):
        result = original(source, destination, expected)
        source.write_bytes(b"changed after copying")
        return result

    monkeypatch.setattr(backup, "copy_file", change_during_copy)
    partial = tmp_path / "partial"
    with pytest.raises(backup.BackupError, match="changed during backup"):
        backup.create_backup(config, partial, confirmed=True)
    assert (partial / "INCOMPLETE.json").exists()
    with pytest.raises(backup.BackupError, match="incomplete"):
        backup.verify_backup(partial)


def test_reference_inventory_validates_original_derivatives_analyses_and_case_media(tmp_path):
    tenant = "tenant-a"
    documents = [
        {"tenant_id": tenant, "kind": "video", "record_id": "vid_a", "payload": {}},
        {
            "tenant_id": tenant,
            "kind": "analysis",
            "record_id": "ana_a",
            "payload": {"video_id": "vid_a", "detections": [{"frame_index": 0}]},
        },
        {
            "tenant_id": tenant,
            "kind": "case",
            "record_id": "case_a",
            "payload": {
                "video_id": "vid_a",
                "analysis_id": "ana_a",
                "evidence": {
                    "resolved_frames": [{"media_url": "/media/tenants/tenant-a/frame.jpg"}]
                },
            },
        },
    ]
    refs = backup.media_references(documents)
    key = hashlib.sha256(tenant.encode()).hexdigest()
    assert f"video_review/{key}/vid_a/original.mp4" in refs["paths"]
    assert f"video_review/{key}/vid_a/ana_a/0.jpg" in refs["paths"]
    assert "blobs/tenants/tenant-a/frame.jpg" in refs["paths"]
    for name in refs["paths"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    backup.validate_references(tmp_path, refs)
    (tmp_path / refs["paths"][0]).unlink()
    with pytest.raises(backup.BackupError, match="Referenced media"):
        backup.validate_references(tmp_path, refs)
    orphaned = deepcopy(documents)
    orphaned[-1]["payload"]["analysis_id"] = "ana_missing"
    assert backup.media_references(orphaned)["missing_records"] == ["case:case_a:analysis"]


def test_coordinated_mock_roundtrip_preserves_pending_documents_without_starting_services(
    tmp_path, monkeypatch
):
    config = settings(tmp_path)
    (config.data_dir / "blobs").mkdir()
    (config.data_dir / "blobs/frame.jpg").write_bytes(b"fixture")
    documents = [
        {
            "tenant_id": "tenant-a",
            "kind": "video_job",
            "record_id": "job_a",
            "payload": {"status": "queued"},
        }
    ]
    monkeypatch.setattr(backup, "pg_tool", lambda name: name)
    monkeypatch.setattr(backup, "connect", lambda *args: FakeConnection())
    monkeypatch.setattr(backup, "database_records", lambda connection: (deepcopy(documents), []))
    operations = []

    def run(name, args, *_):
        operations.append(name)
        if name == "pg_dump":
            Path(args[args.index("--file") + 1]).write_bytes(b"PGDMP-fixture")

    monkeypatch.setattr(backup, "pg_run", run)
    created = []
    monkeypatch.setattr(backup, "create_database", lambda cfg, name: created.append(name))
    root = tmp_path / "backup"
    backup.create_backup(config, root, confirmed=True)
    result = backup.restore_backup(
        config, root, tmp_path / "restored", "restore_test", confirmed=True
    )
    assert operations == ["pg_dump", "pg_restore"]
    assert created == ["restore_test"]
    assert result["services_started"] is False
    assert result["verified_documents"] == 1
    assert (tmp_path / "restored/blobs/frame.jpg").read_bytes() == b"fixture"
    assert (tmp_path / "restored/RESTORE_COMPLETE.json").is_file()
    assert not (tmp_path / "restored/RESTORE_INCOMPLETE.json").exists()
    assert documents[0]["payload"]["status"] == "queued"
    assert (config.data_dir / "blobs/frame.jpg").read_bytes() == b"fixture"


def test_corrupt_backup_is_rejected_before_connecting_to_restore_database(tmp_path, monkeypatch):
    config = settings(tmp_path)
    root = fixture_backup(tmp_path)
    (root / "database.dump").write_bytes(b"corrupted")
    calls = []
    monkeypatch.setattr(backup, "connect", lambda *args: calls.append(args))
    with pytest.raises(backup.BackupError, match="integrity"):
        backup.restore_backup(config, root, tmp_path / "restored", "restore_test", confirmed=True)
    assert calls == []
    assert not (tmp_path / "restored").exists()


def purged_documents(*, status="purged", purged_at="2026-09-11T12:00:00Z"):
    return [
        {
            "tenant_id": "tenant-a",
            "kind": "video",
            "record_id": "vid_a",
            "payload": {"status": status, "purged_at": purged_at},
        },
        {
            "tenant_id": "tenant-a",
            "kind": "analysis",
            "record_id": "ana_a",
            "payload": {"video_id": "vid_a", "detections": [{"frame_index": 0}]},
        },
    ]


def test_completed_purge_retains_analysis_history_without_requiring_deleted_media(tmp_path):
    refs = backup.media_references(purged_documents())
    assert refs["paths"] == []
    assert refs["missing_records"] == []
    assert refs["document_count"] == 2
    backup.validate_references(tmp_path, refs)


@pytest.mark.parametrize(
    "status,purged_at",
    [
        ("purge_failed", "2026-09-11T12:00:00Z"),
        ("purging", None),
        ("purged", None),
        ("completed", "2026-09-11T12:00:00Z"),
    ],
)
def test_partial_or_inconsistent_purge_still_requires_original_and_analysis_media(
    tmp_path, status, purged_at
):
    refs = backup.media_references(purged_documents(status=status, purged_at=purged_at))
    assert len(refs["paths"]) == 4
    assert any(path.endswith("original.mp4") for path in refs["paths"])
    assert any(path.endswith("ana_a/0.jpg") for path in refs["paths"])
    with pytest.raises(backup.BackupError, match="Referenced media"):
        backup.validate_references(tmp_path, refs)


def test_missing_parent_is_not_treated_as_a_completed_purge(tmp_path):
    refs = backup.media_references(purged_documents()[1:])
    assert refs["missing_records"] == ["analysis:ana_a:video"]
    assert len(refs["paths"]) == 1
    with pytest.raises(backup.BackupError, match="missing or unsupported"):
        backup.validate_references(tmp_path, refs)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("case", {"video_id": "vid_a", "analysis_id": "ana_a"}),
        ("case", {"analysis_id": "ana_a"}),
        ("evaluation_annotations", {"video_id": "vid_a"}),
        ("evaluation_draft", {"video_id": "vid_a"}),
        ("evaluation_report", {"video_ids": ["vid_a"], "analysis_ids": ["ana_a"]}),
        ("evaluation_report", {"analysis_ids": ["ana_a"]}),
        ("evidence_package", {"video_id": "vid_a", "analysis_id": "ana_a", "status": "ready"}),
        ("evidence_package", {"video_id": "vid_a", "status": "queued"}),
    ],
)
def test_evidence_reference_to_purged_video_fails_closed_even_when_analysis_is_retained(
    tmp_path, kind, payload
):
    documents = [
        *purged_documents(),
        {"tenant_id": "tenant-a", "kind": kind, "record_id": "evidence_a", "payload": payload},
    ]
    refs = backup.media_references(documents)
    assert f"{kind}:evidence_a:purged_video" in refs["missing_records"]
    with pytest.raises(backup.BackupError, match="missing or unsupported"):
        backup.validate_references(tmp_path, refs)
