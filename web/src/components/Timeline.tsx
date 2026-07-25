/**
 * The timeline scrubber (PRD M8, UC5).
 *
 * Two distinct interactions, deliberately kept distinct because they have different costs:
 *
 * - **Scrubbing** is client-driven. Dragging the handle asks the server to reconstruct one instant, and
 *   the request is debounced — a drag across the strip is a hundred mousemove events, and firing a
 *   reconstruction for each would queue a hundred queries to render one frame nobody waited to see.
 * - **Playing** is server-driven over SSE. The server knows the frame schedule, so it emits a frame when
 *   one is due instead of the client polling and hoping. Polling would also drift: each round trip adds
 *   its own latency to the interval, so playback would run slower than requested by an amount nobody
 *   could measure from the client side.
 *
 * The strip behind the handle is an activity density plot, fetched as a fixed number of buckets counted
 * in the database. Fetching the events themselves would make the UI slower the further back you look,
 * which is precisely when you need it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { useSioStore } from "../store";
import type { ReplayFrame } from "../types";

const SPEEDS = [1, 5, 20, 60] as const;
/**
 * How often the strip re-asks how far history now extends.
 *
 * Ten seconds: fast enough that the window visibly tracks live time, slow enough that it is not a query per
 * second for a value that changes by one second per second.
 */
const BOUNDS_REFRESH_MS = 10_000;

const DEFAULT_WINDOW_MIN = 15;
/** Debounce for scrub requests. Long enough to skip the middle of a drag, short enough to feel direct. */
const SCRUB_DEBOUNCE_MS = 120;

interface Density {
  counts: number[];
  severe: number[];
  total: number;
}

function formatClock(iso: string | null): string {
  if (!iso) return "--:--:--";
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}

