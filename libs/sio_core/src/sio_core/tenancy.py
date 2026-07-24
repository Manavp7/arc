"""Tenant scoping helpers.

Multi-tenancy is enforced at the *query* level, not by convention: every store method takes a
``tenant_id``, and this module is how a request's tenant reaches them. Phase 5 adds the
authenticated principal that populates it; until then the configured default applies.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .config import get_settings
from .errors import PolicyDenied
from .telemetry import get_tenant_id, set_tenant_id


def current_tenant() -> str:
    """The tenant for the current context: request/message scope, else the configured default."""
    return get_tenant_id() or get_settings().tenant_id


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """Bind ``tenant_id`` for the duration of a block."""
    previous = get_tenant_id()
    set_tenant_id(tenant_id)
    try:
        yield tenant_id
    finally:
        set_tenant_id(previous)


def assert_same_tenant(resource_tenant: str, *, action: str = "read", resource: str = "") -> None:
    """Guard against cross-tenant access on an already-loaded object.

    Store queries filter by tenant, so this is a second line of defence for code paths that
    receive an object from elsewhere (a bus message, a cache) and must not serve it to the
    wrong caller.
    """
    tenant = current_tenant()
    if resource_tenant != tenant:
        raise PolicyDenied(
            action, resource or resource_tenant, f"resource belongs to tenant {resource_tenant!r}"
        )
