# Recorded-footage evaluation lab

The Evaluation tab compares retained completed analysis runs with independently authored incident labels. It evaluates emitted entry/dwell events for selected zone scopes and reviewed times. It does not estimate object-class accuracy, identity accuracy, or performance on footage outside the selected dataset.

## Operator workflow

1. Upload footage, configure zones/rules and finish at least one analysis in **Footage**. Run another version if comparing configurations or model outputs.
2. In **Evaluation**, choose a recording. Declare whether it is real footage, an authored fixture or unknown, and choose development, validation or held-out partition. A clip's partition becomes fixed on its first annotation save.
3. Review the protected pixelated playback independently. Select the zone/event pairs being evaluated. Mark the intervals completely reviewed for *all* selected scopes. Label every observed incident window, including incidents with no corresponding detector alert. An incident window is one expected event, not a label per frame.
4. Confirm the review declaration and save. Empty incident labels within reviewed coverage mean that absence was verified. Unreviewed times remain unknown.
5. Freeze the annotation version. Later edits affect the draft only. Choose one or two completed analysis versions and add the clip to the comparison. Repeat for additional clips in the same partition, using the same candidate positions for corresponding configurations.
6. Run and save evaluation. The report retains annotation snapshots, source analysis IDs, model/rule/zone configuration, configuration and annotation hashes, matched events, unmatched alarms and missed labels. **Inspect** opens the exact retained run at the event/incident time.

## Measurement contract

Predictions are eligible only when their emitted timestamp is inside a reviewed coverage interval and their `(zone_id, event_type)` is in the reviewed scopes. Events outside this domain are excluded and counted separately. Reviewed duration is the union of intervals, so overlaps do not inflate the denominator.

A prediction can match one annotated incident with the same scope if its timestamp is inside the incident window plus the saved tolerance. Matching is deterministic maximum-cardinality bipartite matching; stable closest-first edge order resolves available choices. Duplicate predictions are not deduplicated: only one can match a single incident, and the others remain false alarms.

- Precision = matched predictions / all eligible predictions; null when there are no eligible predictions.
- Recall = matched incidents / annotated incidents in reviewed coverage; null when there are no annotated incidents.
- False alarms per reviewed hour = unmatched eligible predictions / reviewed hours. Multiple reviewed event scopes share the clip-time denominator; this is not a scope-hour rate.
- Median signed delay = emitted timestamp minus the annotated window start across matched incidents. A negative delay means the emission preceded the annotated start within tolerance. Null when there are no matches.

Rollups sum counts and unioned reviewed duration across distinct clips before calculating rates, rather than averaging clip percentages. A report cannot repeat a clip or pool different dataset partitions. Candidate positions, tolerance and labels are fixed in the saved report; candidate positions across clips are chosen by the reviewer, not inferred as a global model version.

## Persistence and permissions

Drafts use optimistic revisions and reject stale saves/freezes. Frozen annotations and reports have no mutation endpoints. Tenant lookups and zone permissions apply to all inputs and output records. Creation uses the shared per-video mutation locks so archive cannot interleave with creating evidence references in the supported single-API-process deployment. Archived/purged recordings cannot acquire new evaluation references.

Storage reference kinds:

- `evaluation_draft`: `record_id = video_id`, top-level `video_id`.
- `evaluation_annotations`: immutable `ann_...`, top-level `video_id` and `annotation_set_id`.
- `evaluation_report`: immutable `eval_...`, top-level `video_ids`, `analysis_ids`, and `inputs[{video_id, annotation_set_id, analysis_ids}]`.
- `evaluation_partition`: per-upload partition declaration; metadata alone does not protect media.

The lab scans up to 5,000 retained records per document kind. The API supports up to 20 clips and four candidate positions per report; the current UI exposes two candidate positions. Clips must already satisfy the video workbench's bounded upload/analysis limits.

## What remains unverified

The UI requires a human declaration of complete review, and footage origin is reviewer supplied. Neither is independently certified. Authored fixtures prove software behavior, not real-site accuracy. A sticky partition prevents changing the same uploaded record from development to holdout, but does not recognize re-uploads of the same footage. Repeated access to labels/results can contaminate a holdout; blind annotation workflows and independently controlled datasets are not implemented.
