/**
 * The alerts inbox (PRD M16, Tier 2 #2).
 *
 * An inbox is judged on one thing: whether the most important item is at the top and obviously so. Everything
 * here serves that. The list is ordered by the server (escalated first, then priority) and this component
 * does **not** re-sort it — two sort orders, one in the service and one in the UI, is a bug waiting for a
 * demo, and the service is the one that can explain its ordering.
 *
 * Each row shows the priority score *and the sentence that justifies it*, because a bare number invites the
 * question it cannot answer. Acknowledge and escalate act in place and reconcile against the server's reply,
 * so a row cannot show "acknowledged" if the write failed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import type { Alert } from "../types";
import { fromAlert, type Explainable } from "./ExplanationDrawer";

const REFRESH_MS = 5000;

/**
 * Rows rendered at once.
 *
 * The inbox rendered all hundred, each with buttons, in a scroller nobody reads past the first screen of.
 * The cap is safe *because* the server orders the list: the rows that matter are the ones at the top, which
 * is exactly the reason this component does not re-sort.
 */
const ROWS = 40;

function ageOf(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function AlertsPanel({ onExplain }: { onExplain: (subject: Explainable) => void }) {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [groups, setGroups] = useState<
    Array<{ kind: string; count: number; max_score: number; alerts?: Alert[] }>
  >([]);
  const [filter, setFilter] = useState<string>("live");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  /** Alerts this operator has just acted on, kept visible so their own action does not vanish. */
  const justActed = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const state = filter === "live" ? undefined : filter;
      const response = await api.alertInbox({ state, limit: 100 });
      // "live" is open + escalated, which the API cannot express in one state filter. Filtering here keeps
      // the default view free of resolved noise without inventing a server-side pseudo-state.
      const rows =
        filter === "live"
          ? response.alerts.filter(
              (alert) =>
                alert.state === "open" ||
                alert.state === "escalated" ||
                // Keep what this operator just acknowledged. Dropping it on the next poll made their own
                // action visible for about four seconds and then vanish, which reads as the click having
                // failed. It leaves on the poll after they navigate away.
                justActed.current.has(alert.alert_id),
            )
          : response.alerts;
      setAlerts(rows);
      setGroups(response.groups ?? []);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [filter]);

  useEffect(() => {
    // Switching filter is a deliberate change of view, so the "keep what I just acted on" set is dropped:
    // carrying it over would leak acknowledged rows into a filter that explicitly excludes them.
    justActed.current.clear();
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = useCallback(
    async (alert: Alert, action: "ack" | "escalate" | "resolve") => {
      setBusy(alert.alert_id);
      try {
        const updated =
          action === "ack"
            ? await api.acknowledgeAlert(alert.alert_id, "acknowledged from the console")
            : action === "escalate"
              ? await api.escalateAlert(alert.alert_id, "escalated from the console")
              : await api.resolveAlert(alert.alert_id, "resolved from the console");
        // Reconcile against what the server said, rather than assuming. A row that shows "acknowledged"
        // when the write failed is worse than one that did not appear to respond.
        justActed.current.add(updated.alert_id);
        setAlerts((current) =>
          current.map((row) => (row.alert_id === updated.alert_id ? updated : row)),
        );
        setError(null);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const explain = useCallback(
    (alert: Alert) =>
      onExplain(
        fromAlert(alert, [
          ...(alert.state === "open" || alert.state === "escalated"
            ? [{ label: "Acknowledge", onClick: () => void act(alert, "ack"), primary: true }]
            : []),
          ...(alert.state !== "resolved"
            ? [{ label: "Resolve", onClick: () => void act(alert, "resolve") }]
            : []),
          ...(alert.state === "open"
            ? [{ label: "Escalate", onClick: () => void act(alert, "escalate"), danger: true }]
            : []),
        ]),
      ),
    [act, onExplain],
  );

  const counts = useMemo(() => {
    // "Needs attention" is open PLUS escalated, matching the top bar exactly. They disagreed before: the
    // panel counted only state === "open" and read "0 open" while the header said "10 open alerts" — the
    // same word meaning two things in one screen, and the panel's version implied an empty inbox that was
    // in fact a hundred rows long.
    const escalated = alerts.filter((alert) => alert.state === "escalated").length;
    const open = alerts.filter((alert) => alert.state === "open").length;
    return { open, escalated, attention: open + escalated };
  }, [alerts]);

  return (
    <div className="panel-body alerts-panel">
      <div className="alerts-head">
        <div className="alerts-counts">
          <span className="pill">{counts.attention} need attention</span>
          {counts.escalated > 0 && (
            <span className="pill pill-escalated" title="Unacknowledged past their escalation timer">
              {counts.escalated} escalated
            </span>
          )}
          {groups.slice(0, 3).map((group) => (
            <span
              key={group.kind}
              className="pill pill-quiet"
              title={`${group.count} occurrences folded into ${group.alerts?.length ?? 0} alert(s)`}
            >
              {/* "occurrences", not a bare multiplier. These count folded repeats while the rows below
                  count alerts, so three chips reading ×33 ×38 ×30 above 100 rows summed to 101 and looked
                  like an arithmetic error. Both numbers were right; only the label was ambiguous. */}
              {group.kind.replace(/_/g, " ")} {group.count} occurrences
            </span>
          ))}
        </div>
        <select
          className="alerts-filter"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Filter alerts by state"
        >
          <option value="live">live</option>
          <option value="open">open</option>
          <option value="escalated">escalated</option>
          <option value="acknowledged">acknowledged</option>
          <option value="resolved">resolved</option>
        </select>
      </div>

      {error && <p className="panel-error">{error}</p>}

      {alerts.length === 0 && !error && (
        // Says which query returned nothing. "No alerts" over a filtered view is misleading — the operator
        // reads it as "nothing is happening" when it means "nothing matching this filter".
        <p className="panel-empty">
          No {filter === "live" ? "open or escalated" : filter} alerts. Only medium severity and above reach
          the inbox.
        </p>
      )}

      {alerts.length > ROWS && (
        <p className="feed-note">
          showing the {ROWS} highest-priority of {alerts.length} — narrow the filter to see the rest
        </p>
      )}

      <ul className="alerts-list">
        {alerts.slice(0, ROWS).map((alert) => (
          <li
            key={alert.alert_id}
            className={`alert-row sev-${alert.severity} state-${alert.state}`}
            onClick={() => explain(alert)}
            onKeyDown={(event) => {
              if (event.key === "Enter") explain(alert);
            }}
            tabIndex={0}
            role="button"
          >
            <div className="alert-score" title={alert.urgency_reason ?? undefined}>
              {alert.score.toFixed(0)}
            </div>
            <div className="alert-main">
              <div className="alert-title">
                {alert.title}
                {alert.count > 1 && <span className="alert-count">×{alert.count}</span>}
              </div>
              {/* The number never appears without the sentence that justifies it — and that sentence is
                  about the SCORE. The escalation reason is a different fact and gets its own line, having
                  once overwritten this one so that every row's justification for its priority read
                  "unacknowledged for 21 min". */}
              <div className="alert-reason">{alert.urgency_reason}</div>
              {alert.escalation_reason && (
                <div className="alert-escalation">{alert.escalation_reason}</div>
              )}
              <div className="alert-meta">
                <span className={`sev-tag sev-${alert.severity}`}>{alert.severity}</span>
                {alert.zone_id && <span>{alert.zone_id}</span>}
                <span>{ageOf(alert.last_ts)} ago</span>
                {alert.state === "escalated" && <span className="tag-escalated">escalated</span>}
                {alert.ack_by && <span className="tag-acked">ack {alert.ack_by}</span>}
                {(alert.decision_ids?.length ?? 0) > 0 && (
                  <span className="tag-decision">{alert.decision_ids?.length} recommendation(s)</span>
                )}
              </div>
            </div>
            <div className="alert-actions" onClick={(event) => event.stopPropagation()}>
              {(alert.state === "open" || alert.state === "escalated") && (
                <button
                  type="button"
                  disabled={busy === alert.alert_id}
                  onClick={() => void act(alert, "ack")}
                  title="Acknowledge — stops the escalation timer"
                >
                  ack
                </button>
              )}
              {alert.state === "open" && (
                <button
                  type="button"
                  disabled={busy === alert.alert_id}
                  onClick={() => void act(alert, "escalate")}
                  title="Escalate now"
                >
                  esc
                </button>
              )}
              {alert.state !== "resolved" && (
                <button
                  type="button"
                  disabled={busy === alert.alert_id}
                  onClick={() => void act(alert, "resolve")}
                  title="Resolve"
                >
                  done
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