function formatAgo(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

export function Timeline() {
  const replayAt = useSioStore((state) => state.replayAt);
  const replayMode = useSioStore((state) => state.replayMode);
  const replayProgress = useSioStore((state) => state.replayProgress);
  const setHistory = useSioStore((state) => state.setHistory);
  const requestedReplay = useSioStore((state) => state.requestedReplay);
  const clearRequestedReplay = useSioStore(
    (state) => state.clearRequestedReplay,
  );
  const returnToLive = useSioStore((state) => state.returnToLive);

  const [bounds, setBounds] = useState<{ start: string; end: string } | null>(
    null,
  );
  const [windowMin, setWindowMin] = useState<number>(DEFAULT_WINDOW_MIN);
  const [density, setDensity] = useState<Density | null>(null);
  const [speed, setSpeed] = useState<number>(20);
  const [status, setStatus] = useState<string>("");
  const [lag, setLag] = useState<number>(0);

  const trackRef = useRef<HTMLDivElement | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const replayIdRef = useRef<string | null>(null);
  const scrubTimerRef = useRef<number | null>(null);

  /** The window the strip covers: the last `windowMin` minutes of recorded history. */
  const windowRange = useMemo(() => {
    const end = bounds ? new Date(bounds.end) : new Date();
    const start = new Date(end.getTime() - windowMin * 60_000);
    const earliest = bounds ? new Date(bounds.start) : start;
    return { start: start < earliest ? earliest : start, end };
  }, [bounds, windowMin]);

  // --- load the extent of history, and the density strip for the chosen window -------------
  //
  // Polled, not fetched once. The first version had `[]` deps, so the window's end was pinned to the extent
  // of history at mount and the strip read "live 07:31 - 07:46" for sixteen minutes of wall clock while the
  // map, the feed and the alerts all moved. A timeline that does not advance is worse than no timeline: it
  // is a clock that has stopped while still looking like a clock.
  //
  // Skipped while replaying, deliberately. During a replay the window must hold still — a scrubber whose
  // scale slides underneath the handle is unusable, and the whole point of the replay is that the moment
  // being examined stays put.
  useEffect(() => {
    let cancelled = false;
    const refresh = () => {
      if (replayAt) return;
      api
        .timelineBounds()
        .then((result) => {
          if (cancelled || !result.start || !result.end) return;
          setBounds({ start: result.start, end: result.end });
        })
        .catch(() => setStatus("history unavailable"));
    };
    refresh();
    const timer = window.setInterval(refresh, BOUNDS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [replayAt]);

  useEffect(() => {
    let cancelled = false;
    api
      .timelineDensity({
        from: windowRange.start.toISOString(),
        to: windowRange.end.toISOString(),
        buckets: 120,
      })
      .then((result) => {
        if (!cancelled)
          setDensity({
            counts: result.counts,
            severe: result.severe,
            total: result.total,
          });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [windowRange.start, windowRange.end]);

  // --- stop any stream when this unmounts, or the server keeps reconstructing for nobody -----
  const stopStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    if (replayIdRef.current) {
      // Tell the server. Without this the session sits in the registry doing database work for a
      // connection that has gone.
      void api.cancelReplay(replayIdRef.current).catch(() => undefined);
      replayIdRef.current = null;
    }
  }, []);

  useEffect(() => stopStream, [stopStream]);

  // --- scrubbing ----------------------------------------------------------------------------
  const scrubTo = useCallback(
    (fraction: number) => {
      const span = windowRange.end.getTime() - windowRange.start.getTime();
      const ts = new Date(
        windowRange.start.getTime() + span * Math.min(1, Math.max(0, fraction)),
      );
      stopStream();
      // Show the handle immediately, then fetch. Waiting for the response before moving the handle
      // makes the control feel broken during the request.
      useSioStore.getState().setReplayAt(ts.toISOString());
      if (scrubTimerRef.current) window.clearTimeout(scrubTimerRef.current);
      scrubTimerRef.current = window.setTimeout(() => {
        api
          .worldAt(ts.toISOString())
          .then((world) => {
            setHistory(ts.toISOString(), world.entities, "scrubbing");
            setStatus(
              `${world.counts.movers} moving, ${world.counts.static} fixed, ${world.counts.in_zones} in zones`,
            );
          })
          .catch(() => setStatus("could not reconstruct that instant"));
      }, SCRUB_DEBOUNCE_MS);
    },
    [setHistory, stopStream, windowRange],
  );

  const onTrackPointer = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const track = trackRef.current;
      if (!track) return;
      const rect = track.getBoundingClientRect();
      scrubTo((event.clientX - rect.left) / Math.max(1, rect.width));
    },
    [scrubTo],
  );

  const onTrackDrag = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      // Only while a button is held: without this check the world is reconstructed on plain hover.
      if (event.buttons === 0) return;
      onTrackPointer(event);
    },
    [onTrackPointer],
  );

  // --- playing ------------------------------------------------------------------------------
  const play = useCallback(
    async (override?: { from: string; to: string; label?: string }) => {
      stopStream();
      setStatus(
        override?.label
          ? `planning replay of ${override.label}…`
          : "planning replay…",
      );
      try {
        const plan = await api.planReplay({
          // An explicit window when another panel asked for one — Mission Control replays a mission's own
          // start-to-end rather than whatever the scrubber happens to be showing.
          from: override?.from ?? windowRange.start.toISOString(),
          to: override?.to ?? windowRange.end.toISOString(),
          speed,
        });
        replayIdRef.current = plan.replay_id;
        setStatus(
          (override?.label ? `${override.label}: ` : "") +
            `${plan.frames} frames at ${plan.speed}x, ${plan.resolution_s}s per frame` +
            (plan.capped ? " (resolution reduced to fit)" : ""),
        );

        const stream = new EventSource(
          `/api${plan.stream.replace(/^\/api/, "")}`,
        );
        streamRef.current = stream;
        // A NAMED event, so an explicit listener is required: `onmessage` only fires for unnamed frames,
        // and this exact mistake once silently dropped every live update in this app.
        stream.addEventListener("ReplayFrame", (message) => {
          const frame = JSON.parse(
            (message as MessageEvent).data,
          ) as ReplayFrame;
          setHistory(frame.ts, frame.entities, "playing", {
            progress: frame.progress,
            events: frame.events,
          });
          setLag(frame.lag_s);
        });
        stream.addEventListener("ReplayComplete", () => {
          setStatus("replay complete");
          stopStream();
        });
        stream.onerror = () => {
          setStatus("replay stream dropped");
          stopStream();
        };
      } catch {
        setStatus("could not start a replay");
      }
    },
    [setHistory, speed, stopStream, windowRange],
  );

  // Another panel asked for a window. Mission Control's "replay the mission" lands here, so the scrubber, the
  // map and the event feed all move together — which is the whole point of replay, and what a panel doing its
  // own playback would have got wrong.
  useEffect(() => {
    if (!requestedReplay) return;
    clearRequestedReplay();
    void play(requestedReplay);
  }, [requestedReplay, clearRequestedReplay, play]);

  const pause = useCallback(() => {
    stopStream();
    setStatus("paused");
  }, [stopStream]);

  const goLive = useCallback(() => {
    stopStream();
    returnToLive();
    setStatus("");
    setLag(0);
  }, [returnToLive, stopStream]);

  // --- handle position ----------------------------------------------------------------------
  const handleFraction = useMemo(() => {
    if (!replayAt) return 1;
    const span = windowRange.end.getTime() - windowRange.start.getTime();
    if (span <= 0) return 1;
    return Math.min(
      1,
      Math.max(
        0,
        (new Date(replayAt).getTime() - windowRange.start.getTime()) / span,
      ),
    );
  }, [replayAt, windowRange]);

  const peak = useMemo(
    () => Math.max(1, ...(density?.counts ?? [1])),
    [density],
  );
  const isLive = replayMode === "live";

  return (
    <div className="timeline">
      <div className="timeline-controls">
        <button
          type="button"
          className={isLive ? "tl-btn tl-live" : "tl-btn"}
          onClick={goLive}
          disabled={isLive}
          title="Follow the present"
        >
          ● live
        </button>
        {replayMode === "playing" ? (
          <button
            type="button"
            className="tl-btn"
            onClick={pause}
            title="Pause the replay"
          >
            ❙❙ pause
          </button>
        ) : (
          <button
            type="button"
            className="tl-btn"
            onClick={() => void play()}
            title="Replay this window"
          >
            ▶ play
          </button>
        )}
        <select
          className="tl-select"
          id="timeline-speed"
          name="speed"
          value={speed}
          onChange={(event) => setSpeed(Number(event.target.value))}
          title="Playback speed"
        >
          {SPEEDS.map((option) => (
            <option key={option} value={option}>
              {option}x
            </option>
          ))}
        </select>
        <select
          className="tl-select"
          id="timeline-window"
          name="window"
          value={windowMin}
          onChange={(event) => setWindowMin(Number(event.target.value))}
          title="How much history the strip covers"
        >
          {[5, 15, 60, 240].map((option) => (
            <option key={option} value={option}>
              last {option < 60 ? `${option}m` : `${option / 60}h`}
            </option>
          ))}
        </select>
      </div>

      <div
        className="timeline-track"
        ref={trackRef}
        onPointerDown={onTrackPointer}
        onPointerMove={onTrackDrag}
        role="slider"
        tabIndex={0}
        aria-label="Scrub through recorded history"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(handleFraction * 100)}
      >
        <div className="timeline-density">
          {(density?.counts ?? []).map((count, index) => {
            const severe = density?.severe[index] ?? 0;
            return (
              <span
                key={index}
                className={severe > 0 ? "tl-bar tl-bar-severe" : "tl-bar"}
                style={{ height: `${Math.max(6, (count / peak) * 100)}%` }}
                title={`${count} event${count === 1 ? "" : "s"}${severe ? `, ${severe} severe` : ""}`}
              />
            );
          })}
        </div>
        <div
          className="timeline-handle"
          style={{ left: `${handleFraction * 100}%` }}
        />
        {replayMode === "playing" && (
          <div
            className="timeline-progress"
            style={{ width: `${replayProgress * 100}%` }}
          />
        )}
      </div>

      <div className="timeline-readout">
        <span className={isLive ? "tl-clock tl-clock-live" : "tl-clock"}>
          {isLive ? "live" : formatClock(replayAt)}
        </span>
        {!isLive && <span className="tl-ago">{formatAgo(replayAt)}</span>}
        {status && <span className="tl-status">{status}</span>}
        {/* Lag is surfaced rather than hidden: a replay claiming 20x while running slower is the one
            thing a viewer cannot otherwise detect. */}
        {lag > 0.25 && (
          <span className="tl-lag">
            running {lag.toFixed(1)}s behind schedule
          </span>
        )}
        <span className="tl-window">
          {formatClock(windowRange.start.toISOString())} –{" "}
          {formatClock(windowRange.end.toISOString())}
        </span>
      </div>
    </div>
  );
}
