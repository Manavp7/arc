"""Agent memory: what happened last time, and what came of it (PRD M14).

The "learn" step of an observe-reason-decide-act-learn loop is usually the one that does nothing. A loop
that records its own actions and never reads them back has a diary, not a memory.

So memory here is **retrieval that changes the next decision**. An agent embeds a description of the
situation it is in, finds the most similar situations it has seen before, and reads what happened: was the
proposal approved, was it rejected, and if rejected, why. A proposal of a kind that a human rejected three
times is proposed differently, or not at all.

Two decisions that keep this honest:

* **The outcome is written when it is known, not when the action is proposed.** A memory written at proposal
  time records an intention; the useful record is the human's verdict, which arrives later. So entries are
  updated on approval or rejection, and an entry with no verdict yet is explicitly `pending` rather than
  silently counted as a success.
* **Similarity is not relevance.** A nearest-neighbour hit at distance 0.9 is not a precedent, and treating
  it as one produces confident nonsense — an agent "remembering" an unrelated incident. A similarity floor
  applies, and misses are reported as misses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sio_core import describe_error, get_logger
from sio_schemas import new_id, utc_now

log = get_logger("sio.agents.memory")

COLLECTION = "agent_memory"

SIMILARITY_FLOOR = 0.55
"""How similar a past situation must be to count as a precedent.

A nearest-neighbour search always returns something. Treating its best hit as relevant regardless of
distance is how an agent comes to "remember" an unrelated incident and act on it — so a floor applies and
misses are reported rather than filled in.
"""


@dataclass
class MemoryEntry:
    """One remembered situation and what came of it."""

    memory_id: str
    agent: str
    situation: str
    """A short description of the state the agent was in, in words, because that is what gets embedded."""
    proposal: str
    decision_id: str | None = None
    outcome: str = "pending"
    """``pending`` | ``approved`` | ``rejected`` | ``executed`` | ``failed``.

    Starts pending on purpose: a memory written at proposal time records an intention, and counting an
    intention as a success is how an agent learns the wrong lesson.
    """
    reason: str | None = None
    """The human's reason, when they gave one. The most valuable field here by a wide margin."""
    ts: datetime = field(default_factory=utc_now)
    zone_id: str | None = None
    similarity: float | None = None
    """Set when this entry was returned by a search, so a caller can see how close a precedent it is."""

    @property
    def was_rejected(self) -> bool:
        return self.outcome == "rejected"

    def summary(self) -> str:
        verdict = self.outcome + (f" ({self.reason})" if self.reason else "")
        return f"{self.situation} -> proposed {self.proposal} -> {verdict}"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "situation": self.situation,
            "proposal": self.proposal,
            "decision_id": self.decision_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "zone_id": self.zone_id,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_metadata(
        cls, memory_id: str, metadata: dict[str, Any], similarity: float | None = None
    ) -> MemoryEntry:
        return cls(
            memory_id=memory_id,
            agent=str(metadata.get("agent", "unknown")),
            situation=str(metadata.get("situation", "")),
            proposal=str(metadata.get("proposal", "")),
            decision_id=metadata.get("decision_id"),
            outcome=str(metadata.get("outcome", "pending")),
            reason=metadata.get("reason"),
            zone_id=metadata.get("zone_id"),
            ts=_parse_ts(metadata.get("ts")),
            similarity=similarity,
        )


@dataclass
class Recollection:
    """What memory had to say about the situation at hand."""

    precedents: list[MemoryEntry] = field(default_factory=list)
    searched: int = 0
    below_floor: int = 0
    """Hits discarded for being too dissimilar. Reported so a caller can tell 'no precedent' from
    'nothing was close enough', which are different facts."""

    @property
    def found(self) -> bool:
        return bool(self.precedents)

    @property
    def rejections(self) -> list[MemoryEntry]:
        return [entry for entry in self.precedents if entry.was_rejected]

    @property
    def caution(self) -> str | None:
        """A warning to carry into the proposal, if the record justifies one.

        The whole point of the memory. An agent that has had this kind of proposal rejected should say so
        when proposing it again — both because the operator deserves the context and because repeating a
        rejected proposal without acknowledging it is how automation earns distrust.
        """
        rejected = self.rejections
        if not rejected:
            return None
        reasons = [entry.reason for entry in rejected if entry.reason]
        detail = f" Reasons given: {'; '.join(reasons[:2])}." if reasons else ""
        return f"A similar proposal was rejected {len(rejected)} time(s) before.{detail}"

    def describe(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "searched": self.searched,
            "below_floor": self.below_floor,
            "precedents": [
                {
                    "situation": entry.situation,
                    "proposal": entry.proposal,
                    "outcome": entry.outcome,
                    "reason": entry.reason,
                    "similarity": round(entry.similarity, 3) if entry.similarity else None,
                }
                for entry in self.precedents
            ],
            "caution": self.caution,
        }


