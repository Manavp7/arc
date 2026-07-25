/**
 * The no-code workflow builder (PRD M22, Phase 6).
 *
 * A form, not a canvas. A drag-and-drop node graph is what "no-code builder" evokes, and it would be the wrong
 * thing to build first: the hard part of authoring a workflow is not arranging boxes, it is knowing which
 * activities exist and why the one you wrote will not run. A form that validates continuously and explains
 * itself answers those; a canvas answers neither, and takes ten times as long to build.
 *
 * Two decisions carry this panel:
 *
 * **The vocabulary comes from the server.** Activities, operators, severities and valid field paths are fetched,
 * never hard-coded. A hard-coded activity list is a UI offering steps the engine cannot run — the failure being a
 * workflow that looks fine in the browser and is rejected on save. Here the selects cannot express something
 * invalid in the first place.
 *
 * **Validation is server-side, on a debounce.** Re-implementing the rules in TypeScript would produce two
 * validators that disagree, and the browser's would be the one people trust. So the panel asks the engine, and
 * shows exactly what it said — including the execution order, which is the one thing an author cannot read off
 * their own JSON, because `after` makes it a DAG that gets topologically sorted.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";

interface Vocabulary {
  activities: string[];
  operators: string[];
  severities: string[];
  fields: string[];
  note: string;
}

interface Problem {
  where: string;
  message: string;
  fix: string | null;
}

interface ValidationResult {
  valid: boolean;
  problems: Problem[];
  execution_order?: string[];
  compensation_order?: string[];
}

interface StepDraft {
  id: string;
  activity: string;
  after: string[];
  compensate: string;
  optional: boolean;
}

interface ConditionDraft {
  field: string;
  op: string;
  value: string;
}

interface AuthoredWorkflow {
  name: string;
  description: string;
  trigger: { event_types: string[]; min_severity: string; cooldown_s: number };
  conditions: { field: string; op: string; value: unknown }[];
  steps: Record<string, unknown>[];
  enabled: boolean;
}

/** Event types worth offering as triggers. Free text is still allowed — a deployment may have its own. */
const COMMON_TRIGGERS = [
  "fire_detected",
  "smoke_detected",
  "unauthorized_entry",
  "dwell_exceeded",
  "temperature_exceeded",
  "crowd_gathering",
  "anomaly_detected",
  "spill_detected",
];

const BLANK_STEP: StepDraft = { id: "", activity: "", after: [], compensate: "", optional: false };

