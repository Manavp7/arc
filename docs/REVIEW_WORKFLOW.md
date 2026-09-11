# Footage, cases and site planning

The **Footage & cases** workspace connects saved recordings to human investigations. Its tools are Footage, Evaluation lab, Processing queue, Operator inbox, Cases, Compare footage, Evidence packages, Search and Review quality. Administration includes Site editor, Camera setup and Storage. These features use the existing authenticated tenant and role policies.

## Review a recording

1. Sign in as an operator, commander or administrator. Open **Footage & cases → Footage** and select up to 20 MP4s, each at most 180 seconds and 100 MiB. The batch reports each file separately and supports cancelling remaining uploads.
2. Pause on a useful frame, select **Draw zone**, click its corners and close the polygon. Name it for the area visible in this camera view.
3. Add an entry or dwell rule. Choose an analysis profile, save configuration, then queue analysis. Processing queue shows persisted job state, attempts and progress, with cancellation and bounded retries. Results explicitly identify motion analysis or the loaded ONNX model.
4. Select a recorded event to seek its footage. **Create case** captures the persisted event, analysis configuration and model provenance.
5. In Cases, assign an owner, due date, review outcome and investigation summary. Add permanent notes, then link an existing mission or create a draft response mission. Activation remains an explicit mission action.
6. Record a resolution reason before resolving the case. Export HTML for printing or JSON for structured handoff. Exports contain saved records, authenticated authorship and currently permitted mission/decision records.

Reopening a case's footage selects the retained analysis version that produced its evidence. A later rerun does not replace that evidence. A draft rule preview is not a persisted finding and cannot be used as authoritative case evidence.

**Analysis profiles** choose motion or the compatible detector already configured on the server, with 1 or 2 fps sampling and an ONNX confidence threshold from 0.05 to 0.95. Availability is checked locally; the controls do not download models. Jobs retain their profile and model hash through retries and restart recovery. Explicit ONNX failures do not silently switch to motion. Motion has no confidence score, and neither mode establishes real-camera accuracy. See [ANALYSIS_PROFILES.md](ANALYSIS_PROFILES.md).

**Footage navigation** provides Space to play/pause, J/L to seek five seconds, K to pause, arrow keys for approximate playback-frame steps, and a speed selector. Shortcuts yield to text editing and zone drawing. Frame steps use the protected rendition's known `playback_fps`; source fps and detector sampling rate are different. The thumbnail strip shows up to 12 actual saved analysis samples, with gaps left visible. See [FOOTAGE_NAVIGATION.md](FOOTAGE_NAVIGATION.md).

**Private bookmarks** save a title and personal note at a timestamp in a completed retained analysis; use **Bookmark** or B when available. Reopening one loads that exact run. Other operators cannot access the note through the bookmark API. Bookmarks are working notes, not detector events, frozen annotations or case evidence, and are excluded from case exports. Active bookmarks protect their referenced recordings from cleanup; deleting a note never deletes footage. See [REVIEW_BOOKMARKS.md](REVIEW_BOOKMARKS.md).

**Reusable rule presets** save a version of the recording's rules using named logical scopes. Select target recordings, explicitly map each scope to that recording's existing image zone, and review the exact append or replace preview. Bulk application reports each recording separately and refuses stale target revisions. Source polygons are not copied and analysis starts only when requested. See [RULE_PRESETS.md](RULE_PRESETS.md).

**Attach an existing evidence source** adds a persisted recorded event or platform alert to an existing case. Each source keeps its saved configuration, model provenance, authenticated author and attachment context. The timeline supports up to 20 sources including the original. Recording offsets remain relative to their own clips; unknown recording clocks are labelled. Reports include all attached snapshots, and evidence packages can select any attached recording. See [CASE_ATTACHMENTS.md](CASE_ATTACHMENTS.md).

**Compare footage** opens two recording sources attached to a case with their exact retained analyses and sampled overlays. Reviewers set a signed offset explicitly, seek or play within the shared interval, and save the alignment note with authenticated authorship. Source capture clocks remain unknown; this is manual alignment. See [EVIDENCE_COMPARISON.md](EVIDENCE_COMPARISON.md).

**Cases from human observations** capture a frozen annotation and a completed analysis even when no detector event exists. Open a case from a saved evaluation miss to include its exact report context, or use a frozen observation as case evidence. Human evidence has no invented detector confidence and stays separate in review-quality counts. See [CASE_ANNOTATIONS.md](CASE_ANNOTATIONS.md).

Existing live alerts also offer **Open case** in the Incident panel. Linked platform events are copied into the case. When stored detection/observation references resolve to indexed redacted frames, those frames are attached. Unresolved historical references are identified rather than replaced with unrelated footage.

## Search and quality

**Operator inbox** groups active, assigned, overdue and upcoming cases using server time. Assign a case to yourself and set its priority, or select cases to capture a permanent shift handover. Handovers retain each case's revision and authenticated author. They remain in the console; this feature does not send external notifications.

The **notification bell** shows your active assignments, upcoming and overdue deadlines, completed or failed analyses and ready or failed exports. It opens the exact case, saved analysis or package. Read state persists across restarts; **Mark visible read** affects only the displayed page. An owner must match the authenticated username exactly. These are in-app notices; email, SMS and browser push are not enabled. See [NOTIFICATIONS.md](NOTIFICATIONS.md).

