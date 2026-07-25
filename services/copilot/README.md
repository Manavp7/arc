# copilot (M13, M20)

Natural-language questions answered from the world model, with citations.

```
question ──► select tool ──► execute ──► synthesise ──► answer + Explanation
                  ▲             │
                  └─────────────┘  (bounded loop)
```

## The copilot is a client, not a privileged insider

Every tool goes through the same HTTP surface an external caller would use. Three consequences: the API
is exercised by the thing users actually drive, the MCP server can expose these same tools without a
second implementation, and a tool cannot reach data the API would not expose.

The one exception is `graph_query`, which uses the `GraphStore` port because multi-hop traversal is not a
REST shape — and it comes with the most important decision here.

**No raw Cypher, ever.** The obvious `graph_query` takes a query string from the model, and it is a
mistake dressed as flexibility: a language model composing graph queries against a live world model is an
injection vector with extra steps. `DETACH DELETE` is three tokens from a legitimate traversal, and the
model has no idea which of its outputs are destructive. So the tool takes a starting entity, relationship
types and a depth, and builds the traversal itself. **The model chooses what to ask; it never chooses how
to execute.**

## Model selection was a measurement, not a preference

`scripts/eval_tool_calling.py` scores candidate Ollama tags on **this** copilot's 25-question fixture over
**these** nine tools, and writes the result to [`docs/MODELS.md`](../../docs/MODELS.md). Measured here:

| model | selection | arguments | restraint | p50 | p95 |
|---|---|---|---|---|---|
| **`granite4:3b`** | **91 %** | 85 % | 100 % | 3,431 ms | 7,516 ms |
| `qwen3:1.7b` | 86 % | 58 % | 100 % | 2,201 ms | 8,478 ms |
| `llama3.2:3b` | 86 % | 84 % | **33 %** | 2,854 ms | 8,628 ms |
| `qwen2.5:3b` | 73 % | 75 % | 100 % | 2,964 ms | 9,068 ms |
| `qwen3:0.6b` | 68 % | 67 % | 100 % | 1,135 ms | 4,290 ms |
| `qwen2.5:1.5b` | 64 % | 57 % | 100 % | 1,524 ms | 5,639 ms |

`granite4:3b` is pinned as an **exact** tag — never `:latest`, because a floating tag means the model can
change under a deployment without anything in the repository changing, and the first symptom is a copilot
that has quietly started choosing the wrong tool.

Two things worth noting about that table. It disagrees with the published benchmarks that informed the
candidate list, which is the entire argument for running the eval: a general benchmark measures a general
task. And `llama3.2:3b` scores 33 % on **restraint** — it calls tools to say hello — which independently
reproduces the same finding in the public benchmark and is a good sign the harness is sound.

Restraint is scored separately because it is the axis small models fail worst and it damages the most: if
"hello" triggers a database query, nobody trusts the answer to a real question.

## The eval caught a safety bug, not just a scoring one

Asked *"There is a fire at dock 3. What should we do?"*, the pinned model chose **`run_simulation`** —
asked for advice about a fire, it wanted to start one. The tool description already said "only use when
the user explicitly asks to simulate or test something".

**Instructions a model can ignore are not controls.** So `run_simulation` — the only side-effecting tool —
now checks the question itself for an explicit what-if ("simulate", "what if", "drill", "inject") and
refuses otherwise. The refusal is returned as a *successful* result carrying an explanation, so the agent
goes on to answer the real question instead of treating it as an outage.

## Degradation is labelled, never hidden

Three layers stand between a weak model and a broken product, and each announces itself:

1. **Argument repair.** Arguments arriving as a JSON string, or wrapped in markdown fences, are parsed —
   both are common below 4 B. Anything unparseable is *dropped*, never guessed.
2. **A hallucinated tool name is never substituted.** A wrong tool run confidently produces a fluent
   answer about the wrong thing, which is the worst available outcome.
3. **A deterministic keyword router** answers when the model declines to select a tool at all. Every use
   is logged, counted, and stated in the answer's explanation.

The fallback does **not** fire when the model deliberately answers without a tool — a bug the restraint
tests caught, and one that would have made the copilot query the database to say hello, destroying the
restraint property on every model that gets it right.

When no tool returns usable data the copilot **refuses**: *"I could not answer that."* Inventing an answer
here is the single most damaging thing it can do, because the result is indistinguishable from a correct
one. Confidence is computed from what happened — evidence raises it, each degradation lowers it, no
evidence caps it at 0.1 however fluent the prose.

## Why not LangGraph

The PRD asks for a LangGraph agent; this is an explicit state machine, and the deviation is defensible.
LangGraph earns its place when a graph has branches, cycles and human interrupts a hand-rolled loop would
get wrong. This graph is five nodes and one bounded loop, and writing it out means the degraded path is
*visible* rather than hidden in a conditional edge, every step is recorded in `AgentTrace` for the M20
explanation, and a demo does not depend on a framework release. The `LLM` port is the seam that matters,
and it is already swappable.

The loop bound is load-bearing, not a safety net: a small model asked a vague question will call
`list_entities` forever, and an agent with no step budget is a denial-of-service against your own database.

## CI never depends on a model

`ScriptedLLM` answers **every** case in the eval set with no model at all — the user's explicit
requirement. It is not a mock: it makes the same *decisions* a working model makes, and builds its answers
from the actual tool results, so the graph, the tool layer, the argument coercion, the explanation builder
and the refusal paths are all genuinely exercised. Only token generation is replaced. A scripted answer
that ignored the tool output would let a broken tool pass the eval, which is exactly backwards.

## Endpoints

| | |
|---|---|
| `POST /copilot/ask` | answer a question, with explanation and full trace |
| `GET /copilot/tools` | the tools exactly as the model sees them |
| `GET /copilot/evalset` | the questions this copilot is held to |
| `POST /copilot/eval` | score the configured model live, against the fixture |
