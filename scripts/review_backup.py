#!/usr/bin/env python3
"""Coordinated, offline local SIO database and media backup/restore.

Run from the configured deployment environment. This is a host-operator command
for the full database, not a tenant API. It never stops services or replaces data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "libs/sio_core/src"))
sys.path.insert(0, str(REPO_ROOT / "libs/sio_schemas/src"))
MEDIA_ROOTS = ("video_review", "site_plans", "blobs", "sources.json", "evidence_packages")
BACKUP_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class BackupError(Exception):
    """A checked safety or integrity failure suitable for a secret-free CLI message."""


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise BackupError("Manifest paths must be strings")
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or value != path.as_posix()
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or "\\" in value
        or "\x00" in value
    ):
        raise BackupError("Manifest contains an unsafe relative path")
    return path


def no_symlinks(path: Path) -> None:
    path = path.absolute()
    for current in [*reversed(path.parents), path]:
        if current.is_symlink():
            raise BackupError("Symlink paths are unsupported; use physical local directories")


def file_state(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BackupError("Only regular files can be backed up")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def inventory(root: Path) -> tuple[dict[str, tuple], list[str]]:
    no_symlinks(root)
    files: dict[str, tuple] = {}
    directories = []
    if not root.exists():
        return files, directories
    if not root.is_dir():
        raise BackupError("Expected a directory")
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_dir():
                raise BackupError("Symlinks and special directories are unsupported")
            directories.append(path.relative_to(root).as_posix())
        for name in sorted(filenames):
            path = Path(directory) / name
            files[path.relative_to(root).as_posix()] = file_state(path)
    return files, sorted(directories)


def media_inventory(data_dir: Path) -> tuple[dict[str, tuple], list[str]]:
    no_symlinks(data_dir)
    files, directories = {}, []
    for name in MEDIA_ROOTS:
        path = data_dir / name
        if path.is_symlink():
            raise BackupError("A selected media root is a symlink")
        if not path.exists():
            continue
        if path.is_file():
            if name != "sources.json":
                raise BackupError("Media roots must be directories")
            files[name] = file_state(path)
        else:
            if name == "sources.json":
                raise BackupError("sources.json must be a regular file")
            children, subdirectories = inventory(path)
            files.update({f"{name}/{key}": value for key, value in children.items()})
            directories.extend([name, *[f"{name}/{key}" for key in subdirectories]])
    return files, sorted(directories)


def private_directory(path: Path, *, allow_empty=False) -> None:
    no_symlinks(path)
    if path.exists():
        if not allow_empty or not path.is_dir() or any(path.iterdir()):
            raise BackupError(
                "Destination must be new (or, for restore, empty); nothing is overwritten"
            )
    else:
        if not path.parent.is_dir():
            raise BackupError("Create the destination parent directory first")
        path.mkdir(mode=0o700)
    path.chmod(0o700)


def write_json(path: Path, data: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(data, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path: Path) -> Any:
    no_symlinks(path)
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BackupError("Manifest is missing or exceeds the supported size")
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise BackupError("Manifest is not valid JSON") from exc


def hash_file(path: Path) -> dict:
    no_symlinks(path)
    before = file_state(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if file_state(path) != before:
        raise BackupError("A file changed while its integrity was being checked")
    return {"bytes": before[2], "sha256": digest.hexdigest()}


def copy_file(source: Path, destination: Path, expected: tuple | None = None) -> dict:
    no_symlinks(source)
    no_symlinks(destination)
    before = file_state(source)
    if expected is not None and before != expected:
        raise BackupError("Media changed after backup inventory; keep every writer stopped")
    missing_parents = []
    parent = destination.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    for parent in reversed(missing_parents):
        parent.mkdir(mode=0o700)
    digest, total = hashlib.sha256(), 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, flags), "rb") as incoming, destination.open("xb") as outgoing:
        destination.chmod(0o600)
        while chunk := incoming.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        info = os.fstat(incoming.fileno())
        after_fd = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    if file_state(source) != before or after_fd != before or total != before[2]:
        raise BackupError("Media changed during copy; the incomplete backup must not be used")
    return {"bytes": total, "sha256": digest.hexdigest()}


def process_alive(pid: int) -> bool:
    if pid <= 0:
        raise BackupError("Supervisor state contains an invalid PID")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def require_stopped(settings: Any, confirmed: bool) -> None:
    if not confirmed:
        raise BackupError(
            "Stop all SIO writers and pass --confirm-writers-stopped, including standalone services"
        )
    state_file = Path(settings.run_dir) / "supervisor.json"
    no_symlinks(state_file)
    if state_file.exists():
        state = read_json(state_file)
        try:
            pids = [state.get("supervisor_pid"), *state.get("processes", {}).values()]
            live = [int(pid) for pid in pids if pid is not None and process_alive(int(pid))]
        except (TypeError, ValueError, AttributeError) as exc:
            raise BackupError("Supervisor state cannot be safely interpreted") from exc
        if live:
            raise BackupError(
                "SIO supervisor/services are still running; this command never stops them"
            )
    if settings.blob_backend != "file":
        raise BackupError(
            "This command supports the local file blob backend only; external object stores require their own coordinated backup"
        )


def pg_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/opt/postgresql@17/bin", "/usr/local/opt/postgresql@17/bin"):
        candidate = Path(prefix) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise BackupError(
        f"{name} is unavailable; install PostgreSQL client tools compatible with the server"
    )


def pg_env(settings: Any, database: str) -> dict[str, str]:
    # Pass credentials in the child's environment, never as command arguments or log output.
    return {
        **os.environ,
        "PGHOST": str(settings.pg_host),
        "PGPORT": str(settings.pg_port),
        "PGUSER": settings.pg_user,
        "PGPASSWORD": settings.pg_password,
        "PGDATABASE": database,
        "PGCONNECT_TIMEOUT": "15",
    }


def pg_run(name: str, args: list[str], settings: Any, database: str) -> None:
    result = subprocess.run(
        [pg_tool(name), *args],
        env=pg_env(settings, database),
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if result.returncode:
        # Tool output can contain SQL row contents or secrets. Keep it out of CLI/logs.
        raise BackupError(
            f"{name} failed with exit status {result.returncode}; target remains incomplete"
        )


def connect(settings: Any, database: str):
    import psycopg

    try:
        return psycopg.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            user=settings.pg_user,
            password=settings.pg_password,
            dbname=database,
            connect_timeout=15,
            autocommit=True,
        )
    except Exception as exc:
        raise BackupError("Cannot connect to the configured PostgreSQL server") from exc


def require_database_idle(connection, database: str) -> None:
    count = connection.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() AND backend_type = 'client backend'",
        (database,),
    ).fetchone()[0]
    if count:
        raise BackupError(
            "Other database clients are connected; disconnect every SIO writer before backup"
        )


def database_records(connection) -> tuple[list[dict], list[dict]]:
    documents = []
    if connection.execute("SELECT to_regclass('public.workbench_documents')").fetchone()[0]:
        documents = [
            {"tenant_id": tenant, "kind": kind, "record_id": record_id, "payload": payload}
            for tenant, kind, record_id, payload in connection.execute(
                "SELECT tenant_id, kind, record_id, payload FROM workbench_documents ORDER BY tenant_id, kind, record_id"
            )
        ]
    return documents, []


def media_references(documents: list[dict]) -> dict:
    """Only known local evidence contracts are interpreted; no URL is fetched."""
    paths, unresolved = set(), []
    videos = {
        (row["tenant_id"], row["record_id"]): row["payload"]
        for row in documents
        if row["kind"] == "video"
    }
    analyses = {
        (row["tenant_id"], row["record_id"]): row["payload"]
        for row in documents
        if row["kind"] == "analysis"
    }
    missing_records = []
    records = {
        (row["tenant_id"], row["kind"], row["record_id"]): row["payload"] for row in documents
    }

    def require_annotation(tenant: str, source: dict, owner: str) -> None:
        set_id, annotation_id = source.get("annotation_set_id"), source.get("annotation_id")
        if not set_id and not annotation_id:
            return
        frozen = records.get((tenant, "evaluation_annotations", set_id))
        if (
            not frozen
            or frozen.get("video_id") != source.get("video_id")
            or not any(
                item.get("annotation_id") == annotation_id for item in frozen.get("annotations", [])
            )
        ):
            missing_records.append(f"{owner}:frozen_annotation")
            return
        snapshot = (source.get("evidence") or {}).get("annotation_set") or {}
        if snapshot and snapshot.get("annotation_hash") != frozen.get("annotation_hash"):
            missing_records.append(f"{owner}:annotation_hash_mismatch")
        report_id = source.get("evaluation_report_id")
        if report_id:
            report = records.get((tenant, "evaluation_report", report_id))
            if not report or not any(
                clip.get("video_id") == source.get("video_id")
                and clip.get("analysis_id") == source.get("analysis_id")
                and clip.get("annotation_set_id") == set_id
                and any(
                    item.get("annotation_id") == annotation_id for item in clip.get("misses", [])
                )
                for candidate in report.get("candidates", [])
                for clip in candidate.get("clips", [])
            ):
                missing_records.append(f"{owner}:miss_report")

    def completely_purged(video: dict | None) -> bool:
        # Only the completed purge transition excuses absent media. A stale
        # timestamp or partial/failed deletion must still fail integrity checks.
        return bool(video and video.get("purged_at") and video.get("status") == "purged")

    def require_video(tenant: str, video_id: str | None, owner: str) -> None:
        video = videos.get((tenant, video_id))
        if video is None:
            missing_records.append(f"{owner}:video")
        elif completely_purged(video):
            missing_records.append(f"{owner}:purged_video")

    def require_analysis(tenant: str, analysis_id: str, owner: str) -> None:
        analysis = analyses.get((tenant, analysis_id))
        if analysis is None:
            missing_records.append(f"{owner}:analysis")
        else:
            # Retained analysis metadata may legitimately outlive media, but
            # case/evaluation/package evidence must never reference that state.
            require_video(tenant, analysis.get("video_id"), owner)

    for row in documents:
        tenant, kind, record_id, value = (
            row["tenant_id"],
            row["kind"],
            row["record_id"],
            row["payload"],
        )
        tenant_key = hashlib.sha256(tenant.encode()).hexdigest()
        if kind == "video" and not completely_purged(value):
            for filename in ("original.mp4", "playback.mp4", "poster.jpg"):
                paths.add(f"video_review/{tenant_key}/{record_id}/{filename}")
        if kind == "analysis":
            video_id = value.get("video_id")
            video = videos.get((tenant, video_id))
            if video is None:
                missing_records.append(f"analysis:{record_id}:video")
            if not completely_purged(video):
                for frame in value.get("detections", []):
                    index = frame.get("frame_index")
                    if isinstance(index, int) and index >= 0:
                        paths.add(f"video_review/{tenant_key}/{video_id}/{record_id}/{index}.jpg")
        if kind == "site" and value.get("floorplan_file"):
            paths.add(f"site_plans/{tenant_key}/{value['floorplan_file']}")
        if kind == "evidence_package":
            require_annotation(tenant, value, f"evidence_package:{record_id}")
            require_video(tenant, value.get("video_id"), f"evidence_package:{record_id}")
            if value.get("analysis_id"):
                require_analysis(tenant, value["analysis_id"], f"evidence_package:{record_id}")
            for index, reference in enumerate(value.get("case_evidence_refs", [])):
                owner = f"evidence_package:{record_id}:case_evidence:{index}"
                if not isinstance(reference, dict):
                    missing_records.append(f"{owner}:invalid_reference")
                    continue
                if reference.get("video_id"):
                    require_video(tenant, reference["video_id"], owner)
                if reference.get("analysis_id"):
                    require_analysis(tenant, reference["analysis_id"], owner)
                require_annotation(tenant, reference, owner)
        if kind == "evidence_package" and value.get("status") == "ready":
            for filename in (
                "clip.mp4",
                "case.html",
                "case.json",
                "annotations.json",
                "manifest.json",
                "package.zip",
            ):
                paths.add(f"evidence_packages/{tenant_key}/{record_id}/{filename}")
        if kind == "case":
            sources = [(f"case:{record_id}", value)]
            for index, attachment in enumerate(value.get("evidence_attachments", [])):
                owner = f"case:{record_id}:attachment:{index}"
                if not isinstance(attachment, dict):
                    missing_records.append(f"{owner}:invalid_snapshot")
                    continue
                sources.append((owner, attachment))
            for owner, source in sources:
                require_annotation(tenant, source, owner)
                if source.get("video_id"):
                    require_video(tenant, source["video_id"], owner)
                if source.get("analysis_id"):
                    require_analysis(tenant, source["analysis_id"], owner)
                    analysis = analyses.get((tenant, source["analysis_id"]))
                    if (
                        source.get("video_id")
                        and analysis
                        and analysis.get("video_id") != source["video_id"]
                    ):
                        missing_records.append(f"{owner}:analysis_video_mismatch")
                for frame in source.get("evidence", {}).get("resolved_frames", []):
                    url = frame.get("media_url", "")
                    if url.startswith("/media/"):
                        paths.add("blobs/" + unquote(url[len("/media/") :]))
                    else:
                        unresolved.append(f"{owner}:unsupported_frame_reference")
        if kind in {"review_bookmark", "movement_report"}:
            owner = f"{kind}:{record_id}"
            require_video(tenant, value.get("video_id"), owner)
            require_analysis(tenant, value.get("analysis_id"), owner)
            analysis = analyses.get((tenant, value.get("analysis_id")))
            if analysis and analysis.get("video_id") != value.get("video_id"):
                missing_records.append(f"{owner}:analysis_video_mismatch")
            if analysis and analysis.get("status") != "completed":
                missing_records.append(f"{owner}:analysis_not_completed")
        if kind == "case_comparison":
            owner = f"case_comparison:{record_id}"
            if not records.get((tenant, "case", value.get("case_id"))):
                missing_records.append(f"{owner}:case")
            for index, reference in enumerate(value.get("source_refs", [])):
                ref_owner = f"{owner}:source:{index}"
                if not isinstance(reference, dict):
                    missing_records.append(f"{ref_owner}:invalid_reference")
                    continue
                require_video(tenant, reference.get("video_id"), ref_owner)
                require_analysis(tenant, reference.get("analysis_id"), ref_owner)
                require_annotation(tenant, reference, ref_owner)
        if kind in {"evaluation_annotations", "evaluation_draft"}:
            require_video(tenant, value.get("video_id"), f"{kind}:{record_id}")
        if kind == "evaluation_report":
            for video_id in value.get("video_ids", []):
                require_video(tenant, video_id, f"evaluation_report:{record_id}")
            for analysis_id in value.get("analysis_ids", []):
                require_analysis(tenant, analysis_id, f"evaluation_report:{record_id}")
    for path in paths:
        safe_relative(path)
    return {
        "paths": sorted(paths),
        "missing_records": missing_records,
        "unresolved_references": unresolved,
        "document_count": len(documents),
    }


def validate_references(data_dir: Path, references: dict) -> None:
    if not isinstance(references, dict) or any(
        not isinstance(references.get(key), list)
        for key in ("paths", "missing_records", "unresolved_references")
    ):
        raise BackupError("Invalid evidence reference inventory")
    if references["missing_records"] or references["unresolved_references"]:
        raise BackupError(
            "Recorded evidence contains missing or unsupported references; resolve these before declaring a complete restore"
        )
    for name in references["paths"]:
        relative = safe_relative(name)
        path = data_dir.joinpath(*relative.parts)
        no_symlinks(path)
        if not path.is_file():
            raise BackupError(
                f"Referenced media is missing: {relative.parts[0]} file (path withheld)"
            )


def distinct_path(path: Path, source: Path) -> None:
    # A backup/restore target must not overlap the configured live data tree.
    path, source = path.absolute(), source.absolute()
    if path == source or path.is_relative_to(source) or source.is_relative_to(path):
        raise BackupError("Destination must be separate from the configured live data directory")


def create_backup(settings: Any, output: Path, *, confirmed=False) -> dict:
    require_stopped(settings, confirmed)
    data_dir = Path(settings.data_dir).absolute()
    if not data_dir.is_dir():
        raise BackupError("Configured local data directory does not exist")
    output = output.absolute()
    distinct_path(output, data_dir)
    pg_tool("pg_dump")
    before, directories = media_inventory(data_dir)
    with connect(settings, settings.pg_database) as connection:
        require_database_idle(connection, settings.pg_database)
        documents, _ = database_records(connection)
        references = media_references(documents)
        validate_references(data_dir, references)
    private_directory(output)
    write_json(output / "INCOMPLETE.json", {"started_at": stamp(), "operation": "backup"})
    try:
        (output / "media").mkdir(mode=0o700)
        for name in directories:
            (output / "media" / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        pg_run(
            "pg_dump",
            ["--format=custom", "--no-owner", "--no-acl", "--file", str(output / "database.dump")],
            settings,
            settings.pg_database,
        )
        (output / "database.dump").chmod(0o600)
        files = {"database.dump": hash_file(output / "database.dump")}
        for name, state in sorted(before.items()):
            files[f"media/{name}"] = copy_file(data_dir / name, output / "media" / name, state)
        if media_inventory(data_dir) != (before, directories):
            raise BackupError("Media inventory changed during backup; the result is incomplete")
        require_stopped(settings, confirmed)
        with connect(settings, settings.pg_database) as connection:
            require_database_idle(connection, settings.pg_database)
            after_documents, _ = database_records(connection)
        if documents != after_documents:
            raise BackupError(
                "Database workbench records changed during backup; the result is incomplete"
            )
        write_json(output / "references.json", references)
        files["references.json"] = hash_file(output / "references.json")
        manifest = {
            "format": "sio-offline-local-backup",
            "version": BACKUP_VERSION,
            "created_at": stamp(),
            "source_database": settings.pg_database,
            "blob_backend": "file",
            "media_roots": list(MEDIA_ROOTS),
            "files": files,
            "directories": ["media", *[f"media/{name}" for name in directories]],
            "reference_document_count": references["document_count"],
            "excluded": [
                "PostgreSQL cluster files",
                "Redis state",
                "logs",
                "run state",
                "environment secrets",
                "models",
                "external object stores",
            ],
            "note": "Full configured database and selected local media. SHA-256 checks integrity, not authenticity. Every writer must stay stopped throughout backup.",
        }
        write_json(output / "manifest.json", manifest)
        (output / "INCOMPLETE.json").unlink()
        return verify_backup(output)
    except Exception:
        if not (output / "INCOMPLETE.json").exists():
            write_json(output / "INCOMPLETE.json", {"failed_at": stamp(), "operation": "backup"})
        raise


def verify_backup(backup: Path) -> dict:
    backup = backup.absolute()
    no_symlinks(backup)
    if (backup / "INCOMPLETE.json").exists():
        raise BackupError("This backup is marked incomplete")
    manifest = read_json(backup / "manifest.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "sio-offline-local-backup"
        or manifest.get("version") != BACKUP_VERSION
        or manifest.get("blob_backend") != "file"
    ):
        raise BackupError("Unsupported backup manifest")
    files, directories = manifest.get("files"), manifest.get("directories")
    if not isinstance(files, dict) or not isinstance(directories, list):
        raise BackupError("Manifest must enumerate files and directories")
    if len(set(directories)) != len(directories):
        raise BackupError("Manifest directory paths must be unique")
    if not {"database.dump", "references.json"}.issubset(files):
        raise BackupError("Backup is missing the database dump or reference inventory")
    for name in [*files, *directories]:
        path = safe_relative(name)
        if name not in {"database.dump", "references.json"} and (
            path.parts[0] != "media" or (len(path.parts) > 1 and path.parts[1] not in MEDIA_ROOTS)
        ):
            raise BackupError("Manifest lists an unsupported backup path")
    actual, actual_directories = inventory(backup)
    if set(actual) != {*files, "manifest.json"} or set(actual_directories) != set(directories):
        raise BackupError("Backup contains missing or unexpected files/directories")
    for name, expected in files.items():
        if (
            not isinstance(expected, dict)
            or not isinstance(expected.get("bytes"), int)
            or expected["bytes"] < 0
            or not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("sha256", "")))
        ):
            raise BackupError("Manifest contains invalid file integrity metadata")
        if hash_file(backup / name) != expected:
            raise BackupError("Backup file integrity verification failed")
    references = read_json(backup / "references.json")
    validate_references(backup / "media", references)
    return manifest


def validate_database_name(name: str, current: str, source: str = "") -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) or name in {
        current,
        source,
        "postgres",
        "template0",
        "template1",
    }:
        raise BackupError(
            "Choose a new lowercase database name distinct from the configured/source database and system databases"
        )


def create_database(settings: Any, name: str) -> None:
    from psycopg import sql

    with connect(settings, settings.pg_database) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            raise BackupError(
                "Restore database already exists; this command never overwrites or drops databases"
            )
        try:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
        except Exception as exc:
            raise BackupError(
                "Could not create the new restore database; existing databases remain unchanged"
            ) from exc


def restore_backup(
    settings: Any, backup: Path, target_data: Path, target_database: str, *, confirmed=False
) -> dict:
    require_stopped(settings, confirmed)
    manifest = verify_backup(backup)
    validate_database_name(
        target_database, settings.pg_database, manifest.get("source_database", "")
    )
    target_data = target_data.absolute()
    distinct_path(target_data, Path(settings.data_dir).absolute())
    distinct_path(target_data, backup.absolute())
    no_symlinks(target_data)
    if target_data.exists() and (not target_data.is_dir() or any(target_data.iterdir())):
        raise BackupError("Restore data directory must be new or empty")
    pg_tool("pg_restore")
    # Check existence before creating any target files. CREATE DATABASE repeats the guard.
    with connect(settings, settings.pg_database) as connection:
        if connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target_database,)
        ).fetchone():
            raise BackupError("Restore database already exists; nothing will be overwritten")
    private_directory(target_data, allow_empty=True)
    marker = target_data / "RESTORE_INCOMPLETE.json"
    write_json(marker, {"started_at": stamp(), "database": target_database, "operation": "restore"})
    try:
        create_database(settings, target_database)
        pg_run(
            "pg_restore",
            [
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                target_database,
                str(backup.absolute() / "database.dump"),
            ],
            settings,
            target_database,
        )
        for name in manifest["directories"]:
            relative = safe_relative(name)
            if len(relative.parts) > 1:
                target_data.joinpath(*relative.parts[1:]).mkdir(
                    parents=True, exist_ok=True, mode=0o700
                )
        for name, expected in manifest["files"].items():
            if name.startswith("media/"):
                result = copy_file(backup / name, target_data / name[len("media/") :])
                if result != expected:
                    raise BackupError("Backup media changed during restore")
        # Recheck dump/manifest as well, and compare restored references with the saved snapshot.
        verify_backup(backup)
        with connect(settings, target_database) as connection:
            documents, _ = database_records(connection)
        references = media_references(documents)
        if references != read_json(backup / "references.json"):
            raise BackupError(
                "Restored database evidence references differ from the backup inventory"
            )
        validate_references(target_data, references)
        complete = {
            "completed_at": stamp(),
            "database": target_database,
            "source_backup": str(backup.absolute()),
            "verified_media_files": sum(name.startswith("media/") for name in manifest["files"]),
            "verified_documents": references["document_count"],
            "services_started": False,
            "note": "Pending jobs are preserved for normal startup recovery. This command has not started services or changed the live configuration.",
        }
        write_json(target_data / "RESTORE_COMPLETE.json", complete)
        marker.unlink()
        return complete
    except Exception:
        # Keep only newly created targets, explicitly incomplete. Never drop a database or delete data.
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create",
        help="Back up the full configured database and local durable media; all writers stopped",
    )
    create.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New backup directory outside the live data directory",
    )
    create.add_argument(
        "--confirm-writers-stopped",
        action="store_true",
        help="Confirm supervisor AND standalone SIO writers stay stopped throughout the operation",
    )
    verify = commands.add_parser(
        "verify", help="Read-only hash, path and recorded-reference verification"
    )
    verify.add_argument("--backup", required=True, type=Path)
    restore = commands.add_parser(
        "restore",
        help="Restore into a NEW database and NEW/empty data directory; never changes live configuration",
    )
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--target-data", required=True, type=Path)
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--confirm-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_backup(args.backup)
            print(f"Verified {len(result['files'])} files; backup format {result['version']}.")
        else:
            from sio_core.config import get_settings

            settings = get_settings()
            if args.command == "create":
                result = create_backup(
                    settings, args.output, confirmed=args.confirm_writers_stopped
                )
                print(f"Backup verified: {args.output.absolute()} ({len(result['files'])} files).")
            else:
                result = restore_backup(
                    settings,
                    args.backup,
                    args.target_data,
                    args.target_database,
                    confirmed=args.confirm_writers_stopped,
                )
                print(
                    f"Restore verified in new database {result['database']} and {args.target_data.absolute()}. Services remain stopped; live configuration is unchanged."
                )
        return 0
    except BackupError as error:
        print(f"Backup operation stopped: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"Backup operation failed ({type(error).__name__}); any newly created target remains marked incomplete. No live database or media was overwritten.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
