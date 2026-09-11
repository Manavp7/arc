# Local verification — 11 September 2026

This records local development checks, not production certification. The earlier September 9 run is retained below as historical evidence.

## Object search and saved movement snapshots

Added **Footage & cases → Objects** and the **Movement and occupancy** panel within retained footage review. They use existing saved detections, so searching or calculating a report does not rerun inference. See [RECORDED_INSIGHTS.md](RECORDED_INSIGHTS.md).

### Automated checks

- Full Python unit suite: **1,777 passed, 33 skipped**, five existing dependency/numerical warnings, in **291.66 seconds**. Report: `.sio/review-runtime/unit-insights-final.xml`. The suite includes local supervisor tests with permission to inspect their own processes and create loopback listeners. The final pagination-boundary and typing adjustments subsequently passed **69 focused tests**; that focused run includes one additional offset-validation case.
- **120 frontend tests passed**, including search pagination through the 50,000-observation limit, protected retained-analysis navigation, counting-line geometry, configuration comparison and occupancy display helpers. TypeScript and the Vite production build passed (**2,332 modules**, main bundle approximately **165 kB gzipped**). Report: `.sio/review-runtime/frontend-insights-final.log`.
- Ruff lint/format passed across **353 Python files**. The configured mypy gate passed for **45 core/schema files**, scoped typing passed for the four changed insight/math/store/storage modules, and all **17 exported contracts** matched.
- Actual PostgreSQL execution of the new bounded metadata query returned seven retained analyses with the expected public-sample classes and sample count, without returning the detection frames. Unit coverage includes malformed samples, finite-segment crossings, gaps, jitter, tenant/zone permissions, current and captured source scope, quotas, deduplication, archive races, backup references and spreadsheet-safe CSV text.

### Browser and persistence checks

Verified at the normal **1280 × 720** viewport and **390 × 844**, then restored the normal viewport:

- Object search found eight local tracks across four accessible completed recordings. Filtering the public still-image recording to `person`, confidence at least **0.8**, and **1–3 seconds** returned three tracks with five matching samples each. Raising confidence to **0.99** displayed the empty state. Opening a selected result loaded its exact retained analysis and paused protected playback at **3 seconds**; mobile navigation also opened the selected **5.5-second** sample.
- Saved **Public still sample - person occupancy test** (`movement_963ddccc6f31f0506df096ecba492d03`) against analysis `ana_4db57a8e5591409f8975e710319ad22d`. It records **peak 4 / mean 4 / 12 samples**, with the person filter and no counting line.
- Drew a finite line with two browser clicks on the existing authored moving-rectangle clip. The calculation found **one B → A crossing between 3.5 and 4 seconds**. Reversing the endpoints produced **one A → B crossing**. Selecting the interval paused playback at **4 seconds**. Saved the reversed calculation as **Authored motion - direction and occupancy test** (`movement_b9d011881c1826c89689fb33112d491f`) against `ana_01ff699a123e4127829269092b179184`: **peak 1 / mean 0.83 / 12 samples**.
- Downloaded both saved CSVs from the browser, including at phone width. API verification confirmed the exact report/analysis identifiers on every row, 13 rows for the occupancy report and 14 rows for the crossing report, including its direction and supporting interval.
- Unsaved movement changes blocked navigation; saving released the guard. Saved snapshot selectors, search controls, retained sample actions, occupancy results and CSV controls remained reachable at 390 pixels, with no horizontal document overflow. The final browser warning/error log was empty.
- After a supervised API restart, both reports retained their source IDs, filters, line geometry and counts. The browser reopened the saved public occupancy report. Existing cases remained at revisions **7** and **3**, the human comparison at **1**, the earlier motion-test recording at **10**, and the private bookmark at **3**. The existing human evidence package stayed ready with its unchanged archive hash. Read-only reference validation passed for **64 workbench documents** and **112 media paths**. API PostgreSQL, bus and blob checks passed.

Evidence is saved in `.sio/review-runtime/insights-verification.json`, `insights-references.json`, and the two report-ID CSV files. These are public-still and authored-motion workflow fixtures, not camera accuracy measurements. Occupancy is sampled; tracks remain local to an analysis; crossing times are intervals between samples. Reports preserve source media against archive/purge. No new model, semantic search, live-camera integration, external publication or physical action was added. This batch verifies restart persistence and backup references, not another offline restore drill or sustained production load.

---

## Official YOLO26 Nano installation

