# Evidence packages

**Footage & cases → Evidence packages** builds a downloadable ZIP from a case opened on a persisted recording event. The server resolves the case's exact tenant-scoped recording and analysis; callers cannot supply media paths, external URLs or replacement evidence.

## Build and download

1. Sign in as an **operator, commander or administrator** and select a recorded case. Cases created only from platform alerts can use the existing case HTML/JSON export instead.
2. Review the protected recording. Set the clip's start/end times, or use the playhead controls, and preview the interval. Choose **0.1–60 seconds**, entirely inside the recording.
3. Optionally add up to **30 factual annotations**, each at a time inside the clip and no longer than **2,000 characters**. Times refer to seconds in the original recording. Notes are attributed to the signed-in author and are not independently verified findings.
4. Select **Build evidence ZIP**. The panel follows queued/building status. Download when ready. Failed or interrupted builds show an explanation; create a new package to retry.

The ZIP contains:

| File | Contents |
| --- | --- |
| `clip.mp4` | Trimmed, additionally pixelated video; audio, subtitles and source metadata removed |
| `case.html` | Escaped, self-contained printable case report with no external assets |
| `case.json` | Case snapshot at package creation, including retained event/model/rule provenance |
| `annotations.json` | Authored notes, original timestamps and offsets within the exported clip |
| `manifest.json` | Case revision, recording/analysis identifiers, clip interval, file sizes and SHA-256 hashes |

The console also records the final ZIP's hash. These hashes identify exact package bytes; they are not digital signatures, authenticity certification or a complete chain of custody. Later case edits do not rewrite an already built package.

## Privacy, access and processing limits

Only the protected playback rendition is used. The private original is never included or exposed through this flow. Pixelation is **not a guarantee of anonymity**: clothing, movement, scene context, text and authored notes may still identify people or places. Review the complete export before sharing it. The builder does not send it to anyone.

Creation requires `case.write`, `review.read`, `media.read` and access to the case's zones. Reads/downloads remain tenant-scoped and check current case access; downloads also require permissions and zones for any included mission/decision context. An archived recording must be restored before building a new package. The exact completed analysis and event must remain available.

At most **three packages** may be waiting/building per API process. Encoding shares the local media processor with uploads and analysis and runs outside the request loop. The clip encoder has a 90-second timeout and a 50 MiB output cap; payload files are capped at 100 MiB. Local FFmpeg and ffprobe are required. Interrupted builds are not silently reported as ready. Persisted case/package references protect their recording against storage cleanup. Back up database records and media together; see [REVIEW_BACKUPS.md](REVIEW_BACKUPS.md).

## Verification

Run `.venv/bin/pytest tests/unit/test_evidence_packages.py` from the repository root. The focused implementation run passed **15 tests**, including a real FFmpeg fixture that verifies trim duration, aspect ratio, coarse pixelation, removed audio, escaped HTML, exact case provenance and matching hashes. Other tests cover bounds, forged evidence/authorship, tenant/zone permissions, concurrent admission, interrupted jobs and partial-output cleanup. Frontend interval and case-summary selection regressions are in `web/tests/commissioning.test.mjs` (`npm --prefix web test`). These authored fixtures do not establish real-world anonymity or evidentiary authenticity.