**Evidence packages** trims up to 60 seconds from a case's pixelated playback, includes timestamped operator annotations, and packages the retained case, HTML/JSON reports and SHA-256 manifest into a downloadable ZIP. The original upload is never included. Pixelation is not a guarantee of anonymization. See [EVIDENCE_PACKAGES.md](EVIDENCE_PACKAGES.md).

**Evaluation lab** supports reviewed coverage, incident windows, frozen annotation versions and comparison of retained analyses. Misses and false alarms are counted only within reviewed zone/event scopes. Label fixture footage as authored, and keep evaluation partitions separate from tuning. A report over an authored fixture does not establish real-camera accuracy. See [EVALUATION_LAB.md](EVALUATION_LAB.md).

Search covers visible entities, events, alerts, recorded files/events, sites and cases. Filter by record type, source, zone or time; save a named personal query to reuse it. Source and zone filters match identifiers. Searches are bounded; broad queries may require narrower filters to retrieve older results.

Review quality summarizes visible case outcomes and response/resolution times. Reviewed precision uses only confirmed and false-positive detector-origin case labels. Cases opened from human annotations are counted separately and excluded from this denominator, even if detector evidence is later attached. Unreviewed cases are shown separately. Recall and missed incidents remain unavailable without independent ground-truth annotations. These case metrics are not detector accuracy measurements over all footage.

## Site editor

An integrator or administrator can create a named site, upload a PNG/JPEG floorplan, draw image-space zones, place camera markers and save a versioned layout. Unsaved edits must be saved or discarded before changing sites. Concurrent stale saves return a conflict and preserve the existing saved revision.

Optional camera pose drafts hold source ID, measured latitude/longitude, bearing, height, tilt, horizontal/vertical field of view and actual frame dimensions. Site layouts and pose drafts do **not** commission live sources, change the simulator's yard or establish geographic accuracy. Commissioning requires surveyed calibration and validation against actual camera footage. Image-space video rules remain separate from floorplan polygons.

**Camera setup** connects an existing RTSP source and site camera to a saved readiness draft. Test the source, review available indexed pixelated imagery, enter a measured pose and at least three non-collinear ground checkpoints, then inspect projection errors against the chosen tolerance. Missing measurements remain missing. Passing these checks records readiness; it does not activate the source or establish survey accuracy. See [CAMERA_COMMISSIONING.md](CAMERA_COMMISSIONING.md).

## Local verification installation

The September 11 local run uses `.sio/review-runtime/` for PostgreSQL, Redis, uploaded review media and operator documents. Its environment is saved in `.sio/review-runtime/env.sh`. This replaces the earlier disposable `/private/tmp/arc-review-runtime` run. The standard repository setup remains described in README and VIDEO_REVIEW.md.

For this existing local installation, from the repository root:

```bash
source .sio/review-runtime/env.sh
# Datastores already initialized; start them only if they are stopped.
/opt/homebrew/opt/postgresql@17/bin/pg_ctl \
  -D "$SIO_DATA_DIR/postgres" -l "$SIO_DATA_DIR/logs/postgres.log" \
  -o '-p 55432 -k /private/tmp -c listen_addresses=127.0.0.1 -c shared_buffers=32MB' start
redis-server --port 56379 --bind 127.0.0.1 \
  --dir "$SIO_DATA_DIR/redis" --appendonly yes --daemonize yes \
  --pidfile "$SIO_DATA_DIR/run/redis.pid" --logfile "$SIO_DATA_DIR/logs/redis.log"
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/supervisor.py --profile lite
```

Open http://127.0.0.1:5173. The API is on port 8000 and consumer health on 8120. Stop the application with Ctrl-C in its supervisor terminal, or with the same environment and `.venv/bin/python scripts/supervisor.py --stop`. The database and Redis keep their state. This is a local development installation with local test identities and loopback PostgreSQL trust authentication; it is not a deployment configuration.

## Storage and operational boundary

PostgreSQL migration `008_workbench.sql` adds tenant-scoped documents, optimistic revisions and append-only revision history. Original footage, derivatives and floorplans live under `SIO_DATA_DIR`. They must be retained together with database records. Store deletion does not remove history; reusing deleted identifiers is unsupported.

**Storage** reports recorded-media usage and age-based cleanup suggestions. Archive is reversible and frees no bytes. Permanent cleanup requires a fresh preview and exact confirmation; case, evaluation, evidence-package and active bookmark references protect their recordings. Automatic retention is off. Administrators manage storage; integrators can inspect it.

Every additional case attachment and every source captured in an export protects its recording and retained analysis. Offline backup validation follows the same additional source references and associated frame media. Frozen annotation/report references, saved comparison sources and active bookmarks receive the same reference validation. Versioned presets, comparison alignments, private bookmark notes and notification read state are included in the database backup; handle it as sensitive operator data.

The host-operator [backup CLI](REVIEW_BACKUPS.md) coordinates PostgreSQL with media, floorplans, blob files, source configuration and evidence packages. Stop all application writers first. It verifies hashes and restores only into a new database and an empty/new data directory, then validates retained references. It does not overwrite or switch the running installation. Scheduled backups, automatic retention expiry, distributed workers and Temporal workflow execution remain deployment work.

See [VIDEO_REVIEW.md](VIDEO_REVIEW.md) for upload limits, model behavior, private-original retention, pixelation limitations and interrupted-job recovery. See [VERIFICATION.md](VERIFICATION.md) for checks actually performed.