Installed the official [Ultralytics v8.4.0 YOLO26 Nano ONNX asset](https://github.com/ultralytics/assets/releases/tag/v8.4.0), licensed AGPL-3.0, at `.sio/models/yolo26n.onnx`. Its **9,941,957 bytes** and SHA-256 **`2e947b787d9e787b93a16772a5f55b1d4d8c4d86f53146149c5d6a642442d6f7`** match `infra/models.json`. Only this model was installed. The existing adapter loaded its FP32 `[1, 3, 640, 640]` input, `[1, 300, 6]` output and 80 embedded class labels without a source-code change.

- **62 focused detector/profile tests passed**: 61 passed with the optional public-image case initially skipped, followed by that real-image case passing after its sample was obtained. No full-suite rerun was needed for this model installation and documentation update.
- Local inference on the official [public bus image](https://github.com/ultralytics/assets/blob/main/im/bus.jpg) returned **one bus and four people** at confidence **0.35**. The adapter's real-image regression passed. Five repeated single-image calls had a median **381.88 ms** on the current CPU runtime; this small timing sample is not a sustained throughput benchmark.
- Created a clearly labelled **six-second repeated-still test video**, `Ultralytics public sample - still-image test.mp4`, and ran it from the browser with **yolo26n.onnx / 2 samples per second / 0.35 confidence**. Analysis `ana_4db57a8e5591409f8975e710319ad22d` retained **12 samples**, each with one bus and four people. A test-only person dwell rule produced **four findings at 2 seconds**. The browser displayed protected playback, labelled boxes, sample thumbnails, findings and the exact model hash. No case was created from these test findings.
- Restored the stopped local supervisor and restarted the existing lite development stack. The browser and API reopened the same completed analysis with its original settings and findings. PostgreSQL, bus and blob checks passed, the alerts service responded, all **17 consumer checks passed**, and the API reported zero errors.
- Existing original and human-observation cases remained at revisions **7** and **3**; the saved human comparison stayed at revision **1**, the previous motion-test recording at revision **10**, and the private bookmark at revision **3** with its older analysis and timestamp. Read-only reference validation passed for **62 workbench documents** and **112 media paths**.

Reports: `.sio/review-runtime/yolo26-install-verification.json`, `yolo26-video-analysis.json` and `yolo26-runtime-verification.json`. Sample attribution is retained in `.sio/review-runtime/fixtures/ultralytics-sample-source.json`. See [ANALYSIS_PROFILES.md](ANALYSIS_PROFILES.md) for selecting the installed detector. Live sources retain their separate synthetic detector configuration.

This validates real model inference and the local recorded-review workflow on a public still-image fixture. It does not measure accuracy on the user's camera footage, sustained processing performance or live-camera operation. Earlier statements below that weights were absent describe their historical verification batches.

---

## Analysis controls and personal footage review

Added explicit local detector selection and 1/2 fps analysis sampling, per-run model provenance, private retained-analysis bookmarks, keyboard playback controls, approximate playback-frame steps and protected sample thumbnails. See [ANALYSIS_PROFILES.md](ANALYSIS_PROFILES.md), [REVIEW_BOOKMARKS.md](REVIEW_BOOKMARKS.md) and [FOOTAGE_NAVIGATION.md](FOOTAGE_NAVIGATION.md).

### Automated checks

- Full Python unit suite: **1,707 passed, 35 skipped**, five dependency/numerical warnings, in **100.77 seconds**. Report: `.sio/review-runtime/unit-controls-final.xml`. Skipped optional cases remain unverified.
- **Nine PostgreSQL integration tests passed** in rollback-only transactions, including fixed analysis profiles through recovery/retry and private bookmark create/edit/reload/delete with revision checks.
- **102 frontend tests passed**, plus TypeScript and Vite production build (**2,326 modules**, main bundle approximately **156 kB gzipped**). Report: `.sio/review-runtime/frontend-controls-final.log`.
- Ruff lint and format passed across **347 Python files**. The configured mypy gate passed for **45 core/schema files**, scoped typing passed for the six changed profile/bookmark/storage modules, and all **17 exported contracts** matched. A broader optional check including the backup CLI still reports its two existing typing issues; it is not claimed clean.
- Authored tiny ONNX regression models exercise actual inference, embedded labels, threshold changes, missing/replaced weights, and refusal to silently change detectors. These constant-output fixtures establish code behavior, not detector accuracy. Bookmark tests cover private subjects and zones, quotas, archive races, stale revisions, backup references and scope changes during session renewal. Playback tests cover bounded seeks, frame metadata, protected thumbnail provenance and shortcut exclusions.

### Browser and local API checks

Verified desktop **1280 × 900** and narrow **390 × 844** layouts, then restored the normal viewport:

- The existing six-second `authored-h264.mp4` fixture completed an explicit **motion / 1 sample per second** run as `ana_a6966ac58af641a4a319a143269bb9bc`, retaining six sampled frames and its unchanged dwell-only rule. Actual model provenance reports no motion confidence. The local object detector remained visibly unavailable; an explicit ONNX API request returned **422** without replacing the completed analysis.
- Protected playback reports **15 fps**, independently of the uploaded source's **12 fps**. The frame button sought from **2.5s to 2.566666s**; keyboard frame steps also used the protected rendition. J/L seeks stayed inside 0–6s. Speed selection, Space play and K pause worked; typing in the bookmark note left playback paused at its existing position. The protected thumbnail strip sought to its labeled retained timestamps.
- Saved **Authored frame-step review** (`bookmark_5fe38fb7fead456eb64575776c7c32d5`) at **1.133332s**, pinned to older analysis `ana_ba18e477f0644c45825ccb158f0d2829`. After the newer run finished, opening the bookmark selected that older analysis and timestamp with historical configuration editing disabled.
- A second local client changed the saved bookmark while the browser held an edit. The stale save returned **409**, preserved the draft and required an explicit reload. The final note saved as revision **3**. Unsaved notes blocked workspace navigation. A different local administrator subject listed no bookmarks for this recording.
- Controls remained reachable at narrow width with **390px document width in a 390px viewport**. Long retained-analysis identifiers wrapped. The final inspected browser warning/error log was empty.

### Restart persistence and limits

After stopping and restarting the supervised application, the browser restored the new run's **1 sample/s** settings, the bookmark's final note and revision, and its original analysis at **1.133332s**. The original loading-bay case remained at revision **7**, the human-observation case at revision **3**, and its saved comparison at revision **1**. The existing human evidence package remained ready with the same archive hash. Read-only reference validation passed for **57 workbench documents** and **97 media paths**. The API reported healthy PostgreSQL, bus and blob storage with zero processing errors; all **17 consumer health checks** passed. Local evidence summaries are saved as `.sio/review-runtime/controls-verification.json` and `controls-references.json`.

No object-detector weights were downloaded or installed in the running app. Upload limits remain **180 seconds / 100 MiB**. Browser frame steps are approximate playback seeks; these authored fixtures do not establish source-frame accuracy or real-camera detector performance. Bookmarks remain private working notes, separate from case evidence. This batch verifies reference validation and restart persistence; it does not include another offline backup/restore drill or production deployment.

---

## Side-by-side investigations and frozen-observation cases

Added **Compare footage** with exact retained analyses, manually declared time alignment, linked or independent playback, sampled overlays, revision-protected saves and unsaved-navigation protection. Frozen reviewer annotations and exact saved evaluation misses can now open cases or become additional immutable evidence. Human-origin cases have separate review statistics and can produce protected evidence ZIPs. See [EVIDENCE_COMPARISON.md](EVIDENCE_COMPARISON.md) and [CASE_ANNOTATIONS.md](CASE_ANNOTATIONS.md).

### Automated checks

- Final full Python unit suite: **1,643 passed, 35 skipped**, five existing dependency/numerical warnings, in **46.70 seconds**. Report: `.sio/review-runtime/unit-investigation-final.xml`. Optional skipped model/infrastructure cases remain unverified.
- **Eight PostgreSQL integration tests passed** in rollback-only transactions, including actual draft save, freeze, miss report, human-origin case, second evidence attachment and persisted comparison, without changing case history when saving alignment.
- **84 frontend tests passed**, plus TypeScript and Vite production build (**2,318 modules**, main bundle approximately **148 kB gzipped**). Ruff lint/format passed across **339 files**; the configured mypy gate passed for **45 core/schema files**, and **17 exported contracts** matched.
- Regressions cover frozen fingerprints and exact report-miss provenance, tenant/zone permissions, stale case/comparison revisions, distinct source selections, missing media, annotation exports, manual-case precision exclusion, and annotation/comparison backup references. Playback tests cover positive/negative overlap, linked seeks, rejected play, obsolete starts and cancellable decoder readiness.

### Browser checks

Verified in the local in-app browser at **1280 × 900** and **390 × 844**:

- Opened Compare footage from the existing two-source **Authored loading-bay review** case. Its original case stayed at revision 7. Both players loaded their exact retained analyses with saved zones and sampled detections. Saved an explicitly authored +1-second test offset as comparison revision 1, attributed to `console`.
- Linked seeks paired left **2s** with right **3s**. Paired playback advanced at the declared offset, paused together, and stopped at the shared interval end. Independent seeking moved only its selected player; swapping preserved playheads and reversed the offset sign. A non-overlapping offset disabled linked play. Tab navigation with an unsaved alignment stayed in comparison with a save/discard message.
- Browser QA found and fixed a playback-start race from unnecessary seeks. Required seeks now finish decoding before grouped playback begins; redundant seeks are skipped. A separate API response fix exposes completed status in evaluation summaries so the candidate picker includes retained analyses.
- Used the existing six-second `authored-h264.mp4` fixture with an explicitly named **20-second dwell-only rule**. This intentional configuration has no entry rule and produced zero findings. Reviewed and froze an authored entry window at **2.5–3.5s**, with six seconds of reviewed coverage. The saved report recorded **0 matched / 0 false alarms / 1 miss** within that scope. This is a deliberate rule-coverage fixture, not measured real-camera detector accuracy.
- The report's **Open case** created **Authored entry coverage-gap investigation** (`case_af8cc9bca368460f59c68679`) without an event or detector confidence. Its timeline preserved frozen set `ann_b1c3aa27ee8448ee90188aa98452fe62`, analysis `ana_ba18e477f0644c45825ccb158f0d2829`, and report `eval_8d61478b0db04fcd9a0c756190e136e9`. Opening the same frozen observation again reused that case and original report context.
- Confirmed the authored human observation. Review quality showed **one reviewed human-origin case** separately while detector precision remained **1 confirmed / 1 reviewed detector-origin case**. Creating or confirming the case did not change the immutable report.
- Built and downloaded package `pkg_b8a9f8c6d8524ec1962d7f67f232f524` from the human recording at **2–4s**, capturing case revision 2. All four payload hashes and the archive hash verified. The manifest retained annotation/set/report IDs and a null event ID; the report contained the exact frozen snapshot. Archive SHA-256: `6ebf94a69965c6bd23753e5b896dbcde7caf77aaa731b555ea83337173c95e34`.
- Attached an existing frozen human observation from **Authored entry test.mp4** through the Cases picker, advancing only the new case to revision 3. The attachment is labelled human evidence without claiming an evaluated miss. Saved a second comparison at zero offset with an authored visual-cue note. Each side retained its own analysis and rule configuration.
- Comparison players stacked at 390 pixels; comparison, annotation-case actions and human evidence packages had no horizontal document overflow. Playback, offset and save controls remained reachable. Restored the desktop viewport after inspection.

### Persistence and limits

After a final supervised restart, the browser reloaded the new case at revision 3 with both human sources and its saved comparison at revision 1. The original case remained at revision 7 with its +1-second comparison at revision 1. The previously built human package remained ready with captured case revision 2 and its same archive hash. Live reference validation passed across **51 workbench documents** and **91 media paths**, including frozen annotations, report misses, case attachments, packages and comparisons. The API reported healthy PostgreSQL, bus and blob storage with zero processing errors; all **17 consumer health checks** passed. The final inspected browser warning/error log was empty. A final typing-only comparison cleanup passed all 13 comparison tests and scoped mypy across six changed API files.

This extends backup reference validation; a new offline backup/restore drill was not performed for this batch. Earlier isolated restore drills are recorded below.

These checks use authored footage and local development identities. They do not establish real-camera accuracy, matching cross-camera identities, synchronized capture clocks, frame-accurate playback, physical operation, production load or off-host recovery. Alignment remains a reviewer judgment; sampled overlays retain the underlying model limitations. No model weights, external notifications or physical actions were enabled.

---

## Reusable rules, attached evidence and personal notifications

Added versioned rule presets with explicit target-zone mapping and reviewed bulk application; additional immutable case evidence with a unified source timeline and selected-recording exports; and durable personal in-app notifications for assignments, deadlines, analysis and exports. Storage protection and offline backup validation include every attached and package-captured source. See [RULE_PRESETS.md](RULE_PRESETS.md), [CASE_ATTACHMENTS.md](CASE_ATTACHMENTS.md) and [NOTIFICATIONS.md](NOTIFICATIONS.md).

### Automated checks

- Final full Python unit suite: **1,590 passed, 35 skipped**, five existing dependency/numerical warnings, in **46.39 seconds**. The report is `.sio/review-runtime/unit-collaboration-final.xml`. Optional skipped model/infrastructure cases remain unverified.
- **Six PostgreSQL integration tests passed** in rollback-only transactions, including a connected preset-to-case-attachment flow and notification read state retained by fresh manager instances.
- **62 frontend tests passed**; TypeScript and Vite production build passed (**2,314 modules**, main bundle approximately **139 kB gzipped**). Ruff lint/format passed across **333 files**, the configured mypy gate passed for **45 core/schema files**, and all **17 exported contracts** matched.
- Defensive regressions cover stale and duplicate bulk actions, frozen preset versions, full captured-zone permissions including legacy evidence, attachment archival races, selected-source package context, restart-recovered exports, recipient isolation, notification deduplication and persistent read state. Media package tests use real FFmpeg against authored fixtures.

### Browser and runtime checks

Verified in the local in-app browser at desktop size and **390 × 844**:

- Saved **Authored entry preset**, version 1, from the original configured recording. Mapped its logical Entry area scope to two other recordings' own zones, reviewed the append preview and applied it. Both targets gained one rule; their polygons remained their own, the original source stayed at revision 9, and analysis did not start automatically.
- Explicitly ran analysis on `authored-zone-entry.mp4`; its motion-baseline finding appeared at 3 seconds. Attached that exact event to **Authored loading-bay review** with an authored context note. The case advanced from revision 6 to 7 and retained its original analysis, owner, resolved state, notes and mission. Both evidence sources appear in the timeline. Opening the attachment selected its exact retained analysis at 3 seconds with configuration editing disabled.
- Built and downloaded a **2–4 second** ZIP from the attached recording, with an annotation at 3 seconds. Package `pkg_e57a6e7729c4431188b148333292016d` captured case revision 7, both source snapshots and the selected attachment's analysis. All four payload hashes and the archive hash verified; archive SHA-256 is `1b7636cf213f08d8f696da8af30cc9e3e275fd7006782f85d9f8a11dfeadff86`.
- Created a separately labelled **Authored notification workflow** case. Inbox **Assign to me** set owner `console`; its deadline displayed as 17:00 local time. The native date field's automated fill did not update React form state, so this fixture's deadline was set through the authenticated local API. Browser date typing is not claimed verified.
- The bell displayed assignment, due-within-24-hours, completed-analysis and ready-export notices. Links opened the matching case, retained analysis and exact highlighted export. **Mark visible read** cleared the badge; All retained the four read entries. After the final API restart, the browser and API still showed zero unread and the database retained the same four read notices.
- Preset, attachment and notification controls were reachable at 390 pixels with no horizontal document overflow. The notification popup stayed within the viewport. Restored the normal viewport after checking. The browser warning/error log was empty before the controlled restart.
- After restart, the original API reported healthy PostgreSQL, bus and blob storage, zero processing errors, and all **17 consumer health checks** passed. Existing source, preset and case records remained available.

### Backup and isolated restore

Stopped the application writers, created and verified `.sio/review-backup-20260911-collaboration/` (**76 files**), and restored it into new database **`sio_restore_20260911_collaboration`** with separate data directory **`.sio/review-restore-20260911-collaboration/`**. The CLI verified **36 workbench documents** and **74 media files**, completing at **09:58:57 UTC**. A direct read of the restored database confirmed the preset, both cases, both original-case evidence sources, and all four read notification records. The earlier backup and live database were preserved. The original application was restarted; the restored copy was not started as a deployment.

These are authored workflow fixtures and local recovery checks. They do not establish real-camera accuracy, synchronized recording clocks, physical operation, anonymization, distributed-worker delivery, production load or off-host disaster recovery. Notifications remain in-app. The new runtime managers support one API process; their bounded scans and other limits are documented in their guides.

---

## Evaluation and operations extension

Added an evaluation lab, durable local processing queue with batch upload, operator inbox and permanent shift handovers, camera readiness wizard, protected storage controls, bounded evidence ZIPs, and a coordinated offline database/media backup CLI. The operator guides describe actual permissions and limits.

### Automated checks

- Full Python unit suite: **1,506 passed, 35 skipped**, five dependency/numerical warnings, in 57.50 seconds. Optional skipped model/infrastructure cases remain unverified. The full run includes queue recovery/cancellation, archive-versus-enqueue races, cleanup reference protection, annotation matching and immutable evaluation reports, camera projection checks, package generation, permissions and handover snapshots.
- **Five PostgreSQL integration tests passed** using rollback-only transactions: tenant isolation, revision/history persistence, retained case evidence, and queue/storage state transitions.
- **50 frontend tests passed**, with TypeScript and Vite production build passing (**2,305 modules**, main bundle approximately **132 kB gzipped**). The map and optional 3D bundles remain larger. Browser testing exposed a case-list summary mismatch in the package picker; its regression now uses summary rows without evidence payloads.
- Ruff lint/format passed across **323 files**. The configured mypy gate passed for **45 source files** and all **17 exported contracts** matched.
- A real FFmpeg test generated an evidence ZIP and checked trim duration, aspect ratio, pixelation, audio removal, escaped HTML, retained provenance and SHA-256 hashes. Camera mathematics use authored measurements; these do not establish surveyed camera accuracy.
- After the full-suite snapshot, **47 focused backup tests passed**, including 14 added purge-integration regressions. Completed purge tombstones preserve metadata without requiring deleted frames; partial purges and case/evaluation/package references to purged media fail validation.

### Browser checks

Verified against the local application at desktop size and **390 × 844**:

- Uploaded two authored MP4 fixtures in one browser file selection. Both reported ready independently and the first uploaded clip became selected. The library contains four authored recordings.
- Submitted the configured six-second fixture through the new queue. The saved job completed on attempt 1 and remained available in Finished/All jobs. Cancellation/restart recovery paths are covered by automated tests; this short browser fixture finished too quickly to exercise cancellation manually.
- Saved and froze authored annotation coverage over six seconds with one entry window at 2.5–3.5 seconds. Compared two retained analysis versions against that same frozen set. Each matched the fixture's one event with zero false alarms/misses; the report explicitly labels authored footage and does **not** establish real-site accuracy.
- Saved a permanent shift handover referencing the resolved authored case at revision 6, with authenticated author `console`. Case assignment, priority and due-date edge cases are covered by API tests; this existing resolved case was not reopened for the drill.
- Built and downloaded a **2–4 second** evidence package with one annotation at 3 seconds. The package preserved the case's original analysis and revision 6, and the browser showed a ready ZIP with four hashed payload files plus its manifest.
- Storage displayed protection from the case, evaluation and evidence package. Archive preview marked that recording ineligible. A newly uploaded unreferenced fixture was archived, inspected in permanent-cleanup preview, and restored to revision 3. The destructive confirmation stayed empty/disabled; no runtime footage was purged. Purge behavior is tested only on temporary test-owned files.
- Camera setup correctly displayed **No configured camera is available** with disabled validation. No RTSP source, credentials, measured survey or physical camera was supplied or invented.
- Evidence, inbox and storage had no horizontal document overflow at 390 pixels. Wide tables scroll inside their containers; mobile tab labels wrap instead of being truncated. The inspected browser warning/error log was empty after these checks. The viewport was restored to its normal size.

### Real backup/restore drill

Stopped every supervisor-managed application writer while leaving PostgreSQL running, then created and verified `.sio/review-backup-20260911-workbench/` (**58 files**, about **12 MB** on disk). Restored it into new database **`sio_restore_20260911_workbench`** and separate data directory **`.sio/review-restore-20260911-workbench/`**. The CLI verified **18 workbench documents** and **56 media files** and wrote `RESTORE_COMPLETE.json` at 07:46:11 UTC on September 11. The restored media includes the retained analyses, floorplan and evidence package. The original database and data directory were not replaced; the original application was restarted afterwards. Its API reported healthy PostgreSQL, bus and blob storage with zero current errors; all 17 consumer health checks passed. The browser reloaded the completed job from its persisted history.

This verifies a local offline recovery drill. It does not verify off-host recovery, encrypted storage/key management, scheduled backups, starting a full deployment from the restored copy, distributed worker coordination, real cameras, production load or physical actions. Queue and media mutation locks support one API process. Automatic retention and external notifications remain off.

---

## Recorded review and casework milestone

Implemented the Footage & cases workspace, versioned recorded analysis, immutable case evidence, assignment and resolution, permanent notes, linked draft missions, HTML/JSON reports, saved searches, reviewed-case metrics, and a floorplan/site editor. See [REVIEW_WORKFLOW.md](REVIEW_WORKFLOW.md) for the operator workflow and [VIDEO_REVIEW.md](VIDEO_REVIEW.md) for processing limits.

### Automated checks

- Full Python unit-suite snapshot: **1,370 passed, 35 skipped**, with five numerical/dependency warnings, in 146.77 seconds. Skipped optional model/infrastructure cases are not claimed verified. Loopback-listener and child-process tests ran with the required local permissions.
- Subsequent focused validation: **26 case/evidence tests**, **15 video/event tests**, and **31 site/store/projection tests** passed. These include resolution-note persistence, retained original analysis, exact event lookup, real FFmpeg conversion and motion analysis, polygon validation, and revision conflicts.
- **Three PostgreSQL integration tests passed** against the isolated local database. They exercise tenant boundaries, optimistic concurrency, append-only history, and case evidence after a later analysis. Test transactions roll back.
- **40 frontend tests passed**, including raw MP4/floorplan requests through the shared API/SDK transport and six stream watchdog/freshness regressions. TypeScript and the production Vite build passed (2,293 modules). The main application bundle is about 113 kB gzipped; the optional mapping and 3D bundles remain substantially larger.
- Ruff lint and format checks passed across **303 files**. The configured mypy gate passed for **45 core/schema source files**; all **17 exported contracts** matched their definitions.

### Browser and runtime checks

Verified at desktop size and **390 × 844** in the local in-app browser:

- An authored six-second moving-rectangle recording was accepted by the live API and served as a decodable H.264 derivative with full-frame pixelation and no audio. A second copy was successfully uploaded through the browser file picker and appeared ready for playback. This is a workflow fixture, not real-camera accuracy evidence.
- Defined a four-point image zone and entry rule in the UI, saved the configuration, and ran analysis. The motion baseline produced one finding at **3 seconds**. Selecting the finding sought the video to exactly 3 seconds.
- Created a case from that finding; edited its title, owner, summary, outcome, and resolution; added a permanent note; created and linked a **draft** response mission. The mission was not activated.
- The due date was saved through the live API and displayed correctly in the browser in local time. Automated native date-field filling was inconclusive; no manual keyboard entry is claimed verified.
- Exported the saved case from the UI, and retrieved both HTML and JSON reports from the live API. Reports include the retained evidence and authenticated activity history.
- Saved and reopened a personal case search. The quality panel displayed the one reviewed authored case as **1 of 1**, with sample counts and the absence of recall/ground-truth measurements clearly stated.
- Created an authored site, uploaded a labelled test floorplan through the live API, then placed a camera, drew a zone, and saved planning notes in the browser. The site remained at saved revision 3 after the API restart. A subsequent browser-picker floorplan upload saved revision 4 and preserved its zone, marker, and notes. Camera pose remained an uncommissioned draft.
- After a supervised API restart, the case retained its owner, due date, resolution reason, notes, mission, and revision. Opening its footage selected the original analysis ID, disabled historical configuration editing, and loaded protected playback at **3 seconds**. A subsequent browser-triggered analysis rerun advanced the recording to revision 6; reopening the existing case still selected its original analysis and 3-second position.
- Fixed narrow-screen workspace clipping. Footage, case, search, and site controls remained accessible by scrolling; the review workspace had no horizontal document overflow at 390 pixels.
- The restarted API health check reported bus, PostgreSQL, and blob storage healthy, with zero current API processing errors. Earlier shutdown cancellation traces remain in the log.
- Browser QA found and fixed mixed-case duplicate Content-Type headers that had caused binary uploads to carry the JSON default. The shared request helper now preserves the caller's media type. Browser metadata probing also defers unreadable original codecs to mandatory server validation. The upload/floorplan checks completed with an empty browser error/warning log.
- Restart testing also exposed a half-open stream remaining marked live. A 45-second inactivity watchdog now covers pending requests and raw SSE bytes, including heartbeat comments; abort closes the reader and triggers reconnection. Map freshness uses the newest snapshot or push timestamp. A controlled API pause visibly changed the browser badge to **reconnecting** after silence. After automatic API resume, the same page returned to **live** with a current activity timestamp and freshly updated map, without a page reload or process restart.

### Runtime and remaining limits

The current local run uses PostgreSQL 17 on **55432**, Redis on **56379**, API on **8000**, and the console on **5173**. State, fixtures, media, database files, and logs live in **`.sio/review-runtime/`** under this checkout. The saved `env.sh` and [workflow guide](REVIEW_WORKFLOW.md#local-verification-installation) describe restarting this existing installation. The historical temporary-directory runtime below is no longer the active installation.

FFmpeg/ffprobe 9.0.1 were used after repairing the local broken FFmpeg installation. The review pipeline processed real fixture pixels using the explicitly labelled motion baseline; optional object-recognition weights were not installed or validated. Full-frame pixelation is not a guarantee of anonymization, and private originals remain on local storage.

At this earlier milestone, backup/restore recovery was not verified; the extension above records the subsequent local drill. Neither milestone establishes real-camera accuracy, surveyed geolocation, physical device operation, live OIDC-provider compatibility, sustained load, distributed processing, automated retention, or Temporal durability. The local video worker supports one API process. Site layouts and optional pose drafts do not commission live sources. The broader demo continues to use simulated yard observations, a scripted copilot, hash embeddings, and dry-run actions.

---

# Historical local verification — 9 September 2026

This records a local development run, not production certification.

## Automated checks

- Complete Python unit suite: **1,276 passed, 35 skipped** in 96 seconds. Optional model/infrastructure cases account for skips. Supervisor tests were allowed to create loopback listeners and inspect their own child processes.
- Ruff lint and formatting passed across the repository.
- Configured mypy gate passed for 45 core/schema source files.
- Exported JSON schemas matched all 17 contracts.
- Frontend TypeScript and production Vite build passed. The main application bundle is about 96 kB gzipped; the mapping bundle and optional lazy 3D twin remain substantially larger.
- All **20 browser/SDK regressions passed**, covering explicit login, OIDC PKCE and refusal handling, renewal/logout races, shared SSE parsing, reconnection, snapshot races, stale entities and cancelled replay requests.
- Subsequent fixes passed 41 mission/provenance tests, 100 decision/prediction tests and four HTTP responsiveness regressions. The acceptance scenario was rerun successfully after these runtime changes.
- The PostgreSQL acceptance scenario passed through sensor observation, measurement persistence, rule/event evidence, alert creation, authenticated decision approval, duplicate approval rejection and historical world/event reads. It uses authored sensor data, memory bus/graph transports and a rollback-only PostgreSQL transaction.

The initial unit run identified invalid AutoETS intervals and outdated structural tests; those were corrected before the complete passing run. Invalid forecast intervals now use an explicitly labelled finite fallback. Numerical/dependency deprecation warnings are still reported by upstream libraries.

## Browser checks

Verified in the local in-app browser at desktop size and 390 × 844:

- Explicit development sign-in and sign-out, role-dependent source/approval controls.
- Live map/entity updates and independent snapshot recovery after the application restart.
- Alert to incident navigation, linked event sequence and decision records.
- Approval of an option changes its displayed state to approved.
- Drafting an incident mission opens that mission in Respond with draft state. Subsequent provenance fixes bind human mission actions to the authenticated actor; historical append-only entries are preserved.
- Incident replay displays a historical map; returning to LIVE restores current state.
- Simulator connection test returns a sample without publishing it.
- Source configuration saves and reports that an ingestion restart is required.
- System health displays live service checks, counters and actual adapter/mode labels.
- Mobile timeline controls and scrubber fit the viewport; map status clears the scale control.
- No browser warnings or errors in the final browser console inspection.

After the final backend restart, a new mission entry displayed `console` as its authenticated
author. Operator navigation opened System health directly, omitting source administration it
cannot access. All 18 aggregated services responded, with zero processing errors at that check;
some consumers were still processing queued observations.

## Runtime and remaining limits

The preview uses a separate PostgreSQL 17 database on port 55432 and Redis on 56379. Existing PostgreSQL 16 and Redis services were not replaced. Preview state and logs live under `/private/tmp/arc-review-runtime`; they are disposable verification data. The console runs on port 5173 with API port 8000.

The lightweight profile starts all 17 consumer services with their own HTTP endpoints and aggregate health on 8120. A transient burst of Redis read timeouts was observed during the first loaded run. Consumers recovered; health subsequently returned quickly with no pending backlog. Forecast/solver/backtest CPU work was moved off the shared event loop. Historical error counters remain visible until process restart; this run does not establish sustained-load capacity.

The yard is simulated, with a separately labelled real Open-Meteo feed. No real camera was connected and no recorded camera evidence is claimed verified. Missing optional vision weights and sprite assets are reported at startup. The default copilot is scripted, embeddings are hashes, actions are dry-run, and no physical gate/drone actuator is installed.

The Keycloak client fixture, idempotent setup and mocked browser protocol are tested. No live Keycloak/provider login, hardware, deployment-scale workload, Linux provisioning, GPU adapter, Temporal durability or physical effect was verified.

See [operations](OPERATIONS.md) for setup and workflow behavior and [README](../README.md) for current scope.
