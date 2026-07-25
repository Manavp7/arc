/**
 * The Explanation drawer, shared across events, alerts and decisions (PRD M20).
 *
 * This is the highest-leverage component in the console, because **every service in the platform already
 * produces a full `Explanation` and until now nothing rendered it.** The fusion service records which sensors
 * corroborated an entity; the events engine records which clause matched and with what value; the forecaster
 * records its measured interval coverage; the decision service records the options it did *not* choose and
 * why. All of that was reachable only by curl.
 *
 * One component for all of them, deliberately. The alternative — a bespoke detail view per panel — would mean
 * four places to keep in step, and in practice three of them would show less. A shared drawer also enforces
 * something useful: any service whose explanation renders badly here has produced a poor explanation, and
 * that is worth seeing.
 *
 * The ordering is chosen to answer questions in the order people ask them: what happened, how sure are you,
 * what is that based on, what else did you consider, and what went wrong. Confidence sits near the top
 * because it changes how the rest should be read, and **degradation is a banner rather than a footnote** —
 * an answer produced without a model, or from a partly-invented series, must not look like one that was not.
 */

import { useEffect } from "react";

import type { Alert, Decision, Explanation, SioEvent } from "../types";

/** Anything with an explanation can be shown here. */
export interface Explainable {
  kind: "event" | "alert" | "decision";
  title: string;
  subtitle?: string;
  explanation: Explanation;
  /** Extra rows shown above the explanation, specific to the subject. */
  facts?: Array<[string, string]>;
  /** Actions offered in the footer, e.g. acknowledge or approve. */
  actions?: Array<{ label: string; onClick: () => void; primary?: boolean; danger?: boolean }>;
}

function confidenceTone(value: number): string {
  if (value >= 0.75) return "conf-high";
  if (value >= 0.45) return "conf-medium";
  return "conf-low";
}

function formatClock(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}

