"""Exception hierarchy shared across services."""

from __future__ import annotations


class SioError(Exception):
    """Base class for every error SIO raises deliberately."""


class ConfigError(SioError):
    """Configuration is missing or contradictory."""


class AdapterUnavailable(SioError):
    """An adapter's backing dependency is missing or unreachable.

    Raised at construction time rather than on first use, so a service fails fast at boot
    with a clear message instead of dying mid-stream.
    """


class BusError(SioError):
    """Publishing or consuming failed."""


class StoreError(SioError):
    """A datastore operation failed."""


class NotFound(SioError):
    """A requested resource does not exist."""


class ValidationFailed(SioError):
    """Input did not satisfy a contract."""


class PolicyDenied(SioError):
    """Authorisation refused the action. Carries the reason for the audit record."""

    def __init__(self, action: str, resource: str, reason: str) -> None:
        super().__init__(f"denied {action} on {resource}: {reason}")
        self.action = action
        self.resource = resource
        self.reason = reason


class ModelUnavailable(SioError):
    """A model file or inference backend is missing.

    Distinct from :class:`AdapterUnavailable` because the remedy is different: run
    ``just models``.
    """


class DependencyMissing(SioError):
    """An optional Python package required by the selected adapter is not installed."""

    def __init__(self, package: str, adapter: str, extra: str | None = None) -> None:
        hint = f"uv add {extra or package}"
        super().__init__(f"{adapter} requires the {package!r} package; install it with: {hint}")
        self.package = package
        self.adapter = adapter
