"""What they may do (PRD M18, Phase 5).

RBAC and ABAC behind one port, with three implementations that must agree.

**The hard constraint is that the embedded engine and OPA cannot diverge.** The PRD wants Rego policies; the
platform must also run with no OPA binary present. Two independent implementations of the same rules is a
guarantee of drift, and drift in an authorisation layer means dev allows what production denies, or worse.

So the rules live in **one place**: a table of `Rule` objects here. `EmbeddedPolicyEngine` evaluates them
directly. `infra/opa/policies/sio.rego` is *generated* from that table by `just policies`, and a test asserts
the generated file matches — so a rule added in Python and not regenerated fails CI rather than quietly
diverging.

**Deny is the default and denials explain themselves.** A policy decision an operator cannot understand is
one they route around, usually by acquiring a broader role. `Decision.reason` is written for a human and is
carried into the 403 body and the audit row.

**Every decision is auditable, including the allows.** An audit trail of denials answers "who was stopped";
an audit trail of allows answers "who did this", which is the question that actually gets asked after an
incident.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .authn import ANONYMOUS, Principal
from .config import Settings, get_settings
from .errors import PolicyDenied
from .telemetry import get_logger

log = get_logger("sio.authz")


@dataclass(frozen=True)
class Decision:
    """The outcome of one authorisation check, with the reason it went that way."""

    allowed: bool
    action: str
    resource: str
    reason: str
    rule: str = ""
    principal: str = ""
    tenant: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "rule": self.rule,
            "principal": self.principal,
            "tenant": self.tenant,
        }


@dataclass(frozen=True)
class Rule:
    """One authorisation rule, in the form both engines can express.

    Deliberately narrow. A rule language rich enough to be interesting is one that cannot be faithfully
    generated into Rego, and the point of this table is that the two engines agree. Anything genuinely
    complex belongs in a Rego policy of its own, with `OpaPolicyEngine` as the only engine that can evaluate
    it — and the embedded engine must then refuse rather than approximate.
    """

    action: str
    """Action, or a `*` prefix pattern like `alerts.*`."""
    roles: tuple[str, ...] = ()
    """Roles that satisfy this rule. Empty means any authenticated principal."""
    min_clearance: int = 0
    requires_pii_scope: bool = False
    zone_scoped: bool = False
    """Whether the resource's zone must be within the principal's permitted zones."""
    description: str = ""

    def matches(self, action: str) -> bool:
        if self.action == "*":
            return True
        if self.action.endswith(".*"):
            return action.startswith(self.action[:-1])
        return self.action == action


#: The policy. One table, two engines, generated Rego.
#:
#: Ordered from most to least specific: the first matching rule decides, so a broad `alerts.*` must sit below
#: any narrower `alerts.delete`. Getting that order wrong is silent — the broad rule simply wins — which is
#: why `test_authz.py` asserts specificity ordering rather than trusting it.
POLICY: tuple[Rule, ...] = (
    # --- destructive and administrative ------------------------------------------------------
    Rule("admin.*", roles=("admin",), description="Administrative surfaces are admin-only"),
    Rule(
        "policy.write",
        roles=("admin",),
        min_clearance=3,
        description="Editing policy needs admin and high clearance: it is the lock on every other door",
    ),
    Rule("tenant.create", roles=("admin",), description="Creating tenants is administrative"),
    # --- acting on the world -----------------------------------------------------------------
    Rule(
        "decision.approve",
        roles=("commander", "admin"),
        min_clearance=2,
        description="Authorising an action in the physical world is a commander's decision",
    ),
    Rule(
        "decision.reject",
        roles=("operator", "commander", "admin"),
        description="Rejecting is safe: it results in nothing happening",
    ),
    Rule(
        "workflow.execute",
        roles=("commander", "admin"),
        min_clearance=2,
        description="Running a playbook can dispatch responders",
    ),
    Rule(
        "simulation.inject",
        roles=("operator", "commander", "admin", "ml_engineer"),
        description="Injecting a simulated incident affects no physical thing",
    ),
    # --- personal data -----------------------------------------------------------------------
    Rule(
        "pii.view",
        roles=("commander", "admin"),
        requires_pii_scope=True,
        min_clearance=2,
        description="Unredacted personal data needs both the role and the explicit scope",
    ),
    Rule(
        "media.raw",
        roles=("commander", "admin"),
        requires_pii_scope=True,
        description="Unblurred frames contain faces and plates",
    ),
    # --- ordinary operations -----------------------------------------------------------------
    Rule(
        "alerts.write",
        roles=("operator", "commander", "admin"),
        description="Acknowledging and resolving is an operator's job",
    ),
    Rule(
        "entities.read",
        zone_scoped=True,
        description="Anyone authenticated may read entities, within their permitted zones",
    ),
    Rule("events.read", zone_scoped=True, description="Reading events, within permitted zones"),
    Rule("alerts.read", zone_scoped=True, description="Reading alerts, within permitted zones"),
    Rule(
        "copilot.ask",
        roles=("operator", "commander", "admin", "ml_engineer", "integrator"),
        description="Asking questions; the answers are redacted unless pii.view also passes",
    ),
    Rule(
        "integration.write",
        roles=("integrator", "admin"),
        description="Registering connectors and webhooks",
    ),
    Rule(
        "model.write",
        roles=("ml_engineer", "admin"),
        description="Changing models and thresholds",
    ),
    # --- the floor ---------------------------------------------------------------------------
    Rule(
        "*.read",
        description="Any authenticated principal may read, unless a narrower rule said otherwise",
    ),
)


@runtime_checkable
class PolicyEngine(Protocol):
    """The port."""

    name: str

    def check(
        self,
        principal: Principal,
        action: str,
        resource: str = "",
        *,
        context: dict[str, Any] | None = None,
    ) -> Decision: ...


class EmbeddedPolicyEngine:
    """Evaluates `POLICY` directly. The default, and the one that always works.

    Chosen as the default over OPA deliberately: a governance layer that only functions when an extra binary
    is installed is a governance layer that gets switched off during setup and never switched back on.
    """

    name = "embedded"

    def __init__(self, policy: Iterable[Rule] = POLICY) -> None:
        self.rules = tuple(policy)

    def check(
        self,
        principal: Principal,
        action: str,
        resource: str = "",
        *,
        context: dict[str, Any] | None = None,
    ) -> Decision:
        context = context or {}

        def deny(reason: str, rule: str = "") -> Decision:
            return Decision(
                False, action, resource, reason, rule, principal.subject, principal.tenant_id
            )

        def allow(reason: str, rule: str = "") -> Decision:
            return Decision(
                True, action, resource, reason, rule, principal.subject, principal.tenant_id
            )

        if principal is ANONYMOUS or not principal.tenant_id:
            return deny("no authenticated principal")

        # TENANT FIRST, BEFORE THE ADMIN BYPASS. This ordering is the whole control.
        #
        # With the bypass first, an admin could read another tenant's data — measured, on the first run of
        # this engine: `check(admin, "entities.read", context={"tenant_id": "other"})` returned True. Cross
        # tenant leakage is the most serious failure a multi-tenant system has, and it is invisible: the
        # request succeeds, returns plausible data, and nothing in the logs looks unusual.
        #
        # An admin is an admin OF A TENANT, not of all tenants. There is deliberately no cross-tenant role
        # in this model; if one is ever needed it must be an explicit `platform_admin`, granted separately
        # and audited differently — never an accident of rule ordering.
        resource_tenant = (context or {}).get("tenant_id")
        if resource_tenant is not None and str(resource_tenant) != principal.tenant_id:
            return deny(
                f"that belongs to another tenant ({resource_tenant}); "
                f"you are authenticated against {principal.tenant_id}",
                "tenant-isolation",
            )

        # Admin bypass, stated explicitly rather than emerging from the rule table. An implicit bypass is
        # one nobody can find when they ask "how did that request succeed?".
        if principal.is_admin and not any(
            rule.requires_pii_scope for rule in self.rules if rule.matches(action)
        ):
            return allow("admin", rule="admin-bypass")

        rule = next((candidate for candidate in self.rules if candidate.matches(action)), None)
        if rule is None:
            # Default deny. An action nobody wrote a rule for is not an action anybody may perform, and the
            # message names the action so the fix is obvious.
            return deny(f"no rule permits {action!r}; the default is deny")

        if rule.roles and not principal.has_any(*rule.roles):
            return deny(
                f"{action} needs one of: {', '.join(sorted(rule.roles))}; "
                f"you have {', '.join(sorted(principal.roles)) or 'no roles'}",
                rule.action,
            )
        if principal.clearance < rule.min_clearance:
            return deny(
                f"{action} needs clearance {rule.min_clearance}; yours is {principal.clearance}",
                rule.action,
            )
        if rule.requires_pii_scope and not principal.pii_scope:
            return deny(
                f"{action} needs the pii_scope claim, which your token does not carry",
                rule.action,
            )
        if rule.zone_scoped:
            zone = context.get("zone_id")
            if zone is not None and not principal.may_see_zone(str(zone)):
                return deny(
                    f"you may not see {zone}; your token permits "
                    f"{', '.join(sorted(principal.zones))}",
                    rule.action,
                )

        # Tenant was already checked, above the admin bypass, where it has to be.
        return allow(rule.description or f"permitted by {rule.action}", rule.action)


class OpaPolicyEngine:
    """Delegates to a local OPA binary, and refuses rather than guessing when it is absent.

    Falling back to the embedded engine on failure would be the wrong instinct: an operator who set
    `SIO_POLICY_ENGINE=opa` did so to have OPA's answers, and silently substituting different ones is worse
    than an outage because nobody learns it happened. So an unreachable OPA is a **denial**, loudly.
    """

    name = "opa"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.url = getattr(self.settings, "opa_url", "http://127.0.0.1:8181")
        self.path = "sio/authz/allow"
        self._fallback = EmbeddedPolicyEngine()
        self._warned = False

    def check(
        self,
        principal: Principal,
        action: str,
        resource: str = "",
        *,
        context: dict[str, Any] | None = None,
    ) -> Decision:
        import httpx

        payload = {
            "input": {
                "principal": {
                    "subject": principal.subject,
                    "tenant": principal.tenant_id,
                    "roles": sorted(principal.roles),
                    "clearance": principal.clearance,
                    "zones": sorted(principal.zones),
                    "pii_scope": principal.pii_scope,
                },
                "action": action,
                "resource": resource,
                "context": context or {},
            }
        }
        try:
            response = httpx.post(f"{self.url}/v1/data/{self.path}", json=payload, timeout=3.0)
            response.raise_for_status()
            allowed = bool(response.json().get("result", False))
        except Exception as exc:
            if not self._warned:
                log.error(
                    "authz.opa_unreachable",
                    url=self.url,
                    error=type(exc).__name__,
                    consequence="denying; start OPA or set SIO_POLICY_ENGINE=embedded",
                )
                self._warned = True
            return Decision(
                False,
                action,
                resource,
                "the policy engine (OPA) is unreachable, so this request is denied rather than "
                "evaluated against different rules",
                "opa-unreachable",
                principal.subject,
                principal.tenant_id,
            )
        # OPA answers yes or no; the *reason* comes from the shared table, so a denial is still explicable.
        # Without this an OPA denial would read "denied by policy", which tells an operator nothing.
        local = self._fallback.check(principal, action, resource, context=context)
        return Decision(
            allowed,
            action,
            resource,
            local.reason if allowed == local.allowed else f"decided by OPA at {self.url}",
            local.rule,
            principal.subject,
            principal.tenant_id,
        )


def build_policy_engine(settings: Settings | None = None) -> PolicyEngine:
    settings = settings or get_settings()
    if settings.policy_engine == "opa":
        log.info("authz.backend", engine="opa")
        return OpaPolicyEngine(settings)
    log.info("authz.backend", engine="embedded", rules=len(POLICY))
    return EmbeddedPolicyEngine()


# ------------------------------------------------------------------------------- enforcement
_ENGINE: PolicyEngine | None = None
AuditHook = Callable[[Decision], None]
_AUDIT_HOOKS: list[AuditHook] = []


def policy_engine() -> PolicyEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = build_policy_engine()
    return _ENGINE


def set_policy_engine(engine: PolicyEngine | None) -> None:
    """Override the engine, for tests and for a service that wants a specific one."""
    global _ENGINE
    _ENGINE = engine


def on_decision(hook: AuditHook) -> None:
    """Register an audit sink. Called for allows as well as denials.

    Both, because an audit trail of denials answers "who was stopped" and one of allows answers "who did
    this" — and the second is the question actually asked after an incident.
    """
    _AUDIT_HOOKS.append(hook)


def clear_decision_hooks() -> None:
    _AUDIT_HOOKS.clear()


def authorise(
    principal: Principal,
    action: str,
    resource: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> Decision:
    """Check, audit, and return. Does not raise — callers that want an exception use `require`."""
    decision = policy_engine().check(principal, action, resource, context=context)
    for hook in _AUDIT_HOOKS:
        try:
            hook(decision)
        except Exception as exc:
            # An audit sink must never be able to deny a request by failing, nor to let one through. Log and
            # continue: the decision has already been made, and losing the record is a lesser evil than
            # having the outcome depend on the recorder.
            log.warning("authz.audit_hook_failed", error=type(exc).__name__, detail=str(exc)[:120])
    if not decision.allowed:
        log.info(
            "authz.denied",
            action=action,
            resource=resource,
            principal=principal.subject,
            tenant=principal.tenant_id,
            reason=decision.reason,
        )
    return decision


def require(
    principal: Principal,
    action: str,
    resource: str = "",
    *,
    context: dict[str, Any] | None = None,
) -> Decision:
    """Authorise or raise `PolicyDenied`."""
    decision = authorise(principal, action, resource, context=context)
    if not decision.allowed:
        raise PolicyDenied(action, resource or action, decision.reason)
    return decision


def rego_from_policy(policy: Iterable[Rule] = POLICY) -> str:
    """Generate the Rego that expresses `POLICY`.

    Generated, not hand-written, because two independent implementations of an authorisation policy will
    drift and the drift will be a permissions difference between environments. `just policies` writes this to
    `infra/opa/policies/sio.rego` and a test asserts the file matches, so a rule added here and not
    regenerated fails CI instead of diverging quietly.
    """
    lines = [
        "# GENERATED by sio_core.authz.rego_from_policy — do not edit.",
        "#",
        "# Regenerate with: just policies",
        "#",
        "# Hand-editing this file would recreate the exact problem it exists to prevent: two",
        "# implementations of one policy, drifting, until dev allows what production denies.",
        f"# Generated {datetime.now(UTC).strftime('%Y-%m-%d')} from {len(tuple(policy))} rules.",
        "",
        "package sio.authz",
        "",
        "import rego.v1",
        "",
        "default allow := false",
        "",
        "# An authenticated principal is one with a tenant.",
        'authenticated if input.principal.tenant != ""',
        "",
        "# Admin bypass, except where a rule demands the pii_scope claim.",
        "#",
        "# tenant_matches is asserted HERE, not only in the per-rule blocks below. Omitting it let an",
        "# admin read another tenant's data — which is exactly what the Python engine did on its first",
        "# run, before the check was moved above the bypass. An admin is an admin OF A TENANT; there is",
        "# deliberately no cross-tenant role in this model.",
        "allow if {",
        "\tauthenticated",
        '\t"admin" in input.principal.roles',
        "\tnot pii_rule_applies",
        "\ttenant_matches",
        "}",
        "",
    ]

    pii_actions = [rule.action for rule in policy if rule.requires_pii_scope]
    lines.append("pii_rule_applies if {")
    if pii_actions:
        lines.append(f"\tinput.action in {json_list(pii_actions)}")
    else:
        lines.append("\tfalse")
    lines.extend(["}", ""])

    for index, rule in enumerate(policy):
        lines.append(f"# {rule.description or rule.action}")
        lines.append("allow if {")
        lines.append("\tauthenticated")
        lines.append(f"\t{_rego_action_match(rule.action)}")
        if rule.roles:
            lines.append(f"\tsome role in {json_list(list(rule.roles))}")
            lines.append("\trole in input.principal.roles")
        if rule.min_clearance:
            lines.append(f"\tinput.principal.clearance >= {rule.min_clearance}")
        if rule.requires_pii_scope:
            lines.append("\tinput.principal.pii_scope == true")
        if rule.zone_scoped:
            lines.append("\tzone_permitted")
        lines.append("\ttenant_matches")
        lines.append("}")
        lines.append("")
        _ = index

    lines.extend(
        [
            "# An empty zone list is UNRESTRICTED, not 'no zones'. Inverting this would lock out every",
            "# ordinary operator while looking like a tightening.",
            "zone_permitted if count(input.principal.zones) == 0",
            "zone_permitted if not input.context.zone_id",
            "zone_permitted if input.context.zone_id in input.principal.zones",
            "",
            "tenant_matches if not input.context.tenant_id",
            "tenant_matches if input.context.tenant_id == input.principal.tenant",
            "",
        ]
    )
    return "\n".join(lines)


def json_list(values: list[str]) -> str:
    import json

    return json.dumps(values)


def _rego_action_match(action: str) -> str:
    if action == "*":
        return "true"
    if action.endswith(".*"):
        return f'startswith(input.action, "{action[:-1]}")'
    if action.startswith("*."):
        return f'endswith(input.action, "{action[1:]}")'
    return f'input.action == "{action}"'


__all__ = [
    "POLICY",
    "Decision",
    "EmbeddedPolicyEngine",
    "OpaPolicyEngine",
    "PolicyEngine",
    "Rule",
    "authorise",
    "build_policy_engine",
    "clear_decision_hooks",
    "on_decision",
    "policy_engine",
    "rego_from_policy",
    "require",
    "set_policy_engine",
]