class AgentMemory:
    """Situation memory backed by the vector store.

    Uses the same `VectorStore` port and embedder as frame search, so agent memory is not a second
    persistence mechanism to operate. When no embedder is available the memory degrades to *nothing*
    rather than to a keyword match: a keyword "precedent" would be a different thing wearing the same
    name, and an agent citing it would be misleading about why it acted.
    """

    def __init__(self, vectors: Any, embedder: Any, *, tenant_id: str = "default") -> None:
        self.vectors = vectors
        self.embedder = embedder
        self.tenant_id = tenant_id
        self.written = 0
        self.searched = 0
        self.unavailable = vectors is None or embedder is None

    async def remember(
        self,
        *,
        agent: str,
        situation: str,
        proposal: str,
        decision_id: str | None = None,
        zone_id: str | None = None,
    ) -> MemoryEntry | None:
        """Record a situation and the proposal made in it, outcome still unknown."""
        entry = MemoryEntry(
            memory_id=new_id("mem"),
            agent=agent,
            situation=situation,
            proposal=proposal,
            decision_id=decision_id,
            zone_id=zone_id,
        )
        if self.unavailable:
            return entry
        try:
            vector = await _embed(self.embedder, situation)
            await self.vectors.upsert(
                COLLECTION,
                entry.memory_id,
                vector,
                tenant_id=self.tenant_id,
                metadata=entry.to_metadata(),
                ts=entry.ts,
            )
            self.written += 1
        except Exception as exc:
            log.warning("agents.memory_write_failed", error=describe_error(exc))
        return entry

    async def record_outcome(
        self, entry: MemoryEntry, outcome: str, *, reason: str | None = None
    ) -> None:
        """Update an entry once a human has ruled on it.

        Written when the verdict is known rather than at proposal time, because the verdict is the part
        worth learning from. The situation embedding does not change — only the metadata — so the same
        vector now carries its outcome.
        """
        entry.outcome = outcome
        entry.reason = reason
        if self.unavailable:
            return
        try:
            vector = await _embed(self.embedder, entry.situation)
            await self.vectors.upsert(
                COLLECTION,
                entry.memory_id,
                vector,
                tenant_id=self.tenant_id,
                metadata=entry.to_metadata(),
                ts=entry.ts,
            )
        except Exception as exc:
            log.warning("agents.memory_update_failed", error=describe_error(exc))

    async def recall(
        self, situation: str, *, agent: str | None = None, limit: int = 3
    ) -> Recollection:
        """Find past situations similar to this one."""
        recollection = Recollection()
        if self.unavailable:
            return recollection
        try:
            vector = await _embed(self.embedder, situation)
            hits = await self.vectors.search(
                COLLECTION, vector, tenant_id=self.tenant_id, limit=limit * 3
            )
        except Exception as exc:
            log.warning("agents.memory_search_failed", error=describe_error(exc))
            return recollection

        self.searched += 1
        for hit in hits:
            recollection.searched += 1
            score = float(getattr(hit, "score", 0.0) or 0.0)
            metadata = dict(getattr(hit, "metadata", {}) or {})
            if agent and metadata.get("agent") != agent:
                continue
            if score < SIMILARITY_FLOOR:
                # Not a precedent. Counting it as one is how an agent comes to "remember" an unrelated
                # incident, and the confident nonsense that follows is worse than having no memory.
                recollection.below_floor += 1
                continue
            recollection.precedents.append(
                MemoryEntry.from_metadata(
                    str(getattr(hit, "item_id", "") or metadata.get("memory_id", "")),
                    metadata,
                    similarity=score,
                )
            )
            if len(recollection.precedents) >= limit:
                break
        return recollection

    def describe(self) -> dict[str, Any]:
        return {
            "available": not self.unavailable,
            "collection": COLLECTION,
            "similarity_floor": SIMILARITY_FLOOR,
            "written": self.written,
            "searched": self.searched,
        }


async def _embed(embedder: Any, text: str) -> list[float]:
    """Embed a description, tolerating either an async or a sync embedder.

    The embedders in this codebase differ — the CLIP one is sync, others are not — and an agent should not
    need to know which it has.
    """
    for name in ("embed_text", "embed"):
        function = getattr(embedder, name, None)
        if function is None:
            continue
        result = function(text)
        if hasattr(result, "__await__"):
            result = await result
        return list(result)
    raise AttributeError("the embedder exposes neither embed_text nor embed")


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return utc_now()


__all__ = ["COLLECTION", "SIMILARITY_FLOOR", "AgentMemory", "MemoryEntry", "Recollection"]