export function WorkflowBuilderPanel() {
  const [vocabulary, setVocabulary] = useState<Vocabulary | null>(null);
  const [saved, setSaved] = useState<AuthoredWorkflow[]>([]);
  const [rejected, setRejected] = useState<Problem[]>([]);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [triggers, setTriggers] = useState<string[]>([]);
  const [minSeverity, setMinSeverity] = useState("high");
  const [cooldown, setCooldown] = useState(300);
  const [conditions, setConditions] = useState<ConditionDraft[]>([]);
  const [steps, setSteps] = useState<StepDraft[]>([{ ...BLANK_STEP }]);

  const [result, setResult] = useState<ValidationResult | null>(null);
  const [status, setStatus] = useState<string>("");
  /**
   * Whether `status` is good news.
   *
   * Six of the eight messages this panel can set are failures — "could not load the vocabulary", "refused",
   * "could not delete" — and the first version rendered every one of them in the success green. An error
   * dressed as a success is worse than an unstyled one, because the colour is what people read first.
   */
  const [statusKind, setStatusKind] = useState<"ok" | "bad" | "busy">("ok");
  const [savedOrder, setSavedOrder] = useState<string[]>([]);

  /** The document as the engine will read it. */
  const document = useMemo(
    () => ({
      name,
      description,
      trigger: {
        event_types: triggers,
        min_severity: minSeverity,
        cooldown_s: cooldown,
      },
      conditions: conditions
        .filter((condition) => condition.field)
        .map((condition) => ({
          field: condition.field,
          op: condition.op,
          // `in` and `not_in` need a list, and a comma-separated box is how a person types one. Numbers are
          // coerced, because "60" typed into a `gt` box is meant as a number and a string comparison would
          // silently do the wrong thing.
          value:
            condition.op === "in" || condition.op === "not_in"
              ? condition.value
                  .split(",")
                  .map((part) => part.trim())
                  .filter(Boolean)
              : coerce(condition.value),
        })),
      steps: steps
        .filter((step) => step.id || step.activity)
        .map((step) => ({
          id: step.id,
          activity: step.activity,
          ...(step.after.length ? { after: step.after } : {}),
          ...(step.compensate ? { compensate: step.compensate } : {}),
          ...(step.optional ? { optional: true } : {}),
        })),
    }),
    [name, description, triggers, minSeverity, cooldown, conditions, steps],
  );

  useEffect(() => {
    void (async () => {
      try {
        setVocabulary(await api.workflowVocabulary());
      } catch (error) {
        setStatus(`Could not load the vocabulary: ${describe(error)}`);
        setStatusKind("bad");
      }
      await refresh();
    })();
  }, []);

  const refresh = useCallback(async () => {
    try {
      const payload = await api.authoredWorkflows();
      setSaved(payload.workflows as AuthoredWorkflow[]);
      // Rejected files are shown, not swallowed. A workflow that failed to load is otherwise
      // indistinguishable from one that never fires, and the author will assume the latter.
      setRejected(payload.rejected as Problem[]);
    } catch (error) {
      setStatus(`Could not list workflows: ${describe(error)}`);
      setStatusKind("bad");
    }
  }, []);

  // Validate on a debounce, server-side. Re-implementing the rules here would produce two validators that
  // disagree, and this one would be the one people trust.
  useEffect(() => {
    if (!name && steps.every((step) => !step.activity)) {
      setResult(null);
      return;
    }
    const timer = setTimeout(() => {
      void (async () => {
        try {
          setResult(await api.validateWorkflow(document));
        } catch (error) {
          setStatus(`Validation unavailable: ${describe(error)}`);
          setStatusKind("bad");
        }
      })();
    }, 400);
    return () => clearTimeout(timer);
  }, [document, name, steps]);

  // A save outcome describes the document as it was when the button was pressed. The moment the author edits
  // anything it is stale, and a refusal that lingers while they fix the cause of it is actively misleading.
  useEffect(() => {
    setStatus("");
    setSavedOrder([]);
  }, [document]);

  const problemsFor = (where: string) =>
    (result?.problems ?? []).filter((problem) => problem.where.startsWith(where));

  async function save() {
    if (!result?.valid) return;
    setStatus("Saving…");
    setStatusKind("busy");
    setSavedOrder([]);
    try {
      const payload = await api.saveWorkflow(name, document);
      setSavedOrder(payload.execution_order as string[]);
      setStatus("Saved and armed.");
      setStatusKind("ok");
      await refresh();
    } catch (error) {
      setStatus(`Refused: ${describe(error)}`);
      setStatusKind("bad");
    }
  }

  async function remove(workflowName: string) {
    try {
      await api.deleteWorkflow(workflowName);
      setStatus(`Deleted ${workflowName}.`);
      setStatusKind("ok");
      setSavedOrder([]);
      await refresh();
    } catch (error) {
      setStatus(`Could not delete: ${describe(error)}`);
      setStatusKind("bad");
    }
  }

  function loadForEditing(workflow: AuthoredWorkflow) {
    setName(workflow.name);
    setDescription(workflow.description);
    setTriggers(workflow.trigger.event_types);
    setMinSeverity(workflow.trigger.min_severity);
    setCooldown(workflow.trigger.cooldown_s);
    setConditions(
      workflow.conditions.map((condition) => ({
        field: condition.field,
        op: condition.op,
        value: Array.isArray(condition.value)
          ? condition.value.join(", ")
          : String(condition.value ?? ""),
      })),
    );
    setSteps(
      workflow.steps.map((step) => ({
        id: String(step.id ?? ""),
        activity: String(step.activity ?? ""),
        after: (step.after as string[]) ?? [],
        compensate: String(step.compensate ?? ""),
        optional: Boolean(step.optional),
      })),
    );
  }

  const stepIds = steps.map((step) => step.id).filter(Boolean);

  /**
   * What the sticky bar says, in priority order.
   *
   * A failed save outranks the validity summary. The two can disagree perfectly legitimately — a document can be
   * structurally valid and still be refused by the server, for instance because its name collides with a code
   * playbook — and when they do, the refusal is the fact the author needs. Showing "Valid" instead is how a
   * rejected save comes to look like a successful one.
   */
  const [barText, barTone, barTitle] = ((): [string, string, string] => {
    if (statusKind === "bad" && status) {
      // Truncated to one line by CSS; the full text is in the title and in section 4.
      return [status, "verdict-bar-bad", status];
    }
    if (statusKind === "busy" && status) return [status, "verdict-bar-busy", status];
    if (statusKind === "ok" && status) {
      // Successes take priority too, and the reason is symmetry: the first version gave failures priority and
      // left successes to a message 200px below the fold, so pressing the bar's own button produced no change in
      // the bar. The author's natural response to a button that does not react is to press it again. Any outcome
      // clears on the next edit, so this cannot go stale.
      // No leading space: `status` already ends in a full stop, and adding one produced `armed.  ·` with a
      // double space — inconsistent with the `Valid ·` variant beside it.
      const suffix = savedOrder.length ? `· ${savedOrder.join(" → ")}` : "";
      return [`${status} ${suffix}`.trim(), "verdict-bar-ok", `${status} ${suffix}`.trim()];
    }
    if (result === null) {
      // Dim, not red. An untouched form is not an error, and greeting somebody with the danger colour before
      // they have done anything is how a colour stops meaning anything.
      return ["Add a name and a step", "verdict-bar-busy", "Nothing to validate yet"];
    }
    if (result.valid) {
      const order = (result.execution_order ?? []).join(" → ");
      return [`Valid · ${order}`, "verdict-bar-ok", `Steps will run: ${order}`];
    }
    const count = result.problems.length;
    return [
      `${count} problem${count === 1 ? "" : "s"}`,
      "verdict-bar-bad",
      result.problems.map((problem) => `${problem.where}: ${problem.message}`).join("\n"),
    ];
  })();

  return (
    <div className="panel workflow-builder">
      <header className="panel-header">
        <h2>Workflow builder</h2>
        <p className="hint">
          Compose the activities the platform already has into a new response, without a deploy. Adding a{" "}
          <em>new</em> activity is a code change — dispatching a drone is code.
        </p>
      </header>

      {/* ---------------------------------------------------------------- trigger */}
      <section className="builder-section">
        <h3>1 · When should this run?</h3>
        <label>
          Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="FuelSpillResponse"
          />
        </label>
        <Problems problems={problemsFor("name")} />

        <label>
          What it does
          <input
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Contain a spill in the fuel store and record it"
          />
        </label>

        <fieldset className="trigger-types">
          <legend>Triggered by</legend>
          {COMMON_TRIGGERS.map((eventType) => (
            <label key={eventType} className="checkbox">
              <input
                type="checkbox"
                checked={triggers.includes(eventType)}
                onChange={(event) =>
                  setTriggers(
                    event.target.checked
                      ? [...triggers, eventType]
                      : triggers.filter((item) => item !== eventType),
                  )
                }
              />
              {eventType}
            </label>
          ))}
        </fieldset>
        <Problems problems={problemsFor("trigger.event_types")} />

        <div className="builder-row">
          <label>
            At least this severe
            <select value={minSeverity} onChange={(event) => setMinSeverity(event.target.value)}>
              {(vocabulary?.severities ?? ["high"]).map((severity) => (
                <option key={severity} value={severity}>
                  {severity}
                </option>
              ))}
            </select>
          </label>
          <label>
            Cooldown (seconds)
            <input
              type="number"
              min={0}
              value={cooldown}
              onChange={(event) => setCooldown(Number(event.target.value))}
            />
            {/* Explained inline, because a cooldown of zero looks like "no restriction" rather than the
                hazard it is: a fire produces its detection on nearly every frame while it burns. */}
            <small>
              A fire fires its detection on nearly every frame. Without a cooldown that is one run per frame,
              each dispatching the same drone.
            </small>
          </label>
        </div>
        <Problems problems={problemsFor("trigger.cooldown_s")} />
      </section>

      {/* ------------------------------------------------------------- conditions */}
      <section className="builder-section">
        <h3>2 · Only when… (optional)</h3>
        {conditions.map((condition, index) => (
          <div className="builder-row condition-row" key={index}>
            {/* Labelled, not placeholder-only. Three anonymous boxes in a row is a form somebody has to
                reverse-engineer, and a placeholder disappears the moment they start typing. */}
            <label>
              Field
              <input
                list="workflow-fields"
                value={condition.field}
                onChange={(event) => updateAt(setConditions, conditions, index, { field: event.target.value })}
                placeholder="payload.zone_id"
              />
            </label>
            <label>
              Test
              <select
                value={condition.op}
                onChange={(event) => updateAt(setConditions, conditions, index, { op: event.target.value })}
              >
                {(vocabulary?.operators ?? []).map((operator) => (
                  <option key={operator} value={operator}>
                    {operator}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Value
              <input
                value={condition.value}
                onChange={(event) => updateAt(setConditions, conditions, index, { value: event.target.value })}
                placeholder={condition.op === "in" ? "fuel_store, dock_1" : "value"}
                disabled={condition.op === "exists"}
              />
            </label>
            <button
              className="ghost danger"
              onClick={() => setConditions(conditions.filter((_, at) => at !== index))}
            >
              remove
            </button>
            <Problems problems={problemsFor(`conditions[${index}]`)} />
          </div>
        ))}
        <datalist id="workflow-fields">
          {(vocabulary?.fields ?? []).map((field) => (
            <option key={field} value={field} />
          ))}
        </datalist>
        <button className="ghost" onClick={() => setConditions([...conditions, { field: "", op: "eq", value: "" }])}>
          + condition
        </button>
      </section>

      {/* ------------------------------------------------------------------ steps */}
      <section className="builder-section">
        <h3>3 · What should happen?</h3>
        {steps.map((step, index) => (
          <div className="step-card" key={index}>
            <div className="builder-row">
              <label className="step-id-field">
                Step id
                <input
                  value={step.id}
                  onChange={(event) => updateAt(setSteps, steps, index, { id: event.target.value })}
                  placeholder="notify"
                />
              </label>
              {/* A wide basis, because the activity is the most important fact in a step card and equal
                  columns truncated it to `notify_securit▾`. */}
              <label className="step-activity-field">
                Activity
                <select
                  value={step.activity}
                  onChange={(event) => updateAt(setSteps, steps, index, { activity: event.target.value })}
                >
                  <option value="">choose…</option>
                  {(vocabulary?.activities ?? []).map((activity) => (
                    <option key={activity} value={activity}>
                      {activity}
                    </option>
                  ))}
                </select>
              </label>
              {/* Its own full-width line: the helper below wrapped to three lines inside a 100px column and
                  left an L-shaped hole of dead space under the two fields beside it. */}
              <label className="step-undo-field">
                Undo with
                <select
                  value={step.compensate}
                  onChange={(event) => updateAt(setSteps, steps, index, { compensate: event.target.value })}
                >
                  <option value="">nothing to undo</option>
                  {(vocabulary?.activities ?? []).map((activity) => (
                    <option key={activity} value={activity}>
                      {activity}
                    </option>
                  ))}
                </select>
                <small>Run in reverse order if a later step fails.</small>
              </label>
            </div>
            <div className="builder-row">
              {/* A fieldset, not a label. A `<label>` containing nested `<label>`s is invalid HTML, and it
                  meant clicking the word "After" toggled the first checkbox — the browser associating the
                  outer label with the first control inside it. */}
              <fieldset className="after-picker trigger-types">
                <legend>After</legend>
                {/* Deduplicated. Two steps sharing an id produced two checkboxes with the same key and the
                    same label, so ticking either ticked both — and React logged a duplicate-key error. The
                    validator refuses to save such a workflow, but the editor should not render nonsense while
                    somebody is on their way to fixing it. */}
                {Array.from(new Set(stepIds))
                  .filter((identifier) => identifier !== step.id)
                  .map((identifier) => (
                    <label key={identifier} className="checkbox">
                      <input
                        type="checkbox"
                        checked={step.after.includes(identifier)}
                        onChange={(event) =>
                          updateAt(setSteps, steps, index, {
                            after: event.target.checked
                              ? [...step.after, identifier]
                              : step.after.filter((item) => item !== identifier),
                          })
                        }
                      />
                      {identifier}
                    </label>
                  ))}
                {stepIds.length < 2 && <small>Add another step to order them.</small>}
              </fieldset>
            </div>
            <div className="builder-row step-flags">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={step.optional}
                  onChange={(event) => updateAt(setSteps, steps, index, { optional: event.target.checked })}
                />
                optional — a failure here does not roll the run back
              </label>
              <button className="ghost danger" onClick={() => setSteps(steps.filter((_, at) => at !== index))}>
                remove step
              </button>
            </div>
            <Problems problems={problemsFor(`steps[${index}]`)} />
          </div>
        ))}
        <button className="ghost" onClick={() => setSteps([...steps, { ...BLANK_STEP }])}>
          + step
        </button>
        <Problems problems={problemsFor("steps.after")} />
        {problemsFor("steps").filter((problem) => problem.where === "steps").length > 0 && (
          <Problems problems={problemsFor("steps").filter((problem) => problem.where === "steps")} />
        )}
      </section>

      {/* ----------------------------------------------------------------- verdict */}
      <section className="builder-section verdict-detail">
        <h3>4 · Verdict</h3>
        {result === null && <p className="hint">Fill in a name and a step to see what the engine thinks.</p>}
        {result?.valid && (
          <div className="verdict-ok">
            <strong>Valid.</strong>
            {/* The execution order is the point of showing a verdict at all: `after` makes this a DAG, and the
                engine runs it topologically sorted, so the steps may not run in the order they were typed. */}
            <p>
              Steps will run: <code>{(result.execution_order ?? []).join(" → ")}</code>
            </p>
            {(result.compensation_order ?? []).length > 0 && (
              <p>
                If a later step fails, these are undone in this order:{" "}
                <code>{(result.compensation_order ?? []).join(" → ")}</code>
              </p>
            )}
          </div>
        )}
        {result && !result.valid && (
          <div className="verdict-bad">
            <strong>
              {result.problems.length} problem{result.problems.length === 1 ? "" : "s"}
            </strong>
            <Problems problems={result.problems} />
          </div>
        )}
        {status && (
          <p className={`status status-${statusKind}`}>
            {status}
            {/* The order in the same monospace the verdict uses two lines above, rather than plain text —
                the same value should not be typeset two different ways on one screen. */}
            {savedOrder.length > 0 && (
              <>
                {" "}
                Steps will run: <code>{savedOrder.join(" → ")}</code>
              </>
            )}
          </p>
        )}
      </section>

      {/* ------------------------------------------------------------------ saved */}
      <section className="builder-section">
        <h3>Armed workflows</h3>
        {saved.length === 0 && <p className="hint">None yet. The code playbooks are unaffected.</p>}
        <ul className="workflow-list">
          {saved.map((workflow) => (
            <li key={workflow.name}>
              <div>
                <strong>{workflow.name}</strong>
                <span className="muted workflow-meta">
                  {workflow.trigger.event_types.join(", ")} · {workflow.steps.length} steps ·{" "}
                  {workflow.trigger.cooldown_s}s cooldown
                </span>
                {workflow.description && <p className="muted">{workflow.description}</p>}
              </div>
              <div>
                <button className="ghost" onClick={() => loadForEditing(workflow)}>
                  edit
                </button>
                <button className="ghost danger" onClick={() => void remove(workflow.name)}>
                  delete
                </button>
              </div>
            </li>
          ))}
        </ul>

        {rejected.length > 0 && (
          <div className="verdict-bad">
            <strong>{rejected.length} file(s) on disk were rejected and are NOT armed</strong>
            <Problems problems={rejected} />
          </div>
        )}
      </section>

      {vocabulary?.note && <p className="hint footnote">{vocabulary.note}</p>}

      {/* A sticky one-LINE summary, not a sticky panel.
          The verdict section above grows with the problem list, and pinning it meant a mid-edit state with five
          problems became a 500px bar covering 56% of the rail — including the step cards its own messages were
          describing, which scrolling could not uncover because the bar was pinned. One line cannot grow, so the
          CTA stays reachable while the form stays visible.

          The bar carries the SAVE OUTCOME too, and that is not decoration. Moving the detail out of the bar left
          the outcome message down in section 4, so a rejected save produced a red "Refused: …" 134px below the
          fold while the bar still read "Valid" in green with the CTA enabled — the panel looked like the save had
          worked. For a builder whose purpose is to arm an automated emergency response, a silent rejection is the
          one failure mode worth holding a release for. The bar is the only always-visible element, so it has to
          be the thing that says "this did not save". */}
      <div className="verdict-bar">
        <span className={`verdict-bar-label ${barTone}`} title={barTitle}>
          {barText}
        </span>
        <button className="primary" disabled={!result?.valid} onClick={() => void save()}>
          Save and arm
        </button>
      </div>
    </div>
  );
}

function Problems({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null;
  return (
    <ul className="problems">
      {problems.map((problem, index) => (
        <li key={index}>
          <code>{problem.where}</code> {problem.message}
          {/* The fix is shown, not hidden behind a tooltip. It is the half of the message that saves time. */}
          {problem.fix && <div className="problem-fix">{problem.fix}</div>}
        </li>
      ))}
    </ul>
  );
}

function updateAt<T>(
  setter: (next: T[]) => void,
  current: T[],
  index: number,
  patch: Partial<T>,
): void {
  setter(current.map((item, at) => (at === index ? { ...item, ...patch } : item)));
}

/** Numbers typed into a comparison box are meant as numbers; a string compare would silently do the wrong thing. */
function coerce(raw: string): unknown {
  if (raw === "") return null;
  if (raw === "true") return true;
  if (raw === "false") return false;
  const asNumber = Number(raw);
  return Number.isNaN(asNumber) ? raw : asNumber;
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
