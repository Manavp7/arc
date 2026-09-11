# Additional case evidence

Cases can contain up to **20 immutable source snapshots**, including the original. Attach another recording event or platform alert when it helps explain the same investigation. Original case identifiers and source evidence remain unchanged.

1. Open the case and save or discard current edits.
2. Under **Evidence timeline**, select Recorded event or Platform alert and search by title or identifier. Searches return visible persisted sources; recorded events require a completed retained analysis and available recording.
3. Select the exact result, review its analysis identifier and recording time, optionally explain its relevance, and choose **Attach immutable evidence**.
4. Review the combined timeline. **Open exact footage** opens the analysis version that produced that event, even after later reruns. Protected platform frames appear when exact historical references resolve; missing references remain explicitly unavailable.

Every attachment preserves its authenticated author, attachment time, note, source identifiers and available event/model/rule provenance. Duplicate references return the existing attachment without replacing its note or increasing the case revision. Other stale edits return a conflict; discard the attachment draft and reload the case before trying again. There is no attachment replacement or removal action.

Platform sources use their recorded timestamps. A video's relative seconds do not establish a shared clock across cameras. When the recording's absolute clock is unknown, the timeline orders that source by attachment time and labels the limitation.

Attachment writes require `case.write` and source access. Reading the complete case requires access to **all** evidence zones, including other zones named in the retained analysis's complete rule/layout snapshot, plus applicable review/media permissions. Attaching a restricted source can therefore make the entire case unavailable to an operator with narrower zone access. Snapshots are copied from server records; the API does not accept arbitrary evidence JSON, URLs or file paths.

HTML/JSON reports include all source snapshots and attachment notes. **Prepare evidence package** lets an operator choose which recorded source supplies the trimmed MP4, including a recording attached to an alert-origin case. The ZIP still captures the whole case revision and all evidence metadata. A package records its selected attachment/event/analysis and captured access zones. It never includes a private original; pixelation does not guarantee anonymity. See [EVIDENCE_PACKAGES.md](EVIDENCE_PACKAGES.md).

Storage and backup checks retain all directly or indirectly referenced recordings/analyses, including attachments and frozen package references. No source is automatically merged, identified across cameras or activated by attaching evidence.

Package startup recovery marks previously queued/building exports interrupted and stamps their actual recovery time, allowing an in-app completion notice without opening the evidence page. It scans at most 5,000 recent package records per known tenant; reaching this limit is logged and disclosed in package response recovery metadata. Current in-process work is preserved.

Verification commands:

```bash
.venv/bin/pytest tests/unit/test_case_attachments.py tests/unit/test_evidence_packages.py
npm --prefix web test
npm --prefix web run check
```

Authored fixtures cover tenant/zone access, exact retained versions, immutable snapshots, duplicates, stale revisions, archival races, mixed alert/video cases, summary counts, escaped exports and selected-recording packages. They do not establish real-world incident identity or evidentiary authenticity.
