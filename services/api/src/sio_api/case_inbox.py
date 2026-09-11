"""Permission-filtered operator queues and immutable shift handovers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from pydantic import Field, field_validator

from sio_core.tenancy import current_tenant

from .case_helpers import StrictBody, iso_now, parse_time
from .cases import SCAN_LIMIT, Casework, actor, need

PRIORITIES = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
SUMMARY_KEYS = (
    "case_id",
    "title",
    "owner",
    "status",
    "priority",
    "due_at",
    "verdict",
    "revision",
    "created_at",
    "updated_at",
    "zone_id",
    "source_id",
    "video_id",
)


class HandoverCreate(StrictBody):
    title: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=8000)
    case_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("case_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not x or len(x) > 200 for x in value):
            raise ValueError("Select unique valid case identifiers")
        return value


def inbox_snapshot(
    rows: list[dict[str, Any]],
    subject: str,
    now: datetime,
    *,
    view: str = "active",
    priority: str | None = None,
    q: str = "",
) -> dict[str, Any]:
    """Counts and urgency use server time; a resolved case is never overdue."""
    summarized = []
    for row in rows:
        item = {key: row.get(key) for key in SUMMARY_KEYS}
        item["priority"] = row.get("priority") or "normal"
        due = parse_time(row.get("due_at"))
        active = row.get("status") != "resolved"
        item["overdue"] = bool(active and due and due < now)
        item["due_soon"] = bool(active and due and now <= due <= now + timedelta(hours=24))
        summarized.append(item)
    counts = {
        "active": sum(x["status"] != "resolved" for x in summarized),
        "mine": sum(x["owner"] == subject and x["status"] != "resolved" for x in summarized),
        "unassigned": sum(not x["owner"] and x["status"] != "resolved" for x in summarized),
        "overdue": sum(x["overdue"] for x in summarized),
        "due_soon": sum(x["due_soon"] for x in summarized),
        "resolved": sum(x["status"] == "resolved" for x in summarized),
    }

    def selected(item: dict[str, Any]) -> bool:
        active = item["status"] != "resolved"
        matches_view = {
            "active": active,
            "mine": active and item["owner"] == subject,
            "unassigned": active and not item["owner"],
            "overdue": item["overdue"],
            "due_soon": item["due_soon"],
            "resolved": not active,
            "all": True,
        }.get(view, False)
        text = " ".join(
            str(item.get(key) or "") for key in ("title", "case_id", "owner", "zone_id")
        )
        return bool(
            matches_view
            and (not priority or item["priority"] == priority)
            and (not q or q.casefold() in text.casefold())
        )

    selected_rows = [item for item in summarized if selected(item)]
    selected_rows.sort(
        key=lambda item: (
            not item["overdue"],
            PRIORITIES.get(item["priority"], 2),
            (parse_time(item["due_at"]) or datetime.max.replace(tzinfo=UTC)).timestamp(),
            str(item["case_id"]),
        )
    )
    return {
        "cases": selected_rows,
        "counts": counts,
        "as_of": now.isoformat(),
        "subject": subject,
        "scan_limit": SCAN_LIMIT,
        "possibly_truncated": len(rows) >= SCAN_LIMIT,
    }


def install_inbox_routes(app: FastAPI, store: Any, pool: Any) -> None:
    casework = Casework(store, pool)

    @app.get("/api/case-inbox", tags=["casework"])
    async def inbox(
        request: Request,
        view: Literal[
            "active", "mine", "unassigned", "overdue", "due_soon", "resolved", "all"
        ] = "active",
        priority: Literal["urgent", "high", "normal", "low"] | None = None,
        q: str = Query(default="", max_length=200),
    ):
        principal = actor(request)
        rows = await casework.visible_cases(principal)
        return inbox_snapshot(
            rows, principal.subject, datetime.now(UTC), view=view, priority=priority, q=q
        )

    @app.get("/api/case-inbox/handovers", tags=["casework"])
    async def handovers(request: Request):
        principal = actor(request)
        visible = {row["case_id"] for row in await casework.visible_cases(principal)}
        records = await store.list(current_tenant(), "case_handover", limit=200)
        # A note may mention any of its linked cases. Hide the whole handover when access narrows.
        records = [row for row in records if set(row.get("case_ids", [])) <= visible]
        return {"handovers": records, "limit": 200}

    @app.post("/api/case-inbox/handovers", status_code=201, tags=["casework"])
    async def create_handover(body: HandoverCreate, request: Request):
        principal = actor(request)
        need(principal, "case.write")
        cases = []
        for case_id in body.case_ids:
            record = await casework.load(case_id, principal)
            need(principal, "case.write", record.get("zone_id"))
            cases.append({key: record.get(key) for key in SUMMARY_KEYS})
        handover_id = f"handover_{uuid4().hex}"
        return await store.put(
            current_tenant(),
            "case_handover",
            handover_id,
            {
                "handover_id": handover_id,
                **body.model_dump(),
                "cases": cases,
                "author": principal.subject,
                "captured_at": iso_now(),
            },
            expected_revision=0,
        )
