# Cases from frozen reviewer annotations

An operator can open a case for a reviewed incident even when the selected analysis produced no detector event. Save and freeze the recording's annotation draft, select a retained completed analysis, and open the frozen annotation as a case. An evaluation report's miss can use the same flow with its exact report reference.

The case remains `unreviewed` until someone assigns a verdict. A reviewer label is an authored observation; opening or resolving the case does not alter frozen annotations or evaluation results and does not establish independent detector accuracy.

## API and evidence

`POST /api/cases` accepts `video_id`, `analysis_id`, `annotation_set_id`, and `annotation_id`, plus optional `evaluation_report_id` and the usual case fields. Omit `event_id` and `alert_id`. `POST /api/cases/{case_id}/evidence` accepts the same source IDs, `expected_revision`, and an optional authored `note`.

The backend requires an available retained recording, a completed analysis for that recording, and a genuine frozen annotation set whose content matches its saved hash. It validates the exact annotation and interval. A report reference is accepted only when that exact recording, analysis, frozen set and annotation appear together in the report's misses. Missing references return 404; inconsistent sources return 422; unavailable media or incomplete analysis return 409.

The immutable snapshot has `evidence.source = reviewer_annotation`. It contains the label, the complete frozen annotation set (reviewer, freeze time, hash, scopes and coverage), the selected analysis's model/rules/zones, recording metadata, and optional frozen report miss context. Its `at_s` is the annotation's start. No detector event, confidence or absolute recording clock is manufactured. Timeline ordering uses attachment time when the recording's absolute clock is unknown.

Source identity is recording + analysis + frozen set + annotation. A repeated request reuses the existing case or attachment even if the report reference differs. The original snapshot stays unchanged: adding a report ID later does not retrofit report provenance. The 20-source case limit includes the original source.

## Access and exports

Case creation/attachment requires `case.write`; reading the full snapshot requires `case.read`, `review.read`, and `media.read` across every frozen evidence zone, including annotation scopes and analysis rules outside the selected annotation. Referencing a report also requires visibility into its zones. Tenant scoping and the shared recording mutation lock prevent foreign references and creation while archival is taking effect.

Reviewer recordings count as recording evidence in case summaries and can be selected for protected ZIP clips. Packages preserve annotation, set and optional report IDs in their manifest and all case reference records, and pin the entire case revision. Clips retain the existing 0.1–60 second bounds, pixelation and audio removal. HTML escapes authored text; JSON preserves provenance. Pixelation is not a guarantee of anonymity.

## Metrics and verification

`counts` and `reviewed_count` include all visible cases. `reviewed_precision` includes only detector-originated cases, using `detector_counts` and `detector_reviewed_count`. `reviewer_annotation_counts` and `reviewer_annotation_reviewed_count` report manual-origin investigations separately. Attaching detector evidence to a manual-origin case does not move it into detector precision. Case totals do not add false negatives to an evaluation or supply recall.

Run `pytest tests/unit/test_case_annotations.py` for frozen-source, report matching, tenant/zone, revision/deduplication, archive-lock, metric and actual ZIP regressions. Fixtures are authored locally; these tests make no real-site performance claim.
