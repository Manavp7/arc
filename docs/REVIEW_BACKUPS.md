# Offline local backup and isolated restore

`scripts/review_backup.py` backs up the **entire configured PostgreSQL database**, including all tenants and revision history, together with the local durable media folders. It is a host-operator CLI, not a tenant web permission or recurring automation. Backups contain private originals and potentially sensitive source settings; store them in trusted private storage.

## Requirements and consistency boundary

- Use the deployment's existing `SIO_*` environment, especially `SIO_DATA_DIR`, PostgreSQL connection settings and `SIO_BLOB_BACKEND=file`. Passwords are passed to PostgreSQL tools through the process environment and are not printed or placed in command arguments.
- Stop the supervisor and every standalone SIO writer first, including API, ingest, consumers and external ingestion scripts. Keep them stopped until creation finishes. The CLI never stops or starts a service. `--confirm-writers-stopped` explicitly confirms standalone writers too.
- Live PIDs in `data_dir/run/supervisor.json` cause rejection even with the confirmation flag. Creation also rejects other client connections to the configured database and checks workbench records and media inventories before/after copying. These checks complement operator coordination; they are not an online transactional snapshot across arbitrary external writers.
- PostgreSQL itself stays running. `pg_dump`/`pg_restore` must be available and compatible with the server. The CLI uses `PATH`, then Homebrew PostgreSQL 17 paths on Apple Silicon/Intel. Restoring requires permission to create a database and the server extensions used by the source schema.
- Use real physical local paths. Symlinks, nonregular files, unsafe manifest paths and changed files are rejected. Destinations must be outside the configured live data directory. Create their parent folders first.

## Create and verify

After stopping writers, from the repository and deployment environment:

```sh
.venv/bin/python scripts/review_backup.py create \
  --output /absolute/private/backups/sio-2026-09-11 \
  --confirm-writers-stopped

.venv/bin/python scripts/review_backup.py verify \
  --backup /absolute/private/backups/sio-2026-09-11
```

The output directory must not already exist. The database dump uses PostgreSQL custom format with `--no-owner --no-acl`. The following paths under `SIO_DATA_DIR` are included when present:

- `video_review/`: private originals, protected playback, posters and every retained analysis frame.
- `site_plans/`: uploaded floorplans.
- `blobs/`: the configured local `FileBlobStore` root used for platform frame evidence.
- `sources.json`: durable source configuration, which may itself be sensitive.
- `evidence_packages/`: generated clips, case exports, annotation manifests and ZIP packages.

The backup excludes the PostgreSQL cluster directory, Redis state, models, logs, runtime PID files, environment files and unrelated fixture/output folders. It does not include external object stores or externally referenced source/sample files. Model weights and external source inputs must be made available separately when recommissioning processing; do not assume that restoring metadata makes an external camera or model available.

New backup directories have mode `0700`, files `0600`. `manifest.json` records each included file's length and SHA-256 plus the directory inventory. `references.json` identifies known local evidence references and document counts. Verification checks all hashes, missing/unexpected files and directories, symlinks, and required original/derivative/frame/case/site/package media. Retained analysis history for a video whose purge completed (`status=purged` with `purged_at`) does not require its deliberately deleted media. Missing parents, failed/partial purges, and case/evaluation/package references to a purged video remain integrity failures. SHA-256 detects corruption relative to the manifest; it is not a digital signature or an authenticity guarantee for an untrusted backup.

Reference checks include every additional case evidence attachment, its retained analysis and resolved platform frames, and each source captured in a package's case context. Missing or mismatched source records fail verification. Versioned rule presets, previews, notification records, read states and reconciliation markers are included with the full workbench database history.

## Restore into a separate database and data directory

```sh
.venv/bin/python scripts/review_backup.py restore \
  --backup /absolute/private/backups/sio-2026-09-11 \
  --target-data /absolute/private/restores/sio-2026-09-11 \
  --target-database sio_restore_20260911 \
  --confirm-writers-stopped
```

The target database must be a new lowercase name, distinct from the configured/source database and PostgreSQL system databases. It is created on the **same configured PostgreSQL host**. Existing databases are rejected; no database is dropped or overwritten. The target data directory must be new or empty, separate from the live data and backup directories.

All backup hashes and reference paths are validated before any restore target is created. PostgreSQL restores into the newly created database with a single transaction; local files are copied privately. The command then rechecks the backup, inspects restored workbench references, and verifies the restored local evidence files. Success writes `RESTORE_COMPLETE.json` with counts. Queued/pending jobs are preserved for normal startup recovery, and no services are started. Live environment/configuration is unchanged.

A restore drill is complete only when the command succeeds against a real PostgreSQL instance. Unit tests with mock tools verify guards and control flow, not PostgreSQL extension compatibility or actual restoration. Subsequent application startup against the isolated restored environment is a separate operator decision; avoid starting ingestion into the source deployment during a drill.

## Failure handling

A failed creation retains a new directory marked `INCOMPLETE.json`; verification refuses it. A failed restore retains only its newly created database/data target and `RESTORE_INCOMPLETE.json`. Nothing is deleted automatically, and the live deployment is untouched. Inspect and deliberately remove those partial targets before choosing a fresh destination/name for another attempt. Tool error output is withheld to avoid printing SQL rows or secrets; CLI errors report the failed stage/type.

Automated scheduling, off-host replication, encryption/key management and disaster recovery for external infrastructure are not implemented by this command.
