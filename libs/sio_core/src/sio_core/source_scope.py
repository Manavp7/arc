"""Source-derived zone boundaries shared by collection, stream and media readers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .authn import Principal

ZoneScope = tuple[str, ...] | None


def zones_for(principal: Principal) -> ZoneScope:
    """Empty token scope is unrestricted; restricted tokens cannot read unknown zones."""
    return None if principal.is_admin or not principal.zones else tuple(sorted(principal.zones))


def zone_visible(scope: ZoneScope, zone_id: Any) -> bool:
    return scope is None or (isinstance(zone_id, str) and zone_id in scope)


def require_zone(principal: Principal, zone_id: Any) -> None:
    if not zone_visible(zones_for(principal), zone_id):
        raise HTTPException(403, "Your zone access does not permit this source")


def zone_sql(scope: ZoneScope, column: str = "zone_id") -> tuple[str, list[Any]]:
    """The column/expression must be authored SQL, never user input."""
    return ("", []) if scope is None else (f" AND {column} = ANY(%s)", [list(scope)])


def state_zone_sql(alias: str = "entity_states") -> str:
    """Historical membership wins over the state's optional zone hint."""
    return f"""COALESCE((SELECT r.to_id FROM relationships r
        WHERE r.tenant_id = {alias}.tenant_id AND r.from_id = {alias}.entity_id
          AND r.type = 'entered' AND r.ts_valid_from <= {alias}.ts
          AND (r.ts_valid_to IS NULL OR r.ts_valid_to >= {alias}.ts)
        ORDER BY r.ts_valid_from DESC, r.to_id LIMIT 1), {alias}.zone_id)"""


# These surfaces compute whole-site aggregates, AI context or configuration and do
# not yet have a source-aware projection. Refuse restricted tokens at BOTH entry
# points instead of returning unfiltered results or filtering after aggregation.
_UNSCOPED_SURFACES: dict[str, tuple[str, ...]] = {
    "api": (
        "/api/forecasts",
        "/api/missions",
        "/api/analytics",
        "/api/search/frames",
        "/api/copilot",
        "/api/agents",
        "/api/simulations",
        "/api/audit",
        "/api/sources",
        "/api/camera-setups",
        "/api/sites",
        "/api/system",
        "/api/webhooks",
        "/api/workflow/authored",
    ),
    "prediction": ("/forecasts", "/predict"),
    "missions": ("/missions",),
    "analytics": ("/analytics",),
    "worldmodel": ("/search", "/world", "/entities", "/counts"),
    "copilot": ("/copilot",),
    "agents": ("/agents",),
    "simulation": ("/simulations", "/simulation"),
    "ingest": ("/sources", "/connectors", "/site", "/simulation"),
    "workflow": ("/workflow/authored",),
    "governance": ("/audit",),
    "spatial": ("/spatial", "/site", "/spatial/zones"),
    "tracking": ("/tracks", "/cross-camera", "/counts"),
    "fusion": ("/fusion",),
    "events": ("/events",),
    "webhooks": ("/webhooks",),
    "decision": ("/decisions/recommend", "/decisions/schedule/docks"),
}


def unsupported_zone_surface(service: str, path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _UNSCOPED_SURFACES.get(service, ())
    )
