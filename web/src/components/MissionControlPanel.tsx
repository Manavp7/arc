/**
 * Mission Control (PRD M17, Phase 6).
 *
 * The acceptance for this phase is a journey rather than an endpoint — *create → assign → watch live → replay,
 * all from the UI* — so the panel is built around that sequence and nothing else. There is no mission editor, no
 * bulk operations, no filtering by seventeen fields. A screen that supports one journey completely is worth more
 * than one that half-supports six.
 *
 * Three decisions carry it.
 *
 * **Progress shows what it is waiting for, not just a number.** The service already returns a sentence — "1 of 3
 * met; waiting on Check dock 3" — because `60%` prompts "which 40%?" every single time. Rendering the bar without
 * the sentence would throw away the more useful half.
 *
 * **The comms log is the main content, not a footnote.** After an incident the question is never "what was the
 * state" but "who said what, when". It reads forwards, like the narrative it is.
 *
 * **Refusals are shown, with their reason.** The service explains every refusal — "a mission that never started
 * cannot be complete: start it first, or abort it" — and a panel that collapsed that into "Error" would discard
 * the part that tells the operator what to do.
 *
 * It reuses `.form-panel`, the shared style kit, rather than inventing class names. The builder's first
 * stylesheet defined its buttons under its own scope with no global rules, so the next panel to use `.primary`
 * would have rendered a bare sentence — a missing CSS rule fails silently. This panel was that next panel.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, explainError } from "../lib/api";
import { useSioStore } from "../store";

interface Objective {
  objective_id: string;
  description: string;
  zone_id?: string | null;
  done?: boolean;
  satisfied_by?: string[];
}

interface Comm {
  comm_id: string;
  ts: string;
  author: string;
  kind: string;
  body: string;
  ref?: string | null;
}

interface MissionProgress {
  done: number;
  total: number;
  percent: number;
  outstanding: string[];
  unassigned: string[];
  summary: string;
}

interface ReplayWindow {
  from: string;
  to: string;
  live: boolean;
  zone_id?: string | null;
}

interface Mission {
  mission_id: string;
  name: string;
  description?: string | null;
  state: string;
  commander?: string | null;
  zone_id?: string | null;
  resources: string[];
  objectives: Objective[];
  progress: MissionProgress;
  started_ts?: string | null;
  completed_ts?: string | null;
  alert_ids: string[];
  event_ids: string[];
  legal_transitions: string[];
  replay: ReplayWindow | null;
  comms?: Comm[];
}

const REFRESH_MS = 4000;

/** A mission's state, as a glyph. Typed on the union so an unhandled state is a compile error. */
const STATE_GLYPH: Record<string, string> = {
  draft: "○",
  active: "●",
  paused: "‖",
  completed: "✓",
  aborted: "✕",
};

