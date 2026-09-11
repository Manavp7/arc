# Recorded-video review

The workbench accepts a short MP4, samples its actual pixels, and evaluates image-space entry/dwell rules. Review events are persisted separately from live alerts. Creating a case from a completed event is an explicit action; video analysis does not publish live alarms or dispatch resources.

## Setup

Use the repository bootstrap described in README for a fresh checkout. In an existing checkout:

```bash
uv sync
ffmpeg -version
ffprobe -version
just services
just db-init
just dev-lite
```

`just db-init` must include the workbench document/revision migration. PostgreSQL stores video metadata, configuration, analysis results and revisions. Media files live on the API host under `SIO_DATA_DIR/video_review`; that directory and the database must both persist across restarts. Run one API process for this prototype.

The API package declares OpenCV, NumPy and the perception package. A working `ffmpeg` and `ffprobe` on PATH are also required; a binary that exists but cannot execute disables upload. Verify both version commands and restart the API after repairing dependencies. `just models` installs the repository's optional model assets; their manifest includes checksums and licences.

Open the console, sign in, and select **Footage & cases → Footage**. Upload a clip, draw a polygon on its image, save an enabled rule, and analyze. A new analysis runs against saved configuration. Rule preview evaluates proposed configuration against the latest analysis when it is completed, without changing saved records. If the latest run failed or was interrupted, run analysis successfully before previewing; preview does not fall back to an older run. Save and analyze again to establish a new authoritative result.

## What the result means

- With working local ONNX weights, the existing YOLO detector supplies classes, boxes and confidence. The run records the loaded model name and weights SHA-256. Available files are only a capability hint; the analysis record reports what actually ran.
- Without usable ONNX weights, OpenCV measures foreground changes relative to the clip's first frame. This mode labels regions `motion`, with `confidence: null`. It cannot classify a person, vehicle or fire. Rules requesting those classes fail explicitly. Camera movement, changed lighting and objects present in the first frame can make this baseline unreliable.
- Region tracking associates nearby boxes of the same class. Its IDs are local geometric tracks, not personal identities or proven cross-camera identities.
- Zones use normalized image coordinates, not surveyed positions. Occupancy uses a box's bottom-centre point. An entry event means the track was first observed inside a zone, including a track that first appears there; it is not proof that a boundary crossing was observed.
- Dwell requires repeated occupancy observations, allowing gaps up to 1.1 seconds. Longer gaps reset the visit. One rule emits once per visit after its threshold, with a cooldown per rule/track. Event times are clip offsets in seconds at the sampling resolution.
- Changing saved configuration does not rewrite the last completed run. Each run retains its own rules/zones/model snapshot. Preview events are proposed results; authoritative case links use persisted events from a completed run.

## Privacy and storage

Original uploaded video is retained privately to support analysis and reruns. It has no download endpoint. It is not governed by the camera pipeline's `SIO_RETAIN_RAW` setting.

Normal playback is a derivative with the entire frame reduced to a 16-by-16 pixel grid and enlarged, with audio, subtitles and metadata removed. Evidence JPEGs use that same pixelation before drawing region labels/boxes. This intentionally obscures fine detail; it is **not a guarantee of anonymization**. Coarse scene information and annotations remain visible to authorized tenant readers.

All video, analysis and media lookups use the authenticated tenant. Files use generated identifiers under a tenant-specific directory; client filenames are display titles only. Served media responses use private, non-cacheable headers. Failed uploads remove their temporary media. A completed upload retains its original, playback derivative and poster; each analysis version retains its evidence frames. Reruns preserve earlier analysis/evidence so existing case references remain usable.

The storage manager inventories bytes for originals, playback derivatives, posters and all retained analysis frames. Its total excludes database files, site floorplans, evidence-package exports and backups. Counts are marked incomplete if inspection fails; cleanup fails closed when a reference scan reaches its 5,000-record bound. Camera/MinIO retention rules do not automatically cover this directory.

Retention settings select clips older than `archive_after_days` (default 30) for a cleanup preview. Automatic archive and automatic purge remain off. An administrator may explicitly select an eligible newer clip for manual archive. Archive hides the clip from the active library, retains all files privately, and can be restored. **Archive reclaims zero bytes and archived clips still count toward the 20-upload quota.** Active jobs and case, evaluation-draft, frozen annotation-set, evaluation-report or evidence-package references protect the entire video and every analysis version.

