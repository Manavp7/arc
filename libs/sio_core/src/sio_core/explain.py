"""Explanation builder — the mechanism behind PRD M20 ("explainable by default").

Every event, alert, decision and copilot answer must be able to answer "why do you believe
that?". Making it a builder rather than a convention means the shape is consistent, the
confidence arithmetic is in one place, and a service cannot forget a field.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sio_schemas import (
    Alternative,
    Detection,
    Entity,
    Event,
    EvidenceKind,
    EvidenceRef,
    Explanation,
    Observation,
    TimelineEntry,
    Track,
)


class ExplanationBuilder:
    """Accumulates evidence, then produces an :class:`Explanation`.

    ``confidence`` is derived from the evidence unless explicitly set, using a
    noisy-OR combination: independent pieces of supporting evidence increase belief without
    ever reaching certainty, which is the honest behaviour for sensor fusion.
    """

    def __init__(self, summary: str | None = None) -> None:
        self._summary = summary
        self._evidence: list[EvidenceRef] = []
        self._sources: list[str] = []
        self._timeline: list[TimelineEntry] = []
        self._related: list[str] = []
        self._alternatives: list[Alternative] = []
        self._notes: list[str] = []
        self._confidence: float | None = None
        self._degraded = False

    # ------------------------------------------------------------------- evidence
    def add_evidence(
        self,
        kind: EvidenceKind | str,
        ref: str,
        *,
        ts: datetime | None = None,
        source_id: str | None = None,
        score: float | None = None,
        note: str | None = None,
    ) -> ExplanationBuilder:
        self._evidence.append(
            EvidenceRef(
                kind=EvidenceKind(kind) if isinstance(kind, str) else kind,
                ref=ref,
                ts=ts,
                source_id=source_id,
                score=score,
                note=note,
            )
        )
        if source_id:
            self.add_source(source_id)
        return self

    def add_observation(self, observation: Observation) -> ExplanationBuilder:
        self.add_evidence(
            EvidenceKind.OBSERVATION,
            observation.id,
            ts=observation.ts,
            source_id=observation.source_id,
            score=observation.confidence,
        )
        if observation.raw_ref:
            self.add_evidence(
                EvidenceKind.FRAME,
                observation.raw_ref,
                ts=observation.ts,
                source_id=observation.source_id,
            )
        return self.add_timeline(
            observation.ts, "observation", f"{observation.modality} from {observation.source_id}"
        )

    def add_detection(self, detection: Detection) -> ExplanationBuilder:
        self.add_evidence(
            EvidenceKind.DETECTION,
            detection.id,
            ts=detection.ts,
            source_id=detection.source_id,
            score=detection.confidence,
            note=f"{detection.class_name} @ {detection.confidence:.2f}",
        )
        return self.add_timeline(
            detection.ts,
            "detection",
            f"{detection.class_name} detected by {detection.source_id} "
            f"({detection.confidence:.0%} confidence)",
            ref=detection.id,
        )

    def add_track(self, track: Track) -> ExplanationBuilder:
        self.add_evidence(
            EvidenceKind.TRACK,
            track.track_id,
            ts=track.last_ts,
            source_id=track.source_id,
            score=track.confidence,
            note=f"{track.class_name}, {track.hits} hits over {track.duration_s:.0f}s",
        )
        return self

    def add_entity(self, entity: Entity, *, as_evidence: bool = True) -> ExplanationBuilder:
        self.add_related(entity.entity_id)
        for source in entity.sources:
            self.add_source(source)
        if as_evidence:
            self.add_evidence(
                EvidenceKind.ENTITY,
                entity.entity_id,
                ts=entity.last_seen,
                score=entity.confidence,
                note=entity.label or str(entity.type),
            )
        return self

    def add_event(self, event: Event) -> ExplanationBuilder:
        self.add_evidence(
            EvidenceKind.EVENT,
            event.event_id,
            ts=event.ts,
            score=event.confidence,
            note=f"{event.type} ({event.severity})",
        )
        for entity_id in event.entities:
            self.add_related(entity_id)
        return self.add_timeline(
            event.ts, "event", f"{event.type} ({event.severity})", ref=event.event_id
        )

    def add_query(self, query: str, *, backend: str, rows: int | None = None) -> ExplanationBuilder:
        """Record the query that produced an answer.

        This is the difference between "the copilot says three trucks" and "the copilot ran
        *this* traversal, which returned three rows" — the single most useful piece of evidence
        when an operator distrusts an answer.
        """
        note = f"{backend}" if rows is None else f"{backend}, {rows} rows"
        return self.add_evidence(EvidenceKind.QUERY, query, note=note)

    def add_rule(self, rule_id: str, *, note: str | None = None) -> ExplanationBuilder:
        return self.add_evidence(EvidenceKind.RULE, rule_id, note=note)

    def add_model(self, model_name: str, *, note: str | None = None) -> ExplanationBuilder:
        return self.add_evidence(EvidenceKind.MODEL, model_name, note=note)

    # --------------------------------------------------------------------- context
    def add_source(self, source_id: str) -> ExplanationBuilder:
        if source_id and source_id not in self._sources:
            self._sources.append(source_id)
        return self

    def add_sources(self, source_ids: Iterable[str]) -> ExplanationBuilder:
        for source_id in source_ids:
            self.add_source(source_id)
        return self

    def add_related(self, entity_id: str) -> ExplanationBuilder:
        if entity_id and entity_id not in self._related:
            self._related.append(entity_id)
        return self

    def add_timeline(
        self, ts: datetime, kind: str, summary: str, *, ref: str | None = None
    ) -> ExplanationBuilder:
        self._timeline.append(TimelineEntry(ts=ts, kind=kind, summary=summary, ref=ref))
        return self

    def add_alternative(
        self, hypothesis: str, *, confidence: float = 0.0, why_not: str | None = None
    ) -> ExplanationBuilder:
        self._alternatives.append(
            Alternative(hypothesis=hypothesis, confidence=confidence, why_not=why_not)
        )
        return self

    def add_note(self, note: str) -> ExplanationBuilder:
        self._notes.append(note)
        return self

    def summary(self, text: str) -> ExplanationBuilder:
        self._summary = text
        return self

    def confidence(self, value: float) -> ExplanationBuilder:
        self._confidence = max(0.0, min(1.0, value))
        return self

    def degraded(self, reason: str) -> ExplanationBuilder:
        """Mark the answer as produced by a fallback path, and say why."""
        self._degraded = True
        return self.add_note(f"degraded: {reason}")

    # ----------------------------------------------------------------------- build
    def _derived_confidence(self) -> float:
        scores = [e.score for e in self._evidence if e.score is not None]
        if not scores:
            return 0.5 if self._evidence else 0.0
        # Noisy-OR: P(true) = 1 - Π(1 - sᵢ), capped so nothing is ever certain.
        product = 1.0
        for score in scores:
            product *= 1.0 - max(0.0, min(1.0, score))
        return min(0.99, 1.0 - product)

    def build(self) -> Explanation:
        self._timeline.sort(key=lambda entry: entry.ts)
        return Explanation(
            summary=self._summary,
            evidence=list(self._evidence),
            confidence=self._confidence
            if self._confidence is not None
            else self._derived_confidence(),
            sources=list(self._sources),
            timeline=list(self._timeline),
            related_entities=list(self._related),
            alternatives=list(self._alternatives),
            degraded=self._degraded,
            notes=list(self._notes),
        )


def merge_explanations(
    explanations: Sequence[Explanation], *, summary: str | None = None
) -> Explanation:
    """Combine explanations (e.g. an alert grouping several events) without losing evidence."""
    builder = ExplanationBuilder(summary)
    for explanation in explanations:
        for evidence in explanation.evidence:
            builder._evidence.append(evidence)
        builder.add_sources(explanation.sources)
        for entry in explanation.timeline:
            builder._timeline.append(entry)
        for entity_id in explanation.related_entities:
            builder.add_related(entity_id)
        for alternative in explanation.alternatives:
            builder._alternatives.append(alternative)
        for note in explanation.notes:
            builder.add_note(note)
        if explanation.degraded:
            builder._degraded = True
    return builder.build()
