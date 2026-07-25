/**
 * The decisions panel: recommendations awaiting a human (PRD M13/M15, Tier 2 #1).
 *
 * This panel is where human-on-the-loop stops being an architecture diagram and becomes a button. The
 * decision service produces ranked options and will not act on any of them; the agents service acts only on
 * an *approved* decision. So until this panel existed, the entire approval path was reachable only by curl —
 * the platform could recommend and could execute, and nothing let a person say yes.
 *
 * Two design choices worth stating:
 *
 * **Every option is shown, including the ones not recommended and the infeasible ones.** A panel that shows
 * only the winner is asking for assent, not review; being able to see that the second option costs 1.2 km
 * more is what makes approval meaningful. Infeasible options are shown greyed with their reason, because
 * "why isn't the nearer truck being sent" is a question with an answer.
 *
 * **Approving a *specific* option, not just the decision.** The operator may prefer the second choice, and
 * forcing them to accept the solver's ranking or nothing would make the panel a formality.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "../lib/api";
import type { Decision, DecisionOption } from "../types";
import { fromDecision, type Explainable } from "./ExplanationDrawer";

const REFRESH_MS = 6000;

export function DecisionsPanel({ onExplain }: { onExplain: (subject: Explainable) => void }) {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.decisions({ limit: 30 });
      setDecisions(response.decisions ?? []);
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

  const decide = useCallback(
    async (decision: Decision, verdict: "approve" | "reject", optionId?: string) => {
      setBusy(decision.decision_id);
      try {
        if (verdict === "approve") {
          const result = await api.approveDecision(decision.decision_id, optionId);
          // Say what was authorised, not merely that something was. "Approved" alone leaves the operator
          // unsure which option they just committed the site to.
          setFlash(`Approved ${result.chosen ?? optionId ?? "the recommendation"} — the agent may now act.`);
        } else {
          await api.rejectDecision(decision.decision_id, "rejected from the console");
          setFlash("Rejected. No action will be taken.");
        }
        await load();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(null);
        window.setTimeout(() => setFlash(null), 6000);
      }
    },
    [load],
  );

  const pending = decisions.filter((decision) => decision.approval === "pending");
  const settled = decisions.filter((decision) => decision.approval !== "pending");

  return (
    <div className="panel-body decisions-panel">
      {flash && <p className="panel-flash">{flash}</p>}
      {error && <p className="panel-error">{error}</p>}

      {decisions.length === 0 && !error && (
        <p className="panel-empty">
          No recommendations yet. The decision service proposes options when a high-severity event needs a
          response — and never acts on them itself.
        </p>
      )}

      {pending.length > 0 && (
        <div className="decisions-group">
          <h4 className="decisions-heading">
            Awaiting approval <span className="pill pill-escalated">{pending.length}</span>
          </h4>
          {pending.map((decision) => (
            <DecisionCard
              key={decision.decision_id}
              decision={decision}
              expanded={expanded === decision.decision_id}
              busy={busy === decision.decision_id}
              onToggle={() =>
                setExpanded(expanded === decision.decision_id ? null : decision.decision_id)
              }
              onExplain={() =>
                onExplain(
                  fromDecision(decision, [
                    {
                      label: "Approve recommended",
                      onClick: () => void decide(decision, "approve"),
                      primary: true,
                    },
                    { label: "Reject", onClick: () => void decide(decision, "reject"), danger: true },
                  ]),
                )
              }
              onApprove={(optionId) => void decide(decision, "approve", optionId)}
              onReject={() => void decide(decision, "reject")}
            />
          ))}
        </div>
      )}

      {settled.length > 0 && (
        <div className="decisions-group">
          <h4 className="decisions-heading decisions-heading-quiet">Settled</h4>
          {settled.map((decision) => (
            <DecisionCard
              key={decision.decision_id}
              decision={decision}
              expanded={expanded === decision.decision_id}
              busy={false}
              onToggle={() =>
                setExpanded(expanded === decision.decision_id ? null : decision.decision_id)
              }
              onExplain={() => onExplain(fromDecision(decision))}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function DecisionCard({
  decision,
  expanded,
  busy,
  onToggle,
  onExplain,
  onApprove,
  onReject,
}: {
  decision: Decision;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  onExplain: () => void;
  onApprove?: (optionId: string) => void;
  onReject?: () => void;
}) {
  const chosen = decision.options?.find((option) => option.option_id === decision.chosen);
  return (
    <article className={`decision-card approval-${decision.approval}`}>
      <header onClick={onToggle} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onToggle()}>
        <div className="decision-headline">
          <span className={`approval-tag approval-${decision.approval}`}>{decision.approval}</span>
          <span className="decision-effect">{chosen?.expected_effect ?? decision.expected_effect}</span>
        </div>
        <div className="decision-sub">
          {decision.rationale || "no rationale recorded"}
          <span className="decision-meta">
            {decision.options?.length ?? 0} options · {decision.solver ?? "no solver"} ·{" "}
            {new Date(decision.ts).toLocaleTimeString([], { hour12: false })}
          </span>
        </div>
      </header>

      {expanded && (
        <div className="decision-options">
          {/* When two options score the same, the ranking between them is arbitrary and the UI must not
              imply otherwise. An operator who sees "recommended" on one of two identical rows reasonably
              assumes something distinguishes them. */}
          {tiedTop(decision) && (
            <p className="decision-tie">
              the top {tiedTop(decision)} options score the same — the order between them is arbitrary
            </p>
          )}
          {decision.options?.map((option) => (
            <OptionRow
              key={option.option_id}
              option={option}
              recommended={option.option_id === decision.chosen}
              approvable={decision.approval === "pending" && Boolean(onApprove)}
              busy={busy}
              onApprove={() => onApprove?.(option.option_id)}
            />
          ))}
        </div>
      )}

      <footer className="decision-actions">
        <button type="button" className="link-btn" onClick={onToggle}>
          {expanded ? "hide options" : `show ${decision.options?.length ?? 0} options`}
        </button>
        <button type="button" className="link-btn" onClick={onExplain}>
          why?
        </button>
        {decision.approval === "pending" && onApprove && (
          <>
            <button
              type="button"
              className="decision-btn decision-btn-approve"
              disabled={busy}
              onClick={() => onApprove(decision.chosen ?? "")}
            >
              approve
            </button>
            <button
              type="button"
              className="decision-btn decision-btn-reject"
              disabled={busy}
              onClick={onReject}
            >
              reject
            </button>
          </>
        )}
      </footer>
    </article>
  );
}

