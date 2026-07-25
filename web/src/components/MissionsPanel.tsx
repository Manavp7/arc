/**
 * Missions: playbook runs, step by step (PRD M15).
 *
 * The demo turns on this panel. "A fire is detected and five steps visibly run" is the claim, and until now
 * the run existed only in Postgres and a log line — the most demonstrable thing the platform does was the
 * least visible.
 *
 * Per-step status rather than a progress bar, deliberately. A bar says 60 % and hides which step is stuck; a
 * step list says `dispatch_drone ok, notify_security retrying (2)`, which is the thing an operator needs. The
 * runner already tracks attempts, timeouts and compensation, so the panel's job is only to not throw that
 * away.
 *
 * Compensation is shown in its own colour. A run that failed and *undid itself* is a success of the design
 * and a failure of the attempt, and those must not look the same as either a clean run or an abandoned one.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";
import type { RunStatus } from "../types";

interface Step {
  name: string;
  status: RunStatus;
  attempts: number;
  error?: string | null;
  detail?: string | null;
}

interface Run {
  run_id: string;
  playbook: string;
  status: RunStatus;
  progress: number;
  started: string;
  steps: Step[];
  trigger_event?: string | null;
  dry_run?: boolean;
}

const REFRESH_MS = 4000;

/**
 * Typed as `Record<RunStatus, string>`, which is the point.
 *
 * The first version keyed on `Record<string, string>` and invented its own vocabulary — `ok`, `succeeded`,
 * `skipped`, `timed_out` — none of which the service emits. It emits `completed`. So every step on a
 * completed mission rendered as `·`, the fallback for "unknown", and the panel silently showed no status at
 * all on the one thing the demo is about.
 *
 * With the union as the key type, a status the UI has not handled is a COMPILE ERROR, and a status the UI
 * invents is too. The vocabulary now has exactly one definition and TypeScript enforces it.
 */
const STATUS_GLYPH: Record<RunStatus, string> = {
  completed: "✓",
  running: "…",
  pending: "·",
  failed: "✕",
  cancelled: "–",
  compensated: "↩",
};

export function MissionsPanel() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [summary, setSummary] = useState<{ runs: number; suppressed: number; byPlaybook: Record<string, number> }>({
    runs: 0,
    suppressed: 0,
    byPlaybook: {},
  });
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.workflowRuns(25);
      setRuns((response.recent ?? []) as Run[]);
      setSummary({
        runs: response.runs ?? 0,
        suppressed: response.suppressed_by_cooldown ?? 0,
        byPlaybook: response.by_playbook ?? {},
      });
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="panel-body missions-panel">
      {error && <p className="panel-error">{error}</p>}

      <div className="missions-head">
        <span className="pill">
          {summary.runs} {summary.runs === 1 ? "run" : "runs"}
        </span>
        {summary.suppressed > 0 && (
          <span className="pill pill-quiet" title="Repeat triggers held back by the playbook's cooldown">
            {summary.suppressed} held by cooldown
          </span>
        )}
        {Object.entries(summary.byPlaybook)
          .slice(0, 3)
          .map(([name, count]) => (
            <span key={name} className="pill pill-quiet">
              {name.replace(/_/g, " ")} ×{count}
            </span>
          ))}
      </div>

      {runs.length === 0 && !error && (
        <p className="panel-empty">
          No playbooks have run. A critical event — a fire, an intrusion — triggers one automatically, and every
          step is recorded here as it happens.
        </p>
      )}

      {runs.map((run) => (
        <article key={run.run_id} className={`mission-card mission-${run.status}`}>
          <header>
            <span className={`mission-status mission-status-${run.status}`}>{run.status}</span>
            <span className="mission-name">{run.playbook.replace(/_/g, " ")}</span>
            <span className="mission-time">
              {new Date(run.started).toLocaleTimeString([], { hour12: false })}
            </span>
          </header>

          {/* Per-step, not a bar. A bar says 60% and hides which step is stuck. */}
          <ol className="mission-steps">
            {run.steps.map((step) => (
              <li key={step.name} className={`step step-${step.status}`}>
                <span className="step-glyph">{STATUS_GLYPH[step.status] ?? "·"}</span>
                <span className="step-name">{step.name.replace(/_/g, " ")}</span>
                {step.attempts > 1 && (
                  <span className="step-attempts" title="Retried">
                    ×{step.attempts}
                  </span>
                )}
                {step.error && <span className="step-error">{step.error}</span>}
              </li>
            ))}
          </ol>

          <footer className="mission-foot">
            {run.dry_run && (
              <span className="pill pill-quiet" title="Steps recorded what they would have done">
                dry run
              </span>
            )}
            {run.steps.some((step) => step.status === "compensated") && (
              // A run that failed and undid itself is a success of the design and a failure of the attempt.
              <span className="pill pill-compensated">undone — compensation ran</span>
            )}
            {run.trigger_event && <code className="mission-trigger">{run.trigger_event}</code>}
          </footer>
        </article>
      ))}
    </div>
  );
}
