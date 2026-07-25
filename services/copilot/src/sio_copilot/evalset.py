"""The evaluation set: what the copilot must be able to answer, and what a model must be able to route.

Two things live here, and keeping them in one file is the point:

* `EVAL_CASES` — the questions, each with the tool a competent model should choose. This is the fixture
  `scripts/eval_tool_calling.py` scores candidate Ollama tags against, and the same list drives the
  scripted end-to-end tests. One list, so a model is measured on exactly what the product needs rather
  than on a generic benchmark.
* `scripted_routes()` — the recorded decisions that let `ScriptedLLM` answer **every case** with no model
  at all, which is what keeps CI independent of any model (the user's explicit requirement).

The cases are drawn from the PRD's use cases UC1-UC4 plus the questions an operator actually asks, and
several are deliberately hard: two of them should result in *no* tool call, because a model that calls a
tool for "hello" is a model that will call tools for everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sio_core.llm import Route, ToolCall


@dataclass(frozen=True)
class EvalCase:
    """One question, and what a correct response looks like."""

    id: str
    question: str
    expect_tool: str | None
    """The tool a competent model should choose, or None when it should not call one at all."""
    expect_arguments: dict[str, Any] = field(default_factory=dict)
    """Arguments that must be present and correct. Extra arguments are allowed."""
    acceptable_tools: tuple[str, ...] = ()
    """Other defensible choices. Scored as correct-but-not-preferred.

    Real questions often have two reasonable answers — "which camera saw the truck" is defensible via
    `graph_query` or `describe_entity` — and a fixture that insists on one would punish a model for a
    judgement a human would accept.
    """
    use_case: str = ""
    note: str = ""


EVAL_CASES: tuple[EvalCase, ...] = (
    # --- UC1: what is happening right now -----------------------------------------
    EvalCase(
        id="count_trucks",
        question="How many trucks are on site right now?",
        expect_tool="list_entities",
        expect_arguments={"entity_type": "truck"},
        use_case="UC1",
    ),
    EvalCase(
        id="count_people",
        question="How many people are in the yard?",
        expect_tool="list_entities",
        acceptable_tools=("spatial_query",),
        expect_arguments={"entity_type": "person"},
        use_case="UC1",
    ),
    EvalCase(
        id="whats_on_site",
        question="What is on site at the moment?",
        expect_tool="list_entities",
        use_case="UC1",
    ),
    EvalCase(
        id="anything_unusual",
        question="Is anything unusual happening?",
        expect_tool="timeline_replay",
        acceptable_tools=("list_entities",),
        use_case="UC1",
        note="Recent events are the honest source for 'unusual'.",
    ),
    # --- UC2: incident awareness ---------------------------------------------------
    EvalCase(
        id="fire_check",
        question="Has any camera seen smoke or fire?",
        expect_tool="semantic_search",
        expect_arguments={"query": "smoke"},
        acceptable_tools=("timeline_replay",),
        use_case="UC2",
    ),
    EvalCase(
        id="temperature_now",
        question="What is the temperature in the warehouse?",
        expect_tool="timeseries_query",
        expect_arguments={"metric": "temperature_c"},
        use_case="UC2",
    ),
    EvalCase(
        id="what_to_do_fire",
        question="There is a fire at dock 3. What should we do?",
        expect_tool="propose_decision",
        acceptable_tools=("spatial_query", "timeline_replay"),
        use_case="UC2",
    ),
    EvalCase(
        id="simulate_fire",
        question="Simulate a fire at dock 3 so we can test the response.",
        expect_tool="run_simulation",
        expect_arguments={"scenario": "fire", "zone_id": "dock_3"},
        use_case="UC2",
    ),
    # --- UC3: which camera saw what ------------------------------------------------
    EvalCase(
        id="camera_last_saw",
        question="Which camera last saw entity ent_01ABC?",
        expect_tool="graph_query",
        expect_arguments={"entity_id": "ent_01ABC"},
        acceptable_tools=("describe_entity",),
        use_case="UC3",
    ),
    EvalCase(
        id="cameras_covering_gate",
        question="Which cameras cover gate_a?",
        expect_tool="spatial_query",
        expect_arguments={"question": "cameras_covering", "zone_id": "gate_a"},
        use_case="UC3",
    ),
    EvalCase(
        id="blind_spots",
        question="Where on the site do we have no camera coverage?",
        expect_tool="spatial_query",
        expect_arguments={"question": "blind_spots"},
        use_case="UC3",
    ),
    EvalCase(
        id="describe_entity",
        question="Tell me everything about ent_01XYZ.",
        expect_tool="describe_entity",
        expect_arguments={"entity_id": "ent_01XYZ"},
        use_case="UC3",
    ),
    EvalCase(
        id="show_footage",
        question="Show me footage of a truck at a loading dock.",
        expect_tool="semantic_search",
        expect_arguments={"query": "truck at a loading dock"},
        use_case="UC3",
    ),
    # --- UC4: prediction and planning ----------------------------------------------
    EvalCase(
        id="forecast_occupancy",
        question="How busy will the dock apron be in fifteen minutes?",
        expect_tool="timeseries_query",
        expect_arguments={"forecast": True},
        acceptable_tools=("spatial_query",),
        use_case="UC4",
    ),
    EvalCase(
        id="drone_battery",
        question="Will the drone need to return to base soon?",
        expect_tool="timeseries_query",
        expect_arguments={"metric": "battery_pct"},
        use_case="UC4",
    ),
    EvalCase(
        id="congestion",
        question="Is the yard going to get congested?",
        expect_tool="timeseries_query",
        acceptable_tools=("spatial_query", "list_entities"),
        use_case="UC4",
    ),
    # --- UC5: the past --------------------------------------------------------------
    EvalCase(
        id="ten_minutes_ago",
        question="What did the site look like ten minutes ago?",
        expect_tool="timeline_replay",
        expect_arguments={"minutes_ago": 10},
        use_case="UC5",
    ),
    EvalCase(
        id="what_happened",
        question="What happened in the last five minutes?",
        expect_tool="timeline_replay",
        use_case="UC5",
    ),
    # --- spatial ---------------------------------------------------------------------
    EvalCase(
        id="whats_in_dock3",
        question="What is in dock_3?",
        expect_tool="spatial_query",
        expect_arguments={"question": "in_zone", "zone_id": "dock_3"},
        acceptable_tools=("list_entities",),
    ),
    EvalCase(
        id="trucks_within_500m",
        question="Which trucks are within 500 metres of the gate?",
        expect_tool="spatial_query",
        acceptable_tools=("list_entities",),
        expect_arguments={},
    ),
    EvalCase(
        id="forklifts",
        question="Where are the forklifts?",
        expect_tool="list_entities",
        expect_arguments={"entity_type": "forklift"},
    ),
    EvalCase(
        id="speeding",
        question="Has anything been speeding?",
        expect_tool="timeline_replay",
        acceptable_tools=("list_entities",),
    ),
    # --- restraint: a model that calls a tool for these calls tools for everything ----
    EvalCase(
        id="greeting",
        question="Hello.",
        expect_tool=None,
        note="Restraint. No tool is the correct answer.",
    ),
    EvalCase(
        id="capabilities",
        question="What can you help me with?",
        expect_tool=None,
        note="Restraint. Answerable from the system prompt alone.",
    ),
    EvalCase(
        id="thanks",
        question="Thanks, that is all.",
        expect_tool=None,
        note="Restraint.",
    ),
)


#: Words that name an entity type, and the type they name.
#:
#: Plurals and the obvious synonyms, because "how many lorries" and "where are the staff" are the same question
#: as "trucks" and "people". Ordered longest-first at match time so "forklift" is not swallowed by a substring.
_TYPE_WORDS: dict[str, str] = {
    "truck": "truck",
    "trucks": "truck",
    "lorry": "truck",
    "lorries": "truck",
    "hgv": "truck",
    "person": "person",
    "people": "person",
    "worker": "person",
    "workers": "person",
    "staff": "person",
    "pedestrian": "person",
    "pedestrians": "person",
    "forklift": "forklift",
    "forklifts": "forklift",
    "drone": "drone",
    "drones": "drone",
    "vehicle": "vehicle",
    "vehicles": "vehicle",
}


def _entity_filter(question: str) -> dict[str, Any]:
    """Pull an entity type, and a zone, out of the question.

    Word-boundary matching rather than `in`, because "person" is a substring of "personnel" and — more
    awkwardly — "van" is a substring of "advance". A substring match here produces a filter nobody asked for,
    which is worse than no filter at all: the answer is confidently about the wrong subset.
    """
    lowered = question.lower()
    found: dict[str, Any] = {}
    for word, entity_type in _TYPE_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            found["entity_type"] = entity_type
            break
    zone = re.search(
        r"\b(dock[_ ]?\d+|gate[_ ][ab]|fuel[_ ]store|lane[_ ](?:north|south)|yard)\b", lowered
    )
    if zone:
        # Normalised to the id form the world model uses. "dock 3" and "dock_3" are the same place, and a
        # filter carrying the prose form silently matches nothing.
        found["zone_id"] = zone.group(1).replace(" ", "_")
    return found


def _search_query(question: str) -> dict[str, Any]:
    """Use the question as the semantic query, minus the words that are asking rather than describing.

    "Show me footage of a truck at a loading dock" should search for "truck at a loading dock", not for the
    whole sentence — the leading request words are noise in an embedding space, and they are the same noise in
    every question, so they pull every query toward the same point.
    """
    lowered = question.lower().strip().rstrip("?.")
    for prefix in (
        "show me footage of",
        "show me the footage of",
        "show me footage",
        "show me",
        "find footage of",
        "find me",
        "what did",
        "search for",
    ):
        if lowered.startswith(prefix):
            remainder = lowered[len(prefix) :].strip()
            if remainder:
                return {"query": remainder}
    return {}


def scripted_routes() -> list[Route]:
    """Recorded decisions covering **every** eval case, so CI never needs a model.

    Ordered most-specific first, because matching is first-wins and the broad routes would otherwise
    swallow the narrow ones. The answers are built from the tool results where possible, so a broken tool
    fails the test rather than passing on a canned string.
    """
    return [
        Route(
            intent="simulate",
            patterns=(r"\bsimulate\b", r"\btest the response\b"),
            tool_calls=[
                ToolCall(name="run_simulation", arguments={"scenario": "fire", "zone_id": "dock_3"})
            ],
            answer="Injected a simulated fire at dock_3. This affects the simulated site only.",
        ),
        Route(
            intent="what should we do",
            patterns=(r"what should we do", r"what do you recommend", r"how should we respond"),
            tool_calls=[
                ToolCall(
                    name="propose_decision",
                    arguments={
                        "summary": "Dispatch the patrol drone to dock_3 and notify security",
                        "rationale": "A fire was reported at dock_3; visual confirmation and a human "
                        "responder are both needed before anything is closed.",
                    },
                )
            ],
            answer=(
                "I have recorded a proposal to dispatch the patrol drone to dock_3 and notify security. "
                "Nothing has been executed — it is waiting for your approval."
            ),
        ),
        Route(
            intent="blind spots",
            patterns=(r"blind spot", r"no camera coverage", r"not covered"),
            tool_calls=[ToolCall(name="spatial_query", arguments={"question": "blind_spots"})],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"About {data.get('coverage_fraction', 0) * 100:.0f}% of the site is covered by "
                    f"cameras, leaving {data.get('uncovered_m2', 0):,.0f} m2 with no coverage."
                ),
            ),
        ),
        Route(
            intent="cameras covering",
            patterns=(r"cameras? (cover|watch|see|view)", r"which cameras"),
            tool_calls=[
                ToolCall(
                    name="spatial_query",
                    arguments={"question": "cameras_covering", "zone_id": "gate_a"},
                )
            ],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    "gate_a is covered by "
                    + (
                        ", ".join(camera["source_id"] for camera in data.get("cameras", []))
                        or "no cameras"
                    )
                    + "."
                ),
            ),
        ),
        Route(
            intent="camera last saw",
            patterns=(r"which camera last saw", r"last saw (entity )?ent_", r"who saw"),
            tool_calls=[ToolCall(name="graph_query", arguments={"entity_id": "ent_01ABC"})],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"{data.get('entity_id')} has {data.get('edge_count', 0)} relationship(s); "
                    f"most recently {(data.get('most_recent') or {}).get('to', 'nothing recorded')}."
                ),
            ),
        ),
        Route(
            intent="describe entity",
            patterns=(r"everything about", r"tell me about ent_", r"describe ent_"),
            tool_calls=[ToolCall(name="describe_entity", arguments={"entity_id": "ent_01XYZ"})],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"{data.get('label') or data.get('entity_id')} is a {data.get('type')} last seen "
                    f"{data.get('last_seen')}, corroborated by {len(data.get('sensors') or [])} sensor(s)."
                ),
            ),
        ),
        Route(
            intent="frame search",
            patterns=(r"show me", r"footage", r"smoke", r"fire", r"looked like", r"picture"),
            tool_calls=[ToolCall(name="semantic_search", arguments={"query": "smoke"})],
            # The question itself, not a hard-coded "smoke". The eval caught this: "Show me footage of a truck
            # at a loading dock" searched for smoke and returned fire frames — the right tool answering a
            # completely different question, fluently.
            arguments_from=_search_query,
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"The closest recorded frames to {data.get('query')!r} are "
                    + (
                        ", ".join(
                            f"{match['source_id']} (score {match['score']:.2f})"
                            for match in data.get("matches", [])[:3]
                        )
                        or "none"
                    )
                    + "."
                ),
            ),
        ),
        Route(
            intent="temperature",
            patterns=(r"temperature", r"how hot", r"degrees"),
            tool_calls=[ToolCall(name="timeseries_query", arguments={"metric": "temperature_c"})],
            answer_from=lambda results: _from_json(
                results,
                lambda data: _forecast_sentence(data, "temperature"),
            ),
        ),
        Route(
            intent="battery",
            patterns=(r"battery", r"return to base", r"charge"),
            tool_calls=[ToolCall(name="timeseries_query", arguments={"metric": "battery_pct"})],
            answer_from=lambda results: _from_json(
                results, lambda data: _forecast_sentence(data, "battery")
            ),
        ),
        Route(
            intent="forecast",
            patterns=(r"how busy", r"congest", r"will .* be", r"forecast", r"predict"),
            tool_calls=[
                ToolCall(
                    name="timeseries_query", arguments={"metric": "occupancy", "forecast": True}
                )
            ],
            answer_from=lambda results: _from_json(
                results, lambda data: _forecast_sentence(data, "occupancy")
            ),
        ),
        Route(
            intent="replay",
            patterns=(
                r"minutes ago",
                r"what happened",
                r"earlier",
                r"look like .* ago",
                r"speeding",
            ),
            tool_calls=[ToolCall(name="timeline_replay", arguments={"minutes_ago": 10})],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"{(data.get('counts') or {}).get('movers', 0)} moving entities were on site "
                    f"{data.get('minutes_ago')} minutes ago, and "
                    f"{len(data.get('events') or [])} event(s) were recorded around then."
                ),
            ),
        ),
        Route(
            intent="unusual",
            patterns=(r"unusual", r"anything wrong", r"anomal"),
            tool_calls=[
                ToolCall(name="timeline_replay", arguments={"minutes_ago": 2, "window_minutes": 5})
            ],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    (
                        "Recent events: "
                        + "; ".join(
                            f"{event['type']} ({event['severity']})"
                            for event in (data.get("events") or [])[:4]
                        )
                    )
                    if data.get("events")
                    else "Nothing unusual in the recent record."
                ),
            ),
        ),
        Route(
            intent="in zone",
            patterns=(r"what is in ", r"what's in ", r"in dock", r"which zone"),
            tool_calls=[
                ToolCall(
                    name="spatial_query", arguments={"question": "in_zone", "zone_id": "dock_3"}
                )
            ],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"dock_3 currently holds {len(data.get('confirmed') or [])} confirmed occupant(s)."
                ),
            ),
        ),
        Route(
            intent="within radius",
            patterns=(r"within \d+", r"metres of", r"meters of", r"near the"),
            tool_calls=[
                ToolCall(
                    name="spatial_query",
                    arguments={
                        "question": "within_radius",
                        "radius_m": 500,
                        "entity_type": "truck",
                    },
                )
            ],
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"{data.get('count', 0)} matched within {data.get('radius_m', 0):.0f} m."
                ),
            ),
        ),
        # Restraint routes: no tool, just an answer. A copilot that queries the database to say hello is
        # one nobody trusts with a real question.
        Route(
            intent="greeting",
            # ANCHORED AND COMPLETE. "^hello" alone matched "Hello, how many trucks are on site?" and
            # answered it as a greeting — the same trap the production restraint guard avoids by requiring
            # the absence of site vocabulary. A fixture that mis-routes is worse than no fixture, because
            # it passes.
            patterns=(
                r"^\s*(hello|hi|hey|yo|howdy)[\s.!]*$",
                r"^\s*good (morning|afternoon|evening)[\s.!]*$",
            ),
            answer="Hello. Ask me about what is on site, what happened earlier, or what is forecast.",
        ),
        Route(
            intent="capabilities",
            patterns=(r"what can you", r"help me with", r"what do you do"),
            answer=(
                "I can tell you what is on site now, reconstruct any past moment, search recorded camera "
                "frames, answer spatial questions such as camera coverage and blind spots, report "
                "forecasts with their intervals, and record a proposed action for you to approve."
            ),
        ),
        Route(
            intent="thanks",
            # Also anchored: "thanks — and what happened earlier?" is a question with a courtesy attached.
            patterns=(
                r"^\s*(thanks|thank you|cheers)[\s.!,]*$",
                r"^\s*(thanks|thank you)[\s,]*that('s| is) (all|it|everything)[\s.!]*$",
                r"^\s*that('s| is) (all|it|everything)[\s.!]*$",
            ),
            answer="Any time.",
        ),
        # Broadest last.
        Route(
            intent="list entities",
            patterns=(r"how many", r"on site", r"where are", r"currently", r"right now", r".*"),
            tool_calls=[ToolCall(name="list_entities", arguments={})],
            # Static `{}` meant "How many trucks are on site?" listed EVERYTHING. Tool selection scored 95%
            # while argument accuracy scored 71%, and this route was most of the gap — a wrong answer given
            # confidently, which is the failure mode this platform spends most of its effort avoiding.
            arguments_from=_entity_filter,
            answer_from=lambda results: _from_json(
                results,
                lambda data: (
                    f"{data.get('count', 0)} entities are on site: "
                    + (
                        ", ".join(
                            f"{count} {name}" for name, count in (data.get("by_type") or {}).items()
                        )
                        or "none"
                    )
                    + "."
                ),
            ),
        ),
    ]


def _from_json(results: list[dict[str, Any]], render: Any) -> str:
    """Render an answer from the first tool result that parses.

    Falling back to a plain description rather than raising: the scripted adapter must never be the reason
    a test fails, or it stops being a reliable baseline.
    """
    import json

    for result in results:
        try:
            data = json.loads(result.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "error" in data:
            return f"A tool failed: {data['error']}"
        try:
            return render(data)
        except Exception:
            return f"Result: {str(data)[:200]}"
    return "No usable tool result."


def _forecast_sentence(data: dict[str, Any], label: str) -> str:
    forecasts = data.get("forecasts") or {}
    if forecasts:
        key, first = next(iter(forecasts.items()))
        return f"{first.get('summary') or key} (confidence {first.get('confidence')})."
    if data.get("latest") is not None:
        return (
            f"The latest {label} reading is {data['latest']}, ranging {data.get('min')} to "
            f"{data.get('max')} over {data.get('samples')} samples."
        )
    return f"No {label} data is available."


__all__ = ["EVAL_CASES", "EvalCase", "scripted_routes"]