Permanent deletion is a separate explicit action: archive first, request a purge preview, then confirm exactly the eligible video IDs. The preview is actor-bound, tenant-bound, expires after ten minutes and can be used once. Purge rechecks current references, jobs, files and video revision under the same local mutation lock used by case/evaluation/package creation. A fresh link or edit invalidates the preview. Purge removes media only after recording that deletion has begun; the video tombstone, analysis records and immutable database revision history remain. Deleted media cannot be restored by the app. A file failure reports `purge_failed`, leaves any remaining media private and requires another preview after inspection. No deletion is scheduled automatically.

Global search omits archived, purging, purged or unavailable video records and their analysis events. A retained analysis row alone does not make a footage result available. Existing case snapshots remain readable; their references prevent normal storage cleanup from archiving or deleting the supporting clip. New case creation rechecks the video's lifecycle under the cleanup lock and rejects unavailable or pending-deletion media.

Run only one API process: the in-process mutation locks serialize local writers, while PostgreSQL revisions reject stale updates. A distributed retention service would need database transactions/leases spanning reference creation and cleanup. Coordinated backups and a tested restore remain necessary before purging material that must be retained.

## Limits and recovery

| Limit | Value |
| --- | --- |
| Input | One MP4 video stream, maximum 100 MiB |
| Duration / resolution / rate | 180 seconds, 1920 x 1080, 60 fps |
| Upload body deadline | 60 seconds |
| Stored uploads | 20 per tenant; at most about 2 GiB of original uploads, plus derivatives/results |
| Analysis sampling | 2 fps, maximum 360 sampled frames |
| Detections | Maximum 32 regions per sampled frame |
| Configuration | 12 zones, 3–20 vertices each; 24 rules |
| Review events | Maximum 500 returned events per evaluation |
| Upload I/O | At most two uploads across tenants; one per tenant |
| Processing | One heavy decoder/converter at a time, shared with evidence exports |
| Pending jobs | Maximum 50 per tenant; one active job per video |
| Automatic recovery | Maximum three attempts per job |
| Local job history | Maximum 5,000 jobs per tenant before new enqueue is refused |

Conversion has a 120-second subprocess timeout. The analysis loop stops after its five-minute budget; a currently executing decoder/model call must return before the loop can check that budget. Full-duration ONNX performance depends on the machine and model.

Progress and partial detections are periodically persisted. Failed and cancelled runs never become completed case evidence. Analysis requests create durable PostgreSQL-backed jobs; multiple videos wait in enqueue order for one processor. Uploads can receive bounded input while another analysis runs; derivative conversion waits for the same processor lock. Repeated analyze requests for a video with an active job return that existing analysis. Configuration edits are rejected while a job is queued or running.

Queued cancellation is immediate. Running cancellation becomes `cancelling` and completes after the current native decoder/model call returns. No thread is killed while reading media. Shutdown stops processing at the next safe frame boundary and leaves pending jobs durable.

At startup the manager discovers tenants with persisted jobs. A job whose process stopped retains the old attempt as `interrupted` and creates a new analysis ID from the original saved configuration. Recovery restarts at the clip's beginning; it does not invent a resumable tracking checkpoint. Up to three attempts are allowed, after which the job is failed with an explicit recovery-limit error. Operator retry of a failed/cancelled/interrupted job creates a new job and analysis, preserves the original rule/zone snapshot and retains every previous version. Detection/model failures require explicit retry; they are not retried indefinitely. Runs created before the queue was installed remain interrupted with a request to analyze again.

There is no distributed worker/lease. Multiple API processes are unsupported. Durable PostgreSQL metadata and `SIO_DATA_DIR/video_review` must remain together across restarts; this queue is not a backup.

## HTTP contract

All endpoints are below `/api/review/videos`. Reads require an authenticated read role. Writes require `review.write` (operator, commander, integrator, ML engineer or admin). Existing session bearer/cookie authentication applies; there are no token query parameters.

| Method / suffix | Result |
| --- | --- |
| `GET /` | `{videos, capabilities}` |
| `POST /` | Raw `video/mp4` body, URL-encoded `X-Filename`; returns a ready video after derivative creation |
| `GET /{video_id}` | Video metadata, revision, saved zones/rules and latest analysis pointer |
| `PUT /{video_id}/configuration` | `{revision, zones, rules}`; stale revisions return 409 |
| `POST /{video_id}/analyze` | Empty body; 202 with queued analysis and `job_id`. Multiple videos queue; same-video active requests are idempotent |
| `GET /{video_id}/analysis` | Latest status, progress, model, sampled detections and persisted events; optional `?analysis_id=ana_...` selects an exact retained version |
| `POST /{video_id}/preview` | `{zones, rules}`; returns proposed events using completed detections, without mutation |
| `GET /{video_id}/media` | Pixelated MP4 with byte-range support |
| `GET /{video_id}/poster` | Pixelated poster JPEG |
| `GET /{video_id}/frames/{analysis_id}/{frame_index}` | Pixelated, annotated evidence JPEG from that run |

