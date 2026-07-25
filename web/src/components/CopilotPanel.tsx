/**
 * The copilot panel, with the evidence tree that makes its answers checkable (PRD M11).
 *
 * A copilot that answers fluently and cannot show its work is a liability, not a feature: a wrong number in a
 * confident sentence is harder to catch than an obvious error. So this panel gives equal weight to the answer
 * and to how it was reached — the tool calls, in order, with their latencies and their results.
 *
 * Three things it deliberately does:
 *
 * **The trace is expandable, not hidden.** Each step shows which tool ran, how long it took and whether it
 * succeeded. When an answer looks wrong, the step list is where you find out that `spatial_query` returned
 * nothing rather than that the model invented a figure.
 *
 * **Degradation is shown next to the answer.** The agent reports when it fell back to a template, when a
 * tool failed, or when a small model needed its output repaired. An answer that arrived by fallback must not
 * look like one that did not.
 *
 * **Latency is displayed.** A local 3 B model takes seconds, and a panel that hides that trains the user to
 * think the app has frozen. Showing the number sets the expectation honestly.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import type { Explanation } from "../types";
import { type Explainable } from "./ExplanationDrawer";

interface Turn {
  question: string;
  answer?: string;
  confidence?: number;
  explanation?: Explanation;
  trace?: {
    model: string;
    total_ms: number;
    tools_used: string[];
    used_fallback: boolean;
    degraded: string[];
    steps: Array<{
      index: number;
      kind: string;
      tool?: string | null;
      detail: string;
      latency_ms: number;
      ok: boolean;
    }>;
  };
  elapsed_ms?: number;
  error?: string;
  pending?: boolean;
}

const SUGGESTIONS = [
  "What is on site right now?",
  "Which vehicles are near the fuel store?",
  "What happened in the last ten minutes?",
  "Is anything unusual about the temperature readings?",
  "How busy will dock 3 be in the next half hour?",
];

export function CopilotPanel({ onExplain }: { onExplain: (subject: Explainable) => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const ask = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || asking) return;
      setQuestion("");
      setAsking(true);
      // The pending turn goes in immediately. A local model takes seconds and a panel that shows nothing
      // until it finishes looks broken.
      setTurns((current) => [...current, { question: trimmed, pending: true }]);
      try {
        const reply = await api.ask(trimmed);
        setTurns((current) =>
          current.map((turn, index) =>
            index === current.length - 1
              ? {
                  question: trimmed,
                  answer: reply.answer,
                  confidence: reply.confidence,
                  explanation: reply.explanation,
                  trace: reply.trace,
                  elapsed_ms: reply.elapsed_ms,
                }
              : turn,
          ),
        );
        setExpanded(null);
      } catch (cause) {
        setTurns((current) =>
          current.map((turn, index) =>
            index === current.length - 1
              ? {
                  question: trimmed,
                  error:
                    cause instanceof Error
                      ? cause.message
                      : "the copilot did not answer (is the copilot service running?)",
                }
              : turn,
          ),
        );
      } finally {
        setAsking(false);
      }
    },
    [asking],
  );

  return (
    <div className="panel-body copilot-panel">
      <div className="copilot-log">
        {turns.length === 0 && (
          <div className="copilot-intro">
            <p>
              Ask about the site. The copilot queries the platform&apos;s own services — entities, spatial
              queries, the timeline, forecasts — and shows every call it made.
            </p>
            <div className="copilot-suggestions">
              {SUGGESTIONS.map((suggestion) => (
                <button key={suggestion} type="button" onClick={() => void ask(suggestion)}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, index) => (
          <div key={index} className="copilot-turn">
            <p className="copilot-question">{turn.question}</p>

            {turn.pending && <p className="copilot-thinking">thinking… a local model takes a few seconds</p>}
            {turn.error && <p className="panel-error">{turn.error}</p>}

            {turn.answer && (
              <>
                <p className="copilot-answer">{turn.answer}</p>
                <div className="copilot-meta">
                  {typeof turn.confidence === "number" && (
                    <span className="pill pill-quiet">{(turn.confidence * 100).toFixed(0)}% confident</span>
                  )}
                  {turn.trace && (
                    <>
                      <span className="pill pill-quiet">{(turn.trace.total_ms / 1000).toFixed(1)}s</span>
                      <span className="pill pill-quiet">{turn.trace.model}</span>
                      {turn.trace.tools_used.length > 0 && (
                        <span className="pill pill-quiet">{turn.trace.tools_used.join(" → ")}</span>
                      )}
                      {/* An answer that arrived by fallback must not look like one that did not. */}
                      {turn.trace.used_fallback && <span className="pill pill-degraded">answered without the model</span>}
                      {turn.trace.degraded.length > 0 && (
                        <span className="pill pill-degraded" title={turn.trace.degraded.join("; ")}>
                          degraded
                        </span>
                      )}
                    </>
                  )}
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => setExpanded(expanded === index ? null : index)}
                  >
                    {expanded === index ? "hide how" : "how?"}
                  </button>
                  {turn.explanation && (
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() =>
                        onExplain({
                          kind: "event",
                          title: turn.question,
                          subtitle: turn.trace
                            ? `${turn.trace.model} · ${(turn.trace.total_ms / 1000).toFixed(1)}s · ${turn.trace.tools_used.length} tool call(s)`
                            : undefined,
                          // Bound through a const: the enclosing guard proves this is present, but the
                          // narrowing does not survive into the callback.
                          explanation: turn.explanation as Explanation,
                        })
                      }
                    >
                      evidence
                    </button>
                  )}
                </div>

                {expanded === index && turn.trace && (
                  // The step list: where you find out that a tool returned nothing rather than that the
                  // model invented a figure.
                  <ol className="copilot-steps">
                    {turn.trace.steps.map((step) => (
                      <li key={step.index} className={step.ok ? "step-ok" : "step-failed"}>
                        <span className={`step-kind step-${step.kind}`}>{step.kind}</span>
                        {step.tool && <code className="step-tool">{step.tool}</code>}
                        <span className="step-detail">{step.detail}</span>
                        <span className="step-latency">{step.latency_ms.toFixed(0)}ms</span>
                      </li>
                    ))}
                  </ol>
                )}
              </>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {/* Kept after the first question. They used to vanish on the first ask, with no way back short of
          reloading the page — and they are the fastest way to demonstrate the copilot, so removing them
          removed the feature's own signposting. */}
      {turns.length > 0 && (
        <div className="copilot-suggestions copilot-suggestions-compact">
          {SUGGESTIONS.slice(0, 3).map((suggestion) => (
            <button key={suggestion} type="button" disabled={asking} onClick={() => void ask(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <form
        className="copilot-form"
        onSubmit={(event) => {
          event.preventDefault();
          void ask(question);
        }}
      >
        <input
          type="text"
          // Named, because an unnamed form control is a browser-reported issue and, more to the point,
          // password managers and autofill behave strangely around them.
          id="copilot-question"
          name="question"
          autoComplete="off"
          value={question}
          placeholder={asking ? "waiting for the model…" : "Ask about the site…"}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={asking}
          aria-label="Ask the copilot"
        />
        <button type="submit" disabled={asking || !question.trim()}>
          ask
        </button>
      </form>
    </div>
  );
}
