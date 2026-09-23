# Investigation links, resume and camera timeline

## Exact investigation locations

The console stores its selected view and record identifiers in the address bar.
**Copy view link** copies that URL. A footage location includes the recording ID,
retained analysis ID when available, and playback offset in seconds; case links
select a case. Comparison and evidence views can carry a case ID, and evidence links
can carry a package ID. Incident, mission and site/camera views support their record
identifiers as well.

For example, a recording link has this shape:

```text
?view=footage&video=vid_<id>&analysis=ana_<id>&at=12.5
```

Use the application's copy control for actual identifiers. The URL contains no
access token, media bytes, search query, note or unsaved draft. Recipients must use an
application origin they can reach and sign in with permissions for the referenced
records. A URL does not grant access. APIs and protected-media requests authorize
the current tenant, role and source zones; missing or forbidden sources are not
replaced with unrelated evidence.

An explicit valid view URL takes precedence over resume state. With no explicit
view, the console restores the last location stored in this browser for the
authenticated **tenant and subject**. Resume uses local storage and is best-effort
when browser storage is disabled; it is not cross-device synchronization or a saved
investigation document. Shared-browser storage still contains record identifiers
and should be treated as operator context.

Changes of view or record create browser-history entries. Playback-position changes
replace the current entry, so Back does not traverse every video tick. The player
reports offsets to a tenth of a second. Reload reopens the current retained analysis
and offset; it does not automatically play the recording. Exact-source selection is
separate from frame-accurate decoding: browser playback and approximate frame steps
retain the limitations described in [FOOTAGE_NAVIGATION.md](FOOTAGE_NAVIGATION.md).

Unsaved case edits/notes/attachments, footage drafts, comparison alignments and
capture-clock changes block protected workspace/history navigation until saved or
discarded. Relevant editors also request the browser's unload warning. The URL and
resume state preserve a location only; they do not recover unsaved form content.

## Camera timeline

Open **Footage & cases → Camera timeline** to line up retained recordings by declared
capture clocks. The view shows at most 20 recordings and scans up to 500 retained
analysis metadata records; a bounded-inventory notice appears when those limits may
omit older records.

For a recording with unknown capture time, select **Set capture clock**. Enter:

- **Camera ID:** an operator-declared label used for grouping/filtering, not an
  automatically verified identity or a live-source activation command.
- **Capture start:** a timestamp with an explicit timezone, such as
  `2026-09-23T14:05:00+05:30`. The supported year range is 1970–2100.
- **Clock correction:** signed seconds, from −86,400 to +86,400. Positive values move
  footage later on the shared clock; negative values move it earlier.
- **Uncertainty:** optional nonnegative seconds, up to 86,400. Blank means unknown;
  zero is an explicit operator claim of zero uncertainty.
- **Clock source / measurement note:** explain where the time and correction came
  from. A known capture time requires both the camera ID and this note.

The calculation is:

```text
corrected start = declared capture start + signed correction
clip offset = selected UTC moment − corrected start
```

For example, a camera displaying `10:00:00Z` while the measured real time is
`10:00:05Z` needs a `+5` second correction. The shared view displays UTC regardless of
the input timezone. It shows uncertainty alongside the declaration; uncertainty
does not widen the selectable interval or perform automatic alignment.

Move the shared cursor or enter a UTC moment. **Open at cursor** is available for
recordings whose corrected interval contains that instant, including the start and
excluding the end. It opens the matching clip offset and the latest accessible
completed analysis selected by the timeline response. If no completed analysis is
available, the recording can still be opened for playback. The view is a shared
time navigator, not synchronized multi-player playback or automatic cross-camera
tracking; use [Compare footage](EVIDENCE_COMPARISON.md) for paired playback.

Leave capture time blank when unknown. Such recordings stay in **Capture clock
unknown**, can be opened normally, and are never positioned using upload time,
filename guesses or a fabricated timezone. A capture time is required before adding
correction or uncertainty.

Clock writes use the current clock revision, require `review.write`, retain the
authenticated declarer, and verify access to the source's zones. Stale saves fail
instead of overwriting a colleague's clock. Saved clocks are separate documents;
they do not rewrite recordings, retained analyses, existing case evidence snapshots
or saved manual comparison offsets. The declarations are not independent proof of
clock accuracy, incident ordering or camera synchronization.

## Related workflows and verification boundary

[Recorded visual search](RECORDED_SEARCH.md), object search, bookmarks and case
evidence can open the same exact footage locations. [Source activation](SOURCE_ACTIVATION.md)
changes live connector configuration and is separate from declaring a recording
clock. [Alert delivery](alert-deliveries.md) concerns external notification receipt,
not evidence timestamps.

Navigation encoding/resume, clock validation and UTC-to-clip calculations have
focused tests in `web/tests/investigation-navigation.test.mjs` and
`tests/unit/test_recording_timeline.py`. Browser verification should also exercise
Back/Forward, dirty-editor blocking, refresh and copied links against the combined
application before reporting results. Authored timestamps, mock authentication and
synthetic footage do not establish hardware clock accuracy, live identity-provider
operation or real-site reconstruction accuracy.
