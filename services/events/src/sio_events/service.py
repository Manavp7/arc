"""Events service: rule CEP, anomaly detection, and the event log (PRD M9).

Owns the ``events`` table. Every producer — spatial, perception, this service — publishes to the
``events`` topic, and this service persists them. One writer per table, and the component that needs
recent events for correlation is the same one that already has them.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query

from sio_core import MessageContext, PgPool, SioService, get_logger, get_pg_pool
from sio_core.explain import ExplanationBuilder
from sio_schemas import (
    BusMessage,
    Event,
    EventType,
    EvidenceKind,
    Geo,
    Severity,
    Topic,
    utc_now,
)

from .anomaly import FeatureVector, build_detector
from .engine import Match, RuleEngine
from .facts import Fact, fact_from_message
from .rules import describe_rules, fingerprint_of, load_rules

log = get_logger("sio.events")


class EventsService(SioService):
    """Turns facts into events, and keeps the event log."""

    name = "events"
    subscribes = (Topic.ENTITIES, Topic.EVENTS, Topic.DETECTIONS, Topic.RAW_IOT)
    tick_interval_s = 15.0

    RELOAD_CHECK_S = 5.0
    FEATURE_INTERVAL_S = 30.0
    """How often a feature vector is handed to the anomaly detector.

    Anomaly detection needs a *rate*, not an event: "twelve people entered in a minute" is only odd
    relative to how many usually do. So features are windowed counts, sampled on a fixed cadence.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool: PgPool = get_pg_pool(self.settings)
        self.rules_dir = Path(self.settings.rules_dir)
        ruleset = load_rules(self.rules_dir)
        self.engine = RuleEngine(ruleset)
        self.detector = build_detector(
            self.settings.anomaly_detector,
            warmup=self.settings.anomaly_warmup,
            contamination=self.settings.anomaly_contamination,
        )
        self._fingerprint = ruleset.fingerprint
        self._last_reload_check = 0.0
        self._last_feature_sample = time.monotonic()
        self._counters: dict[str, float] = defaultdict(float)
        self._events_persisted = 0
        self._events_published = 0
        self._facts_seen = 0
        self._anomalies = 0
        self._recent: list[Event] = []
        self._source_zones: dict[str, str] = {}
        """Which zone each sensor watches.

        Without this a fire event says nothing about WHERE. A detection fact carries a camera id, not a
        zone — so the fire response playbook dispatched a drone "to unknown" and fell back to the default
        gate, which is a response nobody can act on. The camera's zone is in the `sources` table; the event
        just has to carry it.
        """

    async def setup(self) -> None:
        await self.pool.open()
        await self._load_source_zones()
        ruleset = self.engine.ruleset
        self.log.info(
            "events.ready",
            rules=len(ruleset.rules),
            enabled=len(ruleset.enabled()),
            match=len(ruleset.by_shape("match")),
            window=len(ruleset.by_shape("window")),
            absence=len(ruleset.by_shape("absence")),
            files=ruleset.loaded_from,
            errors=ruleset.errors or None,
            detector=self.detector.name,
        )
        if ruleset.errors:
            # Loud, but not fatal. A malformed rule file must not take the engine down, or one typo
            # disables the fire rule.
            self.log.warning("events.rule_errors", count=len(ruleset.errors), errors=ruleset.errors)

    async def _load_source_zones(self) -> None:
        """Map sensor to zone once, so every event derived from a detection can say where it happened."""
        rows = await self.pool.fetch(
            "SELECT source_id, zone_id FROM sources WHERE tenant_id = %s AND zone_id IS NOT NULL",
            (self.settings.tenant_id,),
        )
        self._source_zones = {str(row["source_id"]): str(row["zone_id"]) for row in rows}
        self.log.info("events.source_zones", mapped=len(self._source_zones))
        if not self._source_zones:
            self.log.warning(
                "events.no_source_zones",
                effect="events from detections will not carry a zone",
                hint="run: just seed",
            )

    async def health_checks(self) -> dict[str, str]:
        checks = {"postgres": "ok" if await self.pool.ping() else "unreachable"}
        errors = self.engine.ruleset.errors
        checks["rules"] = (
            f"ok ({len(self.engine.ruleset.enabled())} enabled)"
            if not errors
            else f"degraded: {len(errors)} rule(s) failed to load"
        )
        return checks

    async def health_info(self) -> dict[str, str]:
        return {
            "facts_seen": str(self._facts_seen),
            "events_published": str(self._events_published),
            "events_persisted": str(self._events_persisted),
            "anomalies": str(self._anomalies),
            "suppressed_by_cooldown": str(self.engine.stats.get("suppressed", 0)),
            "detector": self.detector.name,
        }

    # ------------------------------------------------------------------ handling
    async def on_message(self, message: BusMessage, ctx: MessageContext) -> None:
        # An event this service published comes back around on the events topic. Persist it, count it
        # for anomaly features, and offer it to rules that compose on events — but never let it be
        # re-derived into itself, which is what the guard in _fire is for.
        if message.kind == "Event":
            event = message.decode(Event)
            await self._persist(event)

        fact = fact_from_message(message.kind, message.decode)
        if fact is None:
            return
        self._facts_seen += 1
        self._count_for_features(fact)

        for match in self.engine.evaluate(fact):
            await self._fire(match, ctx)

        await self._maybe_sample_features(ctx)

    async def _fire(self, match: Match, ctx: MessageContext | None) -> None:
        """Publish the event a rule asserts."""
        rule = match.rule
        if match.fact.kind == "event" and match.fact.get("rule_id") == rule.id:
            return  # never re-derive an event from itself

        explanation = ExplanationBuilder(
            summary=self._render(rule.explanation, match) or rule.description
        )
        explanation.add_rule(rule.id, note=rule.description or None)
        for reason in match.reasons:
            explanation.add_note(reason)
        for ref in match.evidence[:8]:
            explanation.add_evidence(EvidenceKind.OBSERVATION, ref, ts=match.fact.ts)
        if match.aggregate_value is not None and rule.window is not None:
            # State the count and the aggregate, and nothing more.
            #
            # This note used to add "so a single noisy sample cannot trigger this", which was an
            # editorial claim about robustness that depends entirely on which aggregate a rule chose.
            # It appeared verbatim on an event whose window contained exactly one fact. An explanation
            # that flatters the rule is worse than a terse one, because it is the part a human trusts.
            explanation.add_note(
                f"{rule.window.aggregate} computed over {len(match.contributing)} fact(s) in a "
                f"{rule.window.seconds:.0f}s window"
            )

        # A detection knows its camera, not its zone. Resolve it, so the event says where — and record
        # that it was inferred from the camera rather than observed directly.
        zone_id = match.fact.zone_id
        inferred_zone = False
        if not zone_id and match.fact.source_id:
            zone_id = self._source_zones.get(match.fact.source_id)
            inferred_zone = zone_id is not None
        if inferred_zone:
            explanation.add_note(
                f"zone {zone_id} inferred from the sensor {match.fact.source_id} that reported it, "
                "not observed directly"
            )

        latitude, longitude = match.fact.get("lat"), match.fact.get("lon")
        event = Event(
            tenant_id=match.fact.tenant_id or self.settings.tenant_id,
            type=self._event_type(rule.emits),
            severity=self._severity(rule.severity),
            entities=match.entity_ids,
            geo=Geo(lat=float(latitude), lon=float(longitude)) if latitude and longitude else None,
            zone_id=zone_id,
            ts=match.fact.ts,
            detected_ts=utc_now(),
            confidence=rule.confidence,
            explanation=explanation.build(),
            rule_id=rule.id,
            source_ids=[match.fact.source_id] if match.fact.source_id else [],
            attributes={
                **rule.attributes,
                "rule_shape": rule.shape,
                "subject": match.subject,
                **({"zone_inferred_from_sensor": True} if inferred_zone else {}),
                **(
                    {"aggregate": match.aggregate_value}
                    if match.aggregate_value is not None
                    else {}
                ),
            },
        )
        await self._emit(event, ctx)

    async def _emit(self, event: Event, ctx: MessageContext | None) -> None:
        if ctx is not None:
            await ctx.publish(Topic.EVENTS, event)
        else:
            await self.publish(Topic.EVENTS, event)
        self._events_published += 1
        self.log.info(
            "events.fired",
            rule=event.rule_id,
            type=str(event.type),
            severity=str(event.severity),
            entities=event.entities[:3],
            zone=event.zone_id,
            latency_ms=round(event.detection_latency_s * 1000, 1),
        )

    async def _persist(self, event: Event) -> None:
        """Append to the event log.

        ``ON CONFLICT DO NOTHING`` rather than an upsert: the events table is append-only and enforced
        as such by a trigger, and at-least-once delivery means the same event *will* arrive twice.
        Ignoring the duplicate is correct; trying to update it would hit the immutability trigger and
        turn redelivery into an error storm.
        """
        await self.pool.execute(
            """
            INSERT INTO events (
                event_id, tenant_id, type, severity, entities, geom, zone_id,
                ts, detected_ts, confidence, rule_id, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, event_id) DO NOTHING
            """,
            (
                event.event_id,
                event.tenant_id,
                str(event.type),
                str(event.severity),
                event.entities,
                f"SRID=4326;POINT({event.geo.lon} {event.geo.lat})" if event.geo else None,
                event.zone_id,
                event.ts,
                event.detected_ts,
                event.confidence,
                event.rule_id,
                event.to_json(),
            ),
        )
        self._events_persisted += 1
        self._recent.append(event)
        if len(self._recent) > 200:
            del self._recent[:-200]

    # ------------------------------------------------------------------- anomaly
    def _count_for_features(self, fact: Fact) -> None:
        """Accumulate the counters that become an anomaly feature vector."""
        self._counters[f"{fact.kind}_count"] += 1
        if fact.kind == "event":
            self._counters[f"event_{fact.get('event_type')}"] += 1
            if str(fact.get("severity")) in ("high", "critical"):
                self._counters["severe_events"] += 1
        if fact.kind == "entity":
            speed = fact.get("speed_kmh") or 0.0
            self._counters["speed_sum"] += float(speed)
            self._counters["entity_samples"] += 1
            if fact.get("entity_type") == "person":
                self._counters["person_samples"] += 1
        if fact.kind == "detection":
            self._counters["detection_confidence_sum"] += float(fact.get("confidence") or 0.0)

    async def _maybe_sample_features(self, ctx: MessageContext | None) -> None:
        now = time.monotonic()
        if now - self._last_feature_sample < self.FEATURE_INTERVAL_S:
            return
        elapsed = now - self._last_feature_sample
        self._last_feature_sample = now

        counters = dict(self._counters)
        self._counters.clear()
        samples = max(1.0, counters.get("entity_samples", 0.0))
        vector = FeatureVector(
            ts=utc_now(),
            subject="site",
            values={
                # Rates, not totals: a longer sampling interval must not look like a busier site.
                "entities_per_min": counters.get("entity_count", 0.0) / elapsed * 60,
                "detections_per_min": counters.get("detection_count", 0.0) / elapsed * 60,
                "events_per_min": counters.get("event_count", 0.0) / elapsed * 60,
                "severe_events_per_min": counters.get("severe_events", 0.0) / elapsed * 60,
                "mean_speed_kmh": counters.get("speed_sum", 0.0) / samples,
                "person_fraction": counters.get("person_samples", 0.0) / samples,
            },
        )
        verdict = self.detector.observe(vector)
        if not verdict.is_anomaly:
            return

        self._anomalies += 1
        explanation = ExplanationBuilder(
            summary=(
                "Site activity does not match its recent baseline: "
                + ", ".join(verdict.top_features)
            )
        )
        explanation.add_model(verdict.detector, note=f"{verdict.samples} samples of history")
        for reason in verdict.reasons:
            explanation.add_note(reason)
        explanation.add_note(
            "No rule describes this pattern; it is flagged because the measurements themselves are "
            "far from their own recent behaviour"
        )
        await self._emit(
            Event(
                tenant_id=self.settings.tenant_id,
                type=EventType.ANOMALY_DETECTED,
                severity=Severity.MEDIUM if verdict.score < 0.8 else Severity.HIGH,
                ts=verdict.ts,
                detected_ts=utc_now(),
                confidence=round(min(0.9, 0.4 + verdict.score / 2), 3),
                explanation=explanation.build(),
                rule_id=None,  # anomalies have no rule, and the schema says so
                attributes={
                    "detector": verdict.detector,
                    "score": verdict.score,
                    "subject": verdict.subject,
                    "features": {
                        name: {
                            "observed": round(observed, 3),
                            "baseline": round(baseline, 3),
                            "z": round(z, 2),
                        }
                        for name, observed, baseline, z in verdict.deviations[:6]
                    },
                },
            ),
            ctx,
        )

    # ---------------------------------------------------------------------- tick
    async def tick(self) -> None:
        await self._maybe_reload()

        now = utc_now()
        for match in self.engine.check_absences(now):
            await self._fire(match, None)

        pruned = self.engine.prune(now)
        self.log.info(
            "events.stats",
            facts=self._facts_seen,
            published=self._events_published,
            persisted=self._events_persisted,
            anomalies=self._anomalies,
            suppressed=self.engine.stats.get("suppressed", 0),
            windows=len(self.engine._windows),
            pruned=pruned,
            rules=len(self.engine.ruleset.enabled()),
        )

    async def _maybe_reload(self) -> None:
        """Reload rules when the files on disk change.

        Hot reload is an acceptance criterion (M22), and it is also the difference between a rule change
        being a five-second edit and a deploy. Window state is preserved across a reload, so editing one
        rule does not blind the others for their whole window.
        """
        now = time.monotonic()
        if now - self._last_reload_check < self.RELOAD_CHECK_S:
            return
        self._last_reload_check = now
        current = fingerprint_of(self.rules_dir)
        if current == self._fingerprint:
            return
        ruleset = load_rules(self.rules_dir)
        self._fingerprint = ruleset.fingerprint
        self.engine.replace_rules(ruleset)
        self.log.info(
            "events.rules_reloaded",
            rules=len(ruleset.rules),
            enabled=len(ruleset.enabled()),
            errors=ruleset.errors or None,
        )

    # -------------------------------------------------------------------- routes
    def routes(self, app: FastAPI) -> None:
        @app.get("/events/rules", tags=["events"])
        async def rules() -> dict[str, Any]:
            """Every loaded rule, plus the ones that failed to load and why."""
            ruleset = self.engine.ruleset
            return {
                "directory": str(self.rules_dir),
                "files": ruleset.loaded_from,
                "count": len(ruleset.rules),
                "enabled": len(ruleset.enabled()),
                "errors": ruleset.errors,
                "rules": describe_rules(ruleset.rules),
            }

        @app.post("/events/rules/reload", tags=["events"])
        async def reload() -> dict[str, Any]:
            """Reload now, rather than waiting for the timer."""
            ruleset = load_rules(self.rules_dir)
            self._fingerprint = ruleset.fingerprint
            self.engine.replace_rules(ruleset)
            return {
                "reloaded": len(ruleset.rules),
                "enabled": len(ruleset.enabled()),
                "errors": ruleset.errors,
            }

        @app.get("/events/engine", tags=["events"])
        async def engine_state() -> dict[str, Any]:
            """Engine internals: windows, firing counts, and how much the cooldowns suppressed."""
            return {
                **self.engine.describe(),
                "anomaly": self.detector.describe(),
                "events_published": self._events_published,
                "events_persisted": self._events_persisted,
            }

        @app.get("/events/recent", tags=["events"])
        async def recent(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
            """Recent events with their explanations, newest first."""
            return {
                "events": [
                    {
                        "event_id": event.event_id,
                        "type": str(event.type),
                        "severity": str(event.severity),
                        "ts": event.ts.isoformat(),
                        "latency_ms": round(event.detection_latency_s * 1000, 1),
                        "entities": event.entities,
                        "zone_id": event.zone_id,
                        "rule_id": event.rule_id,
                        "confidence": event.confidence,
                        "summary": event.explanation.summary,
                        "why": event.explanation.notes,
                    }
                    for event in reversed(self._recent[-limit:])
                ]
            }

        @app.post("/events/simulate", tags=["events"])
        async def simulate(fact: dict[str, Any]) -> dict[str, Any]:
            """Evaluate a hand-written fact against the rules without publishing anything.

            The tool a rule author actually needs: edit YAML, post the fact you expect it to catch, and
            see which clauses matched and which did not. Without it, tuning a rule means waiting for
            reality to produce the situation.
            """
            probe = Fact(
                kind=str(fact.pop("kind", "entity")),
                ts=utc_now(),
                fields=dict(fact),
                entity_id=fact.get("entity_id"),
                zone_id=fact.get("zone_id"),
                source_id=fact.get("source_id"),
                tenant_id=self.settings.tenant_id,
            )
            matches = self.engine.evaluate(probe)
            return {
                "fact": probe.fields,
                "matched": [
                    {
                        "rule": match.rule.id,
                        "emits": match.rule.emits,
                        "severity": match.rule.severity,
                        "why": match.reasons,
                    }
                    for match in matches
                ],
                "evaluated_rules": len(self.engine.ruleset.enabled()),
            }

    # ------------------------------------------------------------------- helpers
    @staticmethod
    def _event_type(emits: str) -> EventType:
        try:
            return EventType(emits)
        except ValueError:
            # A rule naming an unknown event type still fires, as something rather than nothing. A
            # dropped event would be far harder to notice than an oddly-typed one.
            log.warning("events.unknown_event_type", emits=emits, using="anomaly_detected")
            return EventType.ANOMALY_DETECTED

    @staticmethod
    def _severity(name: str) -> Severity:
        try:
            return Severity(name)
        except ValueError:
            return Severity.INFO

    @staticmethod
    def _render(template: str, match: Match) -> str:
        """Interpolate ``{field}`` references from the matched fact.

        Missing fields render as the placeholder rather than raising: a typo in a rule's explanation must
        not stop the event, because the event is the part that matters.
        """
        if not template:
            return ""
        text = template.strip()
        for token in set(_tokens(text)):
            value = match.fact.get(token)
            if value is None and token == "label":
                value = match.fact.get("entity_id")
            if value is not None:
                text = text.replace("{" + token + "}", _pretty(value))
        return text


def _tokens(text: str) -> list[str]:
    import re

    return re.findall(r"\{([a-zA-Z0-9_.]+)\}", text)


def _pretty(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


__all__ = ["EventsService"]
