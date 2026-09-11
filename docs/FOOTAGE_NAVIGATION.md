# Faster recorded-footage navigation

The footage workbench provides transport controls and a strip of saved analysis samples beside its existing player. These controls review the protected playback derivative. They do not expose or extract frames from the private original recording.

## Controls

| Action | Keyboard | Behavior |
| --- | --- | --- |
| Play or pause | Space | Toggles the current player; never starts automatically on load. |
| Pause | K | Pauses without changing the selected time. |
| Back or forward | J / L | Seeks five seconds, clamped to the recording’s bounds. |
| Previous or next playback frame | Left / right arrow | Pauses and seeks approximately one protected playback frame. |
| Bookmark | B | Pauses at the current time and opens the parent workbench’s bookmark flow, when available. |
| Playback speed | Visible selector | Changes playback to 0.25×, 0.5×, 1×, 1.5× or 2×. |

All seek and frame-step actions pause playback. Keyboard commands ignore text fields, selects, editable content, dialogs, modifier combinations, composition events and zone drawing. Space and arrow keys retain normal behavior when a button, link or native video control has focus. Holding Space or B does not repeatedly toggle playback or create bookmark actions.

Frame steps use **`playback_fps`**, the frame rate of the protected derivative. The source recording’s `fps` must not substitute for it: the transcoder can change the frame rate. Legacy recordings without known `playback_fps` keep frame controls disabled. Browser decoding, timing precision and variable-frame-rate originals mean that these are approximate playback-frame seeks, not guaranteed native-source frame navigation.

Playback pauses when the component leaves the active workspace, the recording/media source changes, zone drawing starts, the tab becomes hidden, or the component unmounts. Rejected play requests leave the player paused with a visible retry message. Single-player buffering uses normal browser buffering behavior and remains pausable.

## Saved sample strip

The strip accepts only a completed analysis for the current recording. It chooses at most 12 actual saved samples, spread across equal time intervals, and keeps intervals without samples visibly empty. It does not interpolate timestamps, reuse an image to fill a gap, or create new images from video or canvas.

Each image must reference that analysis’s exact protected frame route. The image requests use the existing `useProtectedMedia` authentication, media-type checks, image-size limit and cancellation behavior. Only nearby strip items load; pending requests cancel when their source leaves the view. Missing images retain their sample’s timestamp so the reviewer can still seek to that point. The strip scrolls horizontally within the component on narrow screens.

The prominent time on each tile is the saved sample timestamp; the smaller range identifies its timeline interval. Clicking a sample seeks to its exact recorded timestamp. The highlighted interval shows where the playhead currently falls and does not claim its thumbnail is the current decoded frame. Missing samples, gaps and missing images do not establish absence of incidents.

## Parent integration

`FootageNavigation` receives:

```ts
{
  video: { video_id: string; duration_s: number; playback_fps?: number | null };
  analysis: VideoAnalysis | null;
  playerRef: RefObject<HTMLVideoElement | null>;
  atS: number;
  onSeek: (seconds: number) => void;
  onBookmark?: (atS: number) => void;
  disabled?: boolean;
  drawing?: boolean;
  shortcutGuard?: boolean;
  active?: boolean;
  mediaKey?: string | null;
}
```

Pass the protected playback blob URL as `mediaKey` so player listeners are refreshed when the parent mounts or replaces its media element. Disable controls while media is loading or unavailable. Pass `drawing` while defining zones and `shortcutGuard` whenever another local flow should own the keyboard. Only the active mounted footage view should enable these shortcuts. Native player controls may remain enabled.

The parent owns seek state and bookmark persistence. The component’s bookmark action requires a completed analysis for the same recording, even when an `onBookmark` callback exists. Playback controls themselves remain usable without analysis. The component does not create bookmarks, start analysis, change configurations, or fetch a substitute analysis run.

## Verification

Run from `web/`:

```sh
npm run typecheck
node --import ./tests/register.mjs --test tests/footage-navigation.test.mjs
```

The focused tests cover shortcut guards and native controls, metadata-based seek math, recording bounds, timestamp rounding, bounded sample selection, sparse coverage, exact analysis image references, and unfinished or mismatched analysis state. Browser review should also exercise real playback, keyboard focus, thumbnail authentication, component navigation and narrow layouts; helper tests do not establish decoder or frame accuracy.