export function ExplanationDrawer({
  subject,
  onClose,
}: {
  subject: Explainable | null;
  onClose: () => void;
}) {
  // Escape closes it. A drawer that can only be dismissed with the mouse is one people leave open, and this
  // one covers the map.
  useEffect(() => {
    if (!subject) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [subject, onClose]);

  if (!subject) return null;
  const { explanation } = subject;
  const confidence = explanation.confidence ?? 0;

  return (
    <aside className="drawer" role="dialog" aria-label={`Why: ${subject.title}`}>
      <header className="drawer-head">
        <div>
          <span className={`drawer-kind kind-${subject.kind}`}>{subject.kind}</span>
          <h3>{subject.title}</h3>
          {subject.subtitle && <p className="drawer-sub">{subject.subtitle}</p>}
        </div>
        <button type="button" className="drawer-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>

      {/* A banner, not a footnote. An answer produced without a model, or from a partly-invented series,
          must not look like one that was not. */}
      {explanation.degraded && (
        <div className="drawer-degraded">
          <strong>Degraded.</strong> This conclusion was reached with something missing or substituted — see
          the notes below before acting on it.
        </div>
      )}

      <section className="drawer-section">
        <div className="drawer-confidence">
          <span className={`conf-badge ${confidenceTone(confidence)}`}>
            {(confidence * 100).toFixed(0)}% confident
          </span>
          {explanation.summary && <p className="drawer-summary">{explanation.summary}</p>}
        </div>
      </section>

      {subject.facts && subject.facts.length > 0 && (
        <section className="drawer-section">
          <h4>Detail</h4>
          <dl className="drawer-facts">
            {subject.facts.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {explanation.notes && explanation.notes.length > 0 && (
        <section className="drawer-section">
          <h4>Why</h4>
          <ul className="drawer-notes">
            {explanation.notes.map((note, index) => (
              <li key={index} className={note.startsWith("degraded") ? "note-degraded" : undefined}>
                {note}
              </li>
            ))}
          </ul>
        </section>
      )}

      {explanation.evidence && explanation.evidence.length > 0 && (
        <section className="drawer-section">
          <h4>Evidence</h4>
          {/* Ids are shown in full and monospaced, because their purpose is to be looked up. Truncating them
              to look tidy would remove the only reason they are here. */}
          <ul className="drawer-evidence">
            {explanation.evidence.map((item, index) => (
              <li key={index}>
                <span className="ev-kind">{item.kind}</span>
                <code>{item.ref}</code>
                {item.source_id && <span className="ev-source">{item.source_id}</span>}
                {typeof item.score === "number" && <span className="ev-score">{item.score.toFixed(2)}</span>}
                {item.note && <span className="ev-note">{item.note}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {explanation.alternatives && explanation.alternatives.length > 0 && (
        <section className="drawer-section">
          <h4>Considered and not chosen</h4>
          {/* The part that makes a recommendation reviewable rather than merely presented. */}
          <ul className="drawer-alternatives">
            {explanation.alternatives.map((alternative, index) => (
              <li key={index}>
                <span className="alt-hypothesis">{alternative.hypothesis}</span>
                {alternative.why_not && <span className="alt-why">{alternative.why_not}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {explanation.timeline && explanation.timeline.length > 0 && (
        <section className="drawer-section">
          <h4>Sequence</h4>
          <ol className="drawer-timeline">
            {explanation.timeline.map((entry, index) => (
              <li key={index}>
                <span className="tl-time">{formatClock(entry.ts)}</span>
                <span className={`tl-kind tl-${entry.kind}`}>{entry.kind}</span>
                <span className="tl-summary">{entry.summary}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {explanation.sources && explanation.sources.length > 0 && (
        <section className="drawer-section">
          <h4>Sources</h4>
          <p className="drawer-sources">{explanation.sources.join(", ")}</p>
        </section>
      )}

      {subject.actions && subject.actions.length > 0 && (
        <footer className="drawer-actions">
          {subject.actions.map((action) => (
            <button
              key={action.label}
              type="button"
              className={
                action.primary ? "drawer-btn drawer-btn-primary" : action.danger ? "drawer-btn drawer-btn-danger" : "drawer-btn"
              }
              onClick={action.onClick}
            >
              {action.label}
            </button>
          ))}
        </footer>
      )}
    </aside>
  );
}

// --- adapters ---------------------------------------------------------------------------------------
//
// Each subject type is turned into an `Explainable` here rather than in its panel, so the panels stay lists
// and the mapping lives next to the component that consumes it.

export function fromEvent(event: SioEvent): Explainable {
  return {
    kind: "event",
    title: event.explanation?.summary || event.type.replace(/_/g, " "),
    subtitle: `${event.severity} · ${formatClock(event.ts)}${event.zone_id ? ` · ${event.zone_id}` : ""}`,
    explanation: event.explanation,
    facts: [
      ["Type", event.type],
      ["Severity", event.severity],
      ["Confidence", `${((event.confidence ?? 0) * 100).toFixed(0)}%`],
      ...(event.rule_id ? ([["Rule", event.rule_id]] as Array<[string, string]>) : []),
      ...(event.zone_id ? ([["Zone", event.zone_id]] as Array<[string, string]>) : []),
      ...(event.entities?.length ? ([["Entities", event.entities.join(", ")]] as Array<[string, string]>) : []),
    ],
  };
}

export function fromAlert(
  alert: Alert,
  actions: Explainable["actions"] = [],
): Explainable {
  return {
    kind: "alert",
    title: alert.title,
    subtitle: `${alert.severity} · priority ${alert.score.toFixed(1)} · ${alert.state}`,
    explanation: alert.explanation,
    facts: [
      ["Priority", `${alert.score.toFixed(2)} — ${alert.urgency_reason ?? "no reason recorded"}`],
      ["State", alert.state],
      ["Occurrences", String(alert.count)],
      ["First seen", formatClock(alert.ts)],
      ["Last seen", formatClock(alert.last_ts)],
      ...(alert.zone_id ? ([["Zone", alert.zone_id]] as Array<[string, string]>) : []),
      ...(alert.ack_by ? ([["Acknowledged by", alert.ack_by]] as Array<[string, string]>) : []),
      ...(alert.decision_ids?.length
        ? ([["Recommendations", alert.decision_ids.join(", ")]] as Array<[string, string]>)
        : []),
    ],
    actions,
  };
}

export function fromDecision(
  decision: Decision,
  actions: Explainable["actions"] = [],
): Explainable {
  const chosen = decision.options?.find((option) => option.option_id === decision.chosen);
  return {
    kind: "decision",
    title: chosen?.expected_effect || decision.rationale || "Recommendation",
    subtitle: `${decision.approval} · ${decision.options?.length ?? 0} options · ${decision.solver ?? "no solver"}`,
    explanation: decision.explanation,
    facts: [
      ["Approval", decision.approval],
      ["Rationale", decision.rationale || "none recorded"],
      ["Proposed by", decision.proposed_by],
      ...(chosen
        ? ([
            ["Recommended", `${chosen.action} (score ${chosen.score.toFixed(1)})`],
            ["Cost", `${chosen.cost.toFixed(2)} km`],
            ["Risk", chosen.risk.toFixed(2)],
          ] as Array<[string, string]>)
        : []),
      ...(decision.approved_by ? ([["Approved by", decision.approved_by]] as Array<[string, string]>) : []),
    ],
    actions,
  };
}
