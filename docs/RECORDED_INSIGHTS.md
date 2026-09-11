# Objects and movement in recorded footage

**Footage & cases → Objects** searches saved detection samples across accessible recordings. **Footage → Movement** calculates sampled occupancy and directional crossings from a completed retained analysis. Both use existing detections; no extra model or video reprocessing is needed.

## Find an object

Choose a recording, analysis version, detected class, saved image zone, confidence threshold or clip-time window, then select **Search objects**. **Clear filters** restores the full accessible scope. By default, each recording contributes its newest completed accessible analysis, even when a more recent attempt failed or is still processing. Selecting a version pins the query to that exact run.

Each result is a local track and class within one analysis. It shows first and last matching samples, matched sample count, observed confidence range and saved zone memberships. The protected thumbnail highlights the selected object's box. Choose a saved sample, **First**, or **Last**, then **Open footage** to open that exact analysis and time. Times are offsets within individual clips, not synchronized camera timestamps. Confidence filters exclude observations without a measured confidence, including motion regions.

Catalog reads transfer metadata instead of every frame. They consider at most 500 newest-created analyses and 20 accessible recordings. A query processes at most 50,000 retained object observations, with up to 200 track results per page and offset at most 50,000. The console discloses bounded scans; their counts are not exhaustive. Choose one recording and a retained version to reduce the search scope.

## Count crossings and sampled occupancy

Open a completed recording and scroll to **Count what passed through**. The panel always names its exact source analysis. Select an object class, saved zone and optional minimum confidence. With no counting line, **Calculate preview** returns occupancy only.

To count crossings, select **Draw counting line** and click two endpoints on the protected frame. The coordinate editor is an alternative. A circle marks the start and a square the end: side A is visually left of the directed start-to-end line, and B is right. **Reverse A / B** reverses that direction. Coordinates range from 0 to 1, and endpoints must be at least 0.02 normalized units apart.

The calculation uses each box's bottom-center point. A crossing requires a local track to move between stable sides through the finite segment. First appearances, paths around the endpoints, gaps greater than 1.1 seconds, missing frame indices, missing tracks, or observations rejected by filters do not establish a crossing. A 0.005 normalized-distance band suppresses small movements near the line. Return crossings may count again; these are passage observations rather than unique-person counts. The displayed interval brackets the supporting samples; the exact crossing instant is not measured.

Occupancy is the number of qualifying objects at each saved sample. The chart uses separate sample marks, with peak occupancy, an arithmetic mean across samples and links to the busiest sampled moments. Missing or malformed samples are unobserved; a valid sample with no matching objects counts as zero. Counts describe detector observations rather than ground truth.

## Save and export

Name the calculation and select **Save snapshot** after calculating the current preview. Each immutable report preserves its line, filters, summary, model/hash, recording, exact analysis, retained zone/rule scope, author and creation time. Repeating the same save reuses the same report. Changing configuration produces a new snapshot; editing the recording or running another analysis does not rewrite an existing report.

Select a saved snapshot to reopen it. **Download saved CSV** exports that snapshot's summary, every occupancy sample and every crossing interval, with its configuration and provenance. Unsaved changes disable the saved export and protect navigation until saved or discarded. CSV text fields are escaped against spreadsheet formula interpretation. These reports do not create incidents or send alerts.

Creating previews or reports requires `review.write`, plus read/media access to the recording and every retained source zone. Reading or exporting reports rechecks both current source access and captured scope. The limits are 20 reports per analysis, 100 per recording and 1,000 per tenant. There is no report edit/delete route in this version. Reports protect their source recording from archive/purge, and coordinated backup validation checks their exact source references. This remains a single-API-process workflow.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/review/objects/catalog` | Accessible recorded-analysis metadata, classes and saved zones |
| `GET /api/review/objects` | Track search with `video_id`, `analysis_id`, `class_name`, `zone_id`, `min_confidence`, `start_s`, `end_s`, `limit`, `offset` |
| `GET /api/review/videos/{video_id}/movement?analysis_id=...` | Saved reports for one exact analysis |
| `POST /api/review/videos/{video_id}/movement/preview` | Calculate `{analysis_id, configuration}` without saving |
| `POST /api/review/videos/{video_id}/movement` | Save `{analysis_id, configuration, title}` |
| `GET /api/review/movement/{report_id}/csv` | Private CSV from a saved report |

Configuration is `{line: null | {name, start: [x,y], end: [x,y]}, class_name: "", zone_id: "", min_confidence: null}`; empty class and zone select all classes and the whole frame. Request validation rejects unknown fields, non-finite values, invalid lines, unrelated analyses and zones absent from the retained run.