A zone is `{zone_id, name, points: [[x,y], ...]}` with finite coordinates in `[0,1]` and a non-intersecting polygon. A rule is `{rule_id, name, zone_id, event_type: "entry"|"dwell", target_class, threshold_s, cooldown_s, enabled}`. Threshold and cooldown are seconds between 0 and 180. Motion-mode targets are `any` or `motion`.

Video records include `video_id` (`id` is an alias), `title`, `duration_s`, `width`, `height`, `status`, `media_url`, `poster_url`, `revision`, `zones`, `rules`, `analysis_id`, `privacy: "full_frame_pixelation"` and `source: "recorded_file"`.

Analysis status is `queued`, `running`, `completed`, `failed`, `cancelled` or `interrupted`; progress is 0–1. Each sampled frame includes `at_s`, `frame_index`, `frame_url`, and `objects` with `track_id`, `class_name`, `confidence` and normalized `[x1,y1,x2,y2]` boxes. An event includes stable `event_id`, `analysis_id`, `video_id`, `at_s`, `end_s`, `started_at_s`, `rule_id`, `zone_id`, `track_id`, `title`, `confidence` and `frame_url`. Entry/dwell events are emitted instants, so `end_s` equals `at_s`; `started_at_s` records the observed visit start.

When opening footage from a case, pass the case's `analysis_id` to retrieve its exact analysis snapshot. Omitting it selects the latest run and can show different results after a rerun. Malformed, missing, foreign-tenant or different-video analysis identifiers return 404.

## Queue and storage HTTP contract

Queue routes require existing `review.read` / `review.write` permissions. Storage reads require admin or integrator; storage mutations require admin. Existing tenant/session authorization applies to every route.

| Route | Contract |
| --- | --- |
| `GET /api/review/jobs` | `{jobs, capabilities}`; jobs newest first, including job/analysis/video IDs, revision, progress, attempt, timestamps, retained `analysis_ids` and error |
| `POST /api/review/jobs/{job_id}/cancel` | `{revision}`; cancel queued work immediately or request cooperative running cancellation |
| `POST /api/review/jobs/{job_id}/retry` | `{revision}`; 202 new job against the failed/cancelled job's configuration snapshot |
| `GET /api/review/storage` | `{usage, settings, videos}`; byte/count totals and each video's protection reasons |
| `PUT /api/review/storage/settings` | `{revision, archive_after_days}`; revision 0 creates settings, days 1–3650; automatic deletion stays off |
| `POST /api/review/storage/preview` | `{}` applies age settings; `{video_ids:[...]}` previews manual selection; reports eligibility, revision, bytes and links |
| `POST /api/review/storage/archive` | `{videos:[{video_id,revision}]}`; fresh protection checks, reversible logical archive, zero reclaimed bytes |
| `POST /api/review/storage/restore/{video_id}` | `{revision}`; restores an archived record if permanent deletion has not started |
| `POST /api/review/storage/purge-preview` | `{video_ids:[...]}`; archived-only eligibility, 10-minute one-use actor-bound `preview_token`, exact revisions and reclaimable bytes |
| `POST /api/review/storage/purge` | `{preview_token,confirmed_video_ids:[...]}`; exact explicit eligible-ID confirmation, fresh validation, irreversible media deletion and retained tombstone |

Selections are bounded to 20 distinct videos. Cleanup response reasons distinguish `case_linked`, `evaluation_linked`, `evidence_package_linked`, `active_job`, `archive_first`, `within_retention_period`, stale/missing metadata and incomplete scans. Reads of purged video/media return 404; tombstones remain in storage inventory for audit.

## Verification

```bash
.venv/bin/pytest tests/unit/test_video_review.py tests/unit/test_video_jobs_storage.py
.venv/bin/ruff check services/api/src/sio_api/video_*.py tests/unit/test_video_review.py
```

The tests author a small moving-rectangle MP4 and exercise actual decoding, pixelation, rule events, media ranges, auth/tenant boundaries, configuration conflicts, preview non-mutation, failure cleanup and retained evidence across reruns. They verify the connected implementation, not accuracy on real sites or privacy coverage. The conversion test explicitly skips when working FFmpeg tools are unavailable.

Queue/storage tests also verify serial processing, duplicate enqueue, queued/running cancellation, new-version recovery and three-attempt exhaustion, saved-configuration retry, stale cleanup revisions, late case/evaluation/package references, shared mutation serialization, archive/restore, and expired/reused/wrong-actor purge tokens. Permanent deletion tests use only files authored under pytest temporary directories. No runtime user footage is deleted by the verification suite.