/** How many options share the top score, when more than one does. */
function tiedTop(decision: Decision): number {
  const scores = (decision.options ?? [])
    .filter((option) => option.feasible && option.action !== "no_action")
    .map((option) => option.score);
  if (scores.length < 2) return 0;
  const best = Math.max(...scores);
  const tied = scores.filter((score) => Math.abs(score - best) < 0.05).length;
  return tied > 1 ? tied : 0;
}

function OptionRow({
  option,
  recommended,
  approvable,
  busy,
  onApprove,
}: {
  option: DecisionOption;
  recommended: boolean;
  approvable: boolean;
  busy: boolean;
  onApprove: () => void;
}) {
  return (
    <div className={`option-row${recommended ? " option-recommended" : ""}${option.feasible ? "" : " option-infeasible"}`}>
      <div className="option-score">{option.score.toFixed(1)}</div>
      <div className="option-main">
        <div className="option-action">
          {option.action.replace(/_/g, " ")}
          {recommended && <span className="option-tag">recommended</span>}
          {!option.feasible && <span className="option-tag option-tag-no">not possible</span>}
        </div>
        <div className="option-effect">{option.expected_effect}</div>
        <div className="option-metrics">
          <span>cost {option.cost.toFixed(2)}</span>
          <span>risk {option.risk.toFixed(2)}</span>
          {Object.entries(option.expected_metrics ?? {})
            .slice(0, 3)
            .map(([key, value]) => (
              <span key={key}>
                {key.replace(/_/g, " ")} {typeof value === "number" ? value.toFixed(1) : String(value)}
              </span>
            ))}
        </div>
        {/* Shown, not hidden: "why isn't the nearer truck being sent" is a question with an answer. */}
        {option.rejection_reason && <div className="option-why-not">{option.rejection_reason}</div>}
      </div>
      {approvable && option.feasible && (
        <button
          type="button"
          className="option-approve"
          disabled={busy}
          onClick={onApprove}
          title={
            option.action === "no_action"
              ? "Record a deliberate decision to take no action"
              : "Authorise this option specifically"
          }
        >
          {/* "Approve doing nothing" is deliberately offered and deliberately worded. Choosing not to act
              is a real decision an operator should be able to record, and it belongs in the audit trail —
              but a button reading "approve this" on a no-action row reads like a mistake. */}
          {option.action === "no_action" ? "approve doing nothing" : "approve this"}
        </button>
      )}
    </div>
  );
}