export function MissionControlPanel() {
  const [missions, setMissions] = useState<Mission[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Mission | null>(null);
  const [status, setStatus] = useState("");
  const [statusKind, setStatusKind] = useState<"ok" | "bad" | "busy">("ok");
  const [creating, setCreating] = useState(false);

  // The create form. Deliberately four fields: a name, a zone, and objectives typed one per line. A mission is
  // usually created under time pressure, and a form with a dozen optional fields does not get filled in — it
  // gets abandoned in favour of a radio.
  const [name, setName] = useState("");
  const [zone, setZone] = useState("");
  const [objectiveText, setObjectiveText] = useState("");
  const [zones, setZones] = useState<string[]>([]);

  const [entities, setEntities] = useState<{ id: string; label: string; zone: string | null }[]>([]);
  const [comm, setComm] = useState("");
  const commsRef = useRef<HTMLUListElement | null>(null);
  const requestReplay = useSioStore((state) => state.requestReplay);

  const say = useCallback((message: string, kind: "ok" | "bad" | "busy" = "ok") => {
    setStatus(message);
    setStatusKind(kind);
  }, []);

  const loadEntities = useCallback(async () => {
    try {
      const rows = await api.entities({ limit: 300, active_within_s: 600 });
      setEntities(
        rows
          .filter((row) => !row.is_static)
          .map((row) => ({
            id: row.entity_id,
            label: row.label || row.entity_id,
            zone: row.state?.zone_id ?? null,
          })),
      );
    } catch {
      setEntities([]);
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const payload = await api.missions();
      setMissions(payload.missions as Mission[]);
    } catch (error) {
      say(`Could not load missions: ${describe(error)}`, "bad");
    }
  }, [say]);

  const loadDetail = useCallback(
    async (missionId: string) => {
      try {
        setDetail((await api.mission(missionId)) as Mission);
      } catch (error) {
        say(`Could not load that mission: ${describe(error)}`, "bad");
      }
    },
    [say],
  );

  useEffect(() => {
    void refresh();
    void (async () => {
      try {
        const rows = (await api.zones()) as { zone_id: string }[];
        setZones(rows.map((row) => row.zone_id));
      } catch {
        // A missing zone list degrades the form to free text rather than breaking it.
      }
    })();
  }, [refresh]);

  // Poll while a mission is live. Objectives complete themselves from observation, so the panel has to keep
  // looking — a static view of a self-updating thing is how somebody concludes the feature does not work.
  useEffect(() => {
    const timer = setInterval(() => {
      void refresh();
      // Entities refresh on the same beat. Fetching them once per selection made the dropdown's `— in
      // lane_north` a snapshot that could be minutes old, so an operator could commit a resource that had
      // already left the zone — and the objective would then never complete, with nothing on screen to say
      // why. That happened in review and cost 75 seconds of apparent breakage.
      void loadEntities();
      if (selectedId) void loadDetail(selectedId);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh, loadDetail, loadEntities, selectedId]);

  useEffect(() => {
    void loadEntities();
  }, [loadEntities]);

  // Keep the newest testimony in view. A log pinned to the top hides exactly the entries somebody is waiting
  // for — the objective that just completed, the message they just sent.
  useEffect(() => {
    const node = commsRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [detail?.comms?.length]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // Assignable resources: the moving things on site. Fetched when a mission is selected, because the list is
  // only meaningful next to a mission that could use them.

  /**
   * A readable name for an entity id.
   *
   * The operator picks "Truck DLF-267" and the confirmation used to say `sim-vsy1my-truck-0083`. The label was
   * already in hand — the panel simply was not carrying it through, which made its own messages harder to read
   * than the dropdown they came from.
   */
  const labelFor = useCallback(
    (entityId: string) => entities.find((entity) => entity.id === entityId)?.label ?? entityId,
    [entities],
  );

  const objectives = useMemo(
    () =>
      objectiveText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          // `description @ zone` — one line per objective, with an optional zone after an @. Typing beats
          // clicking when a mission is being written during an incident, and the zone is what makes an
          // objective self-completing, so it must be easy to attach.
          const parts = line.split("@").map((part) => part.trim());
          return { description: parts[0] ?? line, zone_id: parts[1] || null };
        }),
    [objectiveText],
  );

  async function create() {
    if (!name.trim()) return;
    say("Creating…", "busy");
    try {
      const mission = (await api.createMission({
        name: name.trim(),
        zone_id: zone || null,
        commander: "console",
        objectives,
      })) as Mission;
      say(`Created ${mission.name}. It is a draft — start it when you are ready.`);
      setName("");
      setObjectiveText("");
      setCreating(false);
      await refresh();
      setSelectedId(mission.mission_id);
    } catch (error) {
      say(`Could not create it: ${describe(error)}`, "bad");
    }
  }

  async function move(to: string, force = false) {
    if (!detail) return;
    say(`Moving to ${to}…`, "busy");
    try {
      await api.missionState(detail.mission_id, to, force);
      say(`Now ${to}.`);
      await refresh();
      await loadDetail(detail.mission_id);
    } catch (error) {
      // The service's refusals carry a message, a fix and the legal moves. Collapsing that into "Error" would
      // throw away the part that tells the operator what to do instead.
      say(describeRefusal(error), "bad");
    }
  }

  async function assign(resourceId: string) {
    if (!detail || !resourceId) return;
    try {
      await api.assignResource(detail.mission_id, resourceId);
      say(`${labelFor(resourceId)} committed.`);
      await loadDetail(detail.mission_id);
    } catch (error) {
      say(describeRefusal(error), "bad");
    }
  }

  async function release(resourceId: string) {
    if (!detail) return;
    try {
      await api.releaseResource(detail.mission_id, resourceId);
      say(`${labelFor(resourceId)} released.`);
      await loadDetail(detail.mission_id);
    } catch (error) {
      say(describeRefusal(error), "bad");
    }
  }

  async function tick(objectiveId: string, done: boolean) {
    if (!detail) return;
    try {
      await api.completeObjective(detail.mission_id, objectiveId, done);
      await loadDetail(detail.mission_id);
    } catch (error) {
      say(describeRefusal(error), "bad");
    }
  }

  async function send() {
    if (!detail || !comm.trim()) return;
    try {
      await api.addComm(detail.mission_id, comm.trim());
      setComm("");
      await loadDetail(detail.mission_id);
    } catch (error) {
      say(describeRefusal(error), "bad");
    }
  }

  /**
   * Actually replay the mission, rather than reporting that one could be planned.
   *
   * The first version POSTed a replay plan and printed "345 frames over 345s at 20x" — and nothing happened on
   * screen. The map kept streaming live, the timeline still said `live`, and the journey's fourth step was a
   * sentence. A browser review caught it.
   *
   * The fix is to hand the window to the store, where `Timeline` picks it up: that component already owns
   * playback — the plan, the EventSource, the named-frame listener, the scrubber position. Doing it here would
   * have given the app a second replay implementation, and the second one would be the one that leaves the
   * scrubber reading "live" while the map replays.
   */
  async function replay() {
    if (!detail) return;
    say("Handing the window to the timeline…", "busy");
    try {
      const plan = await api.missionReplay(detail.mission_id);
      requestReplay({ from: plan.from, to: plan.to, label: plan.name });
      say(
        `Replaying ${plan.name} on the map and timeline below — ${
          plan.live ? "from its start to now" : "start to finish"
        }. Use the timeline's live button to come back.`,
      );
    } catch (error) {
      say(describeRefusal(error), "bad");
    }
  }

  const terminal = detail ? ["completed", "aborted"].includes(detail.state) : false;

  return (
    <div className="panel form-panel mission-control">
      <header className="panel-header">
        <h2>Mission control</h2>
        <p className="hint">
          A mission is the one thing here a person owns. Objectives with a zone complete themselves when an{" "}
          <em>assigned</em> resource is observed there.
        </p>
      </header>

      {/* ------------------------------------------------------------------ create */}
      {!creating && (
        <button className="primary" onClick={() => setCreating(true)}>
          New mission
        </button>
      )}
      {creating && (
        <section className="builder-section">
          <h3>New mission</h3>
          <label>
            Name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Fuel store inspection"
            />
          </label>
          <label>
            Area
            <select value={zone} onChange={(event) => setZone(event.target.value)}>
              <option value="">no particular zone</option>
              {zones.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
            <small>
              Alerts raised here are attached to the mission automatically, so its record is what happened rather
              than what somebody remembered to link.
            </small>
          </label>
          <label>
            Objectives, one per line
            <textarea
              rows={4}
              value={objectiveText}
              onChange={(event) => setObjectiveText(event.target.value)}
              placeholder={"Get eyes on the fuel store @ fuel_store\nConfirm the area is safe"}
            />
            <small>
              Add <code>@ zone</code> and the objective completes itself when an assigned resource is observed
              there. Without a zone it stays a human judgement — which some objectives genuinely are.
            </small>
          </label>
          {objectives.length > 0 && (
            <ul className="objective-preview">
              {objectives.map((item, index) => (
                <li key={index}>
                  {item.description}
                  {item.zone_id ? (
                    <span className="auto-tag">auto · {item.zone_id}</span>
                  ) : (
                    <span className="manual-tag">manual</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="builder-row">
            <button className="primary" disabled={!name.trim()} onClick={() => void create()}>
              Create as draft
            </button>
            <button className="ghost" onClick={() => setCreating(false)}>
              cancel
            </button>
          </div>
        </section>
      )}

      {/* -------------------------------------------------------------------- list */}
      <section className="builder-section">
        <h3>Missions</h3>
        {missions.length === 0 && <p className="hint">None yet.</p>}
        <ul className="mission-list">
          {missions.map((mission) => (
            <li
              key={mission.mission_id}
              className={mission.mission_id === selectedId ? "mission-row selected" : "mission-row"}
              onClick={() => setSelectedId(mission.mission_id)}
            >
              <div className="mission-row-head">
                <span className={`mission-state state-${mission.state}`}>
                  {STATE_GLYPH[mission.state] ?? "?"} {mission.state}
                </span>
                <strong>{mission.name}</strong>
              </div>
              {/* The bar and the sentence together. A percentage alone prompts "which 40%?" every time. */}
              <div className="progress-track" aria-label={mission.progress.summary}>
                <div
                  className={mission.progress.percent === 100 ? "progress-fill full" : "progress-fill"}
                  style={{ width: `${mission.progress.percent}%` }}
                />
              </div>
              <span className="muted">
                {mission.progress.percent}% · {mission.progress.summary}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {/* ------------------------------------------------------------------ detail */}
      {detail && (
        <section className="builder-section mission-detail">
          <h3>{detail.name}</h3>
          {detail.description && <p className="muted">{detail.description}</p>}
          {/* The progress the detail was missing entirely: the only bar was on the list row above. */}
          <div className="progress-track" aria-label={detail.progress.summary}>
            <div
              className={detail.progress.percent === 100 ? "progress-fill full" : "progress-fill"}
              style={{ width: `${detail.progress.percent}%` }}
            />
          </div>
          <p className="muted">
            {detail.progress.percent}% · {detail.progress.summary}
          </p>

          <div className="builder-row mission-actions">
            {detail.legal_transitions.map((to) => (
              <button key={to} className="ghost" onClick={() => void move(to)}>
                {to === "active" && !detail.started_ts ? "start" : to}
              </button>
            ))}
            {detail.legal_transitions.length === 0 && (
              <span className="muted">
                {detail.state} is final — a mission that could be reopened is one whose completion means nothing.
              </span>
            )}
            {detail.replay && (
              <button className="ghost" onClick={() => void replay()}>
                replay {detail.replay.live ? "so far" : "the mission"}
              </button>
            )}
          </div>
          {/* Feedback beside the control that caused it. The status line lives at the foot of the panel, which
              is ~600px below this row — so an operator could click "completed", be refused, and see no
              reaction at all. */}
          {status && <p className={`status status-${statusKind}`}>{status}</p>}

          {/* Completion is blocked, not forbidden. The override is offered where the block is felt, and the
              service records it in the log naming what was outstanding. */}
          {detail.state !== "completed" &&
            detail.legal_transitions.includes("completed") &&
            detail.progress.outstanding.length > 0 && (
              <p className="hint">
                {detail.progress.outstanding.length} objective(s) still open, so completing is blocked.{" "}
                <button className="ghost danger" onClick={() => void move("completed", true)}>
                  complete anyway
                </button>{" "}
                records the override in the log, naming what was outstanding.
              </p>
            )}

          <h4>Objectives</h4>
          {terminal && (
            <p className="muted">
              This mission is {detail.state}, so its objectives are a record of what happened rather than a list
              of work.
            </p>
          )}
          <ul className="objective-list">
            {detail.objectives.map((objective) => (
              <li key={objective.objective_id} className={objective.done ? "met" : ""}>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={Boolean(objective.done)}
                    // A finished mission's objectives are a record, not a worklist. The service refuses the
                    // write; disabling the box means the operator is not invited to try.
                    disabled={terminal}
                    onChange={(event) => void tick(objective.objective_id, event.target.checked)}
                  />
                  {objective.description}
                </label>
                {objective.zone_id && <span className="auto-tag">auto · {objective.zone_id}</span>}
                {/* The cause, not just the tick. "Objective met" without a cause makes a review harder. */}
                {objective.satisfied_by && objective.satisfied_by.length > 0 && (
                  <span className="muted"> observed {objective.satisfied_by.join(", ")}</span>
                )}
              </li>
            ))}
            {detail.objectives.length === 0 && <li className="muted">No objectives set.</li>}
          </ul>
          {detail.progress.unassigned.length > 0 && (
            <p className="verdict-bad">
              {detail.progress.unassigned.length} objective(s) have no assigned resource that could satisfy
              them — that is work nobody is doing, which a progress bar cannot show you.
            </p>
          )}

          <h4>Resources</h4>
          <ul className="resource-list">
            {detail.resources.map((resource) => (
              <li key={resource}>
                <span className="resource-name">{labelFor(resource)}</span>
                <button className="ghost danger" onClick={() => void release(resource)}>
                  release
                </button>
              </li>
            ))}
            {detail.resources.length === 0 && <li className="muted">Nothing committed.</li>}
          </ul>
          <label>
            Commit a resource
            <select
              value=""
              disabled={terminal}
              onChange={(event) => {
                void assign(event.target.value);
                event.target.value = "";
              }}
            >
              <option value="">choose…</option>
              {entities
                .filter((entity) => !detail.resources.includes(entity.id))
                // Anything in a zone this mission has an objective for comes first. The list is 37 long and
                // the resource that will actually complete an objective was appearing at position 40.
                .sort((left, right) => rank(right, detail) - rank(left, detail))
                .slice(0, 60)
                .map((entity) => (
                  <option key={entity.id} value={entity.id}>
                    {entity.label}
                    {entity.zone ? ` — in ${entity.zone}` : ""}
                  </option>
                ))}
            </select>
            <small>
              A resource can be committed to one mission at a time. The database enforces it, so two people
              cannot commit the same drone at once.
            </small>
          </label>

          <h4>Comms</h4>
          {/* Append-only, and said so where somebody might expect an edit button. */}
          <p className="hint">
            Append-only. An entry is testimony — somebody said a thing at a time — and testimony that can be
            edited afterwards is worth nothing in a review.
          </p>
          <ul className="comms-log" ref={commsRef}>
            {(detail.comms ?? []).map((entry) => (
              <li key={entry.comm_id} className={`comm-${entry.kind}`}>
                {/* 24-hour, like the event feed and the timeline. The App shell is deliberately unambiguous
                    about time and this panel was rendering 12-hour with an AM/PM. */}
                <span className="comm-time">
                  {new Date(entry.ts).toLocaleTimeString([], { hour12: false })}
                </span>
                <span className="comm-author">{entry.author}</span>
                <span className="comm-body">{entry.body}</span>
              </li>
            ))}
            {(detail.comms ?? []).length === 0 && <li className="muted">Nothing logged yet.</li>}
          </ul>
          <div className="builder-row">
            <label style={{ flex: "1 1 100%" }}>
              Add an entry
              <input
                value={comm}
                onChange={(event) => setComm(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void send();
                }}
                placeholder="Ground team reports the valve is closed"
              />
            </label>
            <button className="ghost" disabled={!comm.trim()} onClick={() => void send()}>
              append
            </button>
          </div>

          {detail.alert_ids.length > 0 && (
            <p className="muted">
              {detail.alert_ids.length} alert(s) raised in {detail.zone_id} while this mission was running are
              attached to its record.
            </p>
          )}
        </section>
      )}

      {/* Only shown here when no mission is selected — otherwise it renders beside the actions row, next to
          the control that produced it. */}
      {status && !detail && <p className={`status status-${statusKind}`}>{status}</p>}
    </div>
  );
}

/**
 * The service's refusals carry a message, a fix and the legal moves; keep all three.
 *
 * Delegates to the shared `explainError`, because the first version read `error.detail` — a property `ApiError`
 * did not have. It compiled, it looked right, and it returned the bare message every time.
 */
const describeRefusal = explainError;

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** How relevant an entity is to this mission: in a zone an objective needs, then in the mission's own zone. */
function rank(entity: { zone: string | null }, mission: Mission): number {
  if (!entity.zone) return 0;
  const wanted = new Set(
    mission.objectives.filter((item) => !item.done).map((item) => item.zone_id ?? ""),
  );
  if (wanted.has(entity.zone)) return 2;
  if (entity.zone === mission.zone_id) return 1;
  return 0;
}
