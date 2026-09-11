# Case footage comparison

The Compare workspace places two recording evidence attachments from one case beside each other. Each source uses the attachment's retained video ID and completed analysis ID. The original case evidence is selectable, and human annotations with a pinned video and analysis are supported. The two attachment IDs must be distinct; they may reference the same recording at different moments.

## Review workflow

1. Open Compare from a case or choose a case in the comparison workspace.
2. Choose the left and right evidence attachments. New comparisons begin with empty source selectors. Each source opens paused at its evidence moment.
3. Turn off linked playback to find a matching visual cue in each source. Use each player’s position slider, half-second steps, and Evidence moment button. Pause both, then choose **Align these paused positions**.
4. Check the offset and review both views with linked playback. The convention is `right time = left time + offset`: +5 seconds pairs left 10 seconds with right 15 seconds. Negative offsets are supported. Swapping sides reverses the sign and preserves each attachment’s current position.
5. Write an alignment note describing the cue and uncertainty, then save. Saving requires `case.write`. Viewers can compare locally and discard their unsaved changes.

The shared timeline is the intersection of the recordings’ time ranges after applying the offset. Linked seeking stays inside that intersection; playback stops at its end. An offset with no overlap can be saved, but linked playback is disabled. Switch to independent mode to inspect either source. The current offset limit is ±180 seconds and the note limit is 4,000 characters.

## Playback and overlays

- Video is fetched through the existing protected-media endpoint. The panel derives media URLs from the authorized recording response. Existing pixelation and audio-removal rules apply.
- Overlays come from each attachment's exact retained analysis, never its recording's latest run. Missing or incomplete retained analyses produce an error instead of substituting a different run. Saved analysis zones and sampled detections can be toggled separately.
- Playback starts only after an explicit Play action. Source changes, alignment edits, hidden tabs, buffering, the end of footage and leaving the workspace pause playback. A rejected play request pauses the attempted group and offers a retry. Required alignment seeks settle before grouped playback begins; assigning an already aligned position does not initiate another seek.
- Linked mode uses the left recording as its playback clock and corrects visible drift on the right. This is a review aid, not frame-accurate or hardware-synchronized playback. Independent seeking and buffering affect that player alone.
- The players stack on narrow screens. All seeking, playback and source-selection controls remain available without native video controls obscuring the overlays.

Recording offsets are manual reviewer judgments. Capture clocks are unknown; an offset does not establish real-world simultaneity. Local motion tracks do not prove that two camera views show the same person. Detection overlays are sampled and carry the limitations of the selected analysis model.

## Saved state and conflicts

`GET /api/cases/{case_id}/comparison` returns the case's current evidence choices and any saved alignment. An unsaved comparison has `revision: 0`, null source selections, zero offset and an empty note. Saved responses can include `saved_case_revision` to distinguish the case revision at the last save from its current revision.

`PUT /api/cases/{case_id}/comparison` sends `revision`, `case_revision`, `left_attachment_id`, `right_attachment_id`, `offset_s` and `note`. A save uses the exact comparison and case revisions that were loaded. A conflict preserves the visible draft and requires an explicit reload of the saved alignment before another save. The server checks case access and edit permissions, source attachment membership, and retained video/analysis references.

Unsaved drafts block local case changes and the Open case/Open exact footage actions. `EvidenceComparePanel` also exposes `onDirtyChange` for the parent workspace to guard tab and workspace navigation, and installs a browser `beforeunload` warning while dirty. Discard changes restores the saved source choices, offset and note. Draft playhead positions, overlay toggles, speed and linked mode are local session controls and are not saved alignment fields.

## Frontend verification

Run from `web/`:

```sh
npm run typecheck
node --import ./tests/register.mjs --test tests/evidence-comparison.test.mjs
```

The focused tests cover positive and negative offset bounds, overlap edges, clamped seeking from either clock, swapping, human annotation sources, explicit empty selections, exact revision-bearing requests, retained-analysis loading, grouped playback rejection, stale playback attempts, redundant-seek prevention, and cancellable readiness of both sought positions. These tests do not establish browser decoder behavior or frame-accurate synchronization; interactive browser review remains necessary.
