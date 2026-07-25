"""Authorisation (PRD M18, Phase 5).

The load-bearing property of this module is not "the rules are right" — rules change. It is that **the two
engines agree**. The PRD wants Rego policies; the platform must also run with no OPA binary present. Two
hand-written implementations of one authorisation policy will drift, and the drift is a permission difference
between environments, which is the class of bug that only shows up in production.

So `POLICY` is the single source, the Rego is generated from it, and these tests attack the seam.

Two bugs found here that both had the same shape — an ordering mistake in a security control, silent by
construction:

1. **The admin bypass sat above the tenant check**, so an admin could read another tenant's data.
2. **The generated Rego did not reproduce first-match-wins**, so a zoned operator was denied by the embedded
   engine and allowed by OPA.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sio_core.authn import ANONYMOUS, Principal
from sio_core.authz import POLICY, EmbeddedPolicyEngine, Rule, rego_from_policy

ROOT = Path(__file__).resolve().parents[2]
REGO_PATH = ROOT / "infra" / "opa" / "policies" / "sio.rego"


def a_principal(**kwargs) -> Principal:
    defaults = {
        "subject": "tester",
        "tenant_id": "acme",
        "roles": frozenset({"operator"}),
        "clearance": 1,
    }
    defaults.update(kwargs)
    return Principal(**defaults)  # type: ignore[arg-type]


#: The decision matrix both engines must agree on.
#:
#: Deliberately includes the awkward combinations rather than the obvious ones: an admin reaching across
#: tenants, an admin without the PII scope, a zoned operator inside and outside their zones, a commander
#: without clearance. The obvious cases were never the ones that broke.
PRINCIPALS: dict[str, Principal] = {
    "operator": a_principal(),
    "operator_zoned": a_principal(zones=frozenset({"dock_1"})),
    "commander": a_principal(roles=frozenset({"commander"}), clearance=2),
    "commander_no_clearance": a_principal(roles=frozenset({"commander"}), clearance=0),
    "commander_pii": a_principal(roles=frozenset({"commander"}), clearance=2, pii_scope=True),
    "admin": a_principal(roles=frozenset({"admin"}), clearance=3),
    "admin_pii": a_principal(roles=frozenset({"admin"}), clearance=3, pii_scope=True),
    "integrator": a_principal(roles=frozenset({"integrator"})),
    "viewer": a_principal(roles=frozenset({"viewer"})),
}

ACTIONS: tuple[str, ...] = (
    "entities.read",
    "events.read",
    "alerts.read",
    "alerts.write",
    "decision.approve",
    "decision.reject",
    "workflow.execute",
    "simulation.inject",
    "pii.view",
    "media.raw",
    "copilot.ask",
    "integration.write",
    "model.write",
    "admin.reset",
    "policy.write",
    "tenant.create",
    "forecasts.read",
    "unmapped.request",
)

CONTEXTS: tuple[tuple[str, dict], ...] = (
    ("no context", {}),
    ("own tenant", {"tenant_id": "acme"}),
    ("other tenant", {"tenant_id": "other"}),
    ("zone dock_1", {"zone_id": "dock_1"}),
    ("zone fuel_store", {"zone_id": "fuel_store"}),
)


# --- the two bugs -------------------------------------------------------------------------------
def test_an_admin_cannot_read_another_tenant() -> None:
    """The first bug, found by printing decisions rather than reasoning about the code.

    `check(admin, "entities.read", {"tenant_id": "other"})` returned True, because the admin bypass sat
    above the tenant check. Cross-tenant leakage is the most serious failure a multi-tenant system has and it
    is invisible: the request succeeds, returns plausible data, and nothing in the logs looks unusual.

    An admin is an admin OF A TENANT. There is deliberately no cross-tenant role; if one is ever needed it
    must be an explicit `platform_admin`, granted separately and audited differently — never an accident of
    rule ordering.
    """
    engine = EmbeddedPolicyEngine()
    decision = engine.check(PRINCIPALS["admin"], "entities.read", context={"tenant_id": "other"})
    assert not decision.allowed
    assert "another tenant" in decision.reason
    assert decision.rule == "tenant-isolation"
    # And the same principal on their own tenant is fine, or the fix would be a lockout.
    assert engine.check(PRINCIPALS["admin"], "entities.read", context={"tenant_id": "acme"}).allowed


def test_no_role_can_read_another_tenant() -> None:
    """Not just admin: every principal, every action."""
    engine = EmbeddedPolicyEngine()
    for name, principal in PRINCIPALS.items():
        for action in ACTIONS:
            decision = engine.check(principal, action, context={"tenant_id": "other"})
            assert not decision.allowed, f"{name} was allowed {action} across tenants"


def test_the_generated_rego_reproduces_first_match_wins() -> None:
    """The second bug: Rego's `allow` is a union, so ANY matching rule allows.

    The Python engine applies the FIRST matching rule. For `entities.read` both the zone-scoped
    `entities.read` rule and the unconstrained catch-all `*.read` matched — so a zoned operator asking about
    a zone outside their token was DENIED by the embedded engine and ALLOWED by OPA. A permission difference
    between environments, produced by the very generator written to prevent one.
    """
    rego = rego_from_policy()
    catch_all = rego[rego.index("# Any authenticated principal may read") :]
    block = catch_all[: catch_all.index("}")]
    # Every action an earlier rule claims must be excluded from the catch-all, or the catch-all silently
    # grants it without the earlier rule's constraints.
    for action in ("entities.read", "events.read", "alerts.read"):
        assert f'input.action != "{action}"' in block, (
            f"the catch-all does not exclude {action}, so its zone check can be bypassed"
        )


def test_no_rule_precedes_a_catch_all() -> None:
    """A `*` rule anywhere but last makes every rule after it unreachable.

    Unreachable authorisation rules are worse than absent ones: they read as protection while granting
    nothing, so a reviewer checking "is X restricted?" finds a rule and stops looking.
    """
    for index, rule in enumerate(POLICY[:-1]):
        assert rule.action != "*", (
            f"rule {index} is a bare catch-all with {len(POLICY) - index - 1} after it"
        )


def test_rules_are_ordered_specific_before_general() -> None:
    """First match wins, so a broad rule above a narrow one silently wins.

    Checked structurally rather than trusted: for every pair, if an earlier rule's pattern matches a later
    rule's exact action, the earlier one must not be *less* restrictive — otherwise the later rule is dead.
    """
    for index, general in enumerate(POLICY):
        for specific in POLICY[index + 1 :]:
            if not general.matches(specific.action):
                continue
            general_is_looser = (
                not general.roles and general.min_clearance == 0 and not general.requires_pii_scope
            )
            specific_is_tighter = bool(
                specific.roles or specific.min_clearance or specific.requires_pii_scope
            )
            assert not (general_is_looser and specific_is_tighter), (
                f"{general.action!r} precedes {specific.action!r} and is looser, "
                f"so {specific.action!r} can never apply"
            )


# --- ordinary decisions -------------------------------------------------------------------------
def test_anonymous_is_denied_everything() -> None:
    engine = EmbeddedPolicyEngine()
    for action in ACTIONS:
        assert not engine.check(ANONYMOUS, action).allowed


def test_a_principal_without_a_tenant_is_not_authenticated() -> None:
    """A token with no tenant claim is refused at the authn layer; this is the second line."""
    engine = EmbeddedPolicyEngine()
    assert not engine.check(a_principal(tenant_id=""), "entities.read").allowed


def test_approving_an_action_needs_a_commander() -> None:
    """The most consequential action in the platform: it authorises something physical."""
    engine = EmbeddedPolicyEngine()
    assert not engine.check(PRINCIPALS["operator"], "decision.approve").allowed
    assert engine.check(PRINCIPALS["commander"], "decision.approve").allowed
    assert not engine.check(PRINCIPALS["commander_no_clearance"], "decision.approve").allowed


def test_rejecting_is_safer_than_approving_and_the_policy_says_so() -> None:
    """Rejecting results in nothing happening, so it needs less authority. Asymmetry on purpose."""
    engine = EmbeddedPolicyEngine()
    assert engine.check(PRINCIPALS["operator"], "decision.reject").allowed
    assert not engine.check(PRINCIPALS["operator"], "decision.approve").allowed


def test_pii_needs_both_a_role_and_the_scope_claim() -> None:
    """Two independent things, and the admin bypass deliberately does not cover it.

    A role is granted once and forgotten; a scope claim is minted per token. Requiring both means seeing
    personal data is a decision made at issuing time rather than a standing property of a job title.
    """
    engine = EmbeddedPolicyEngine()
    assert not engine.check(PRINCIPALS["admin"], "pii.view").allowed
    assert engine.check(PRINCIPALS["admin_pii"], "pii.view").allowed
    assert engine.check(PRINCIPALS["commander_pii"], "pii.view").allowed
    assert not engine.check(PRINCIPALS["commander"], "pii.view").allowed


def test_a_zoned_principal_is_confined_to_their_zones() -> None:
    engine = EmbeddedPolicyEngine()
    zoned = PRINCIPALS["operator_zoned"]
    assert engine.check(zoned, "entities.read", context={"zone_id": "dock_1"}).allowed
    denied = engine.check(zoned, "entities.read", context={"zone_id": "fuel_store"})
    assert not denied.allowed
    assert "fuel_store" in denied.reason


def test_an_empty_zone_set_is_unrestricted() -> None:
    """The inversion is easy to get backwards, and getting it backwards looks like a tightening.

    A principal with no zone restriction is the common case. Treating an empty set as "permitted nowhere"
    would lock out every ordinary operator.
    """
    engine = EmbeddedPolicyEngine()
    assert engine.check(
        PRINCIPALS["operator"], "entities.read", context={"zone_id": "anywhere"}
    ).allowed


def test_an_unknown_action_is_denied_and_says_so() -> None:
    """Default deny, with the action named so the fix is obvious."""
    engine = EmbeddedPolicyEngine()
    decision = engine.check(PRINCIPALS["admin"], "something.nobody.wrote")
    assert not decision.allowed
    assert "something.nobody.wrote" in decision.reason


def test_every_denial_explains_itself_usefully() -> None:
    """A decision an operator cannot understand is one they route around by acquiring a broader role."""
    engine = EmbeddedPolicyEngine()
    denials = [
        engine.check(principal, action, context=dict(context))
        for principal in PRINCIPALS.values()
        for action in ACTIONS
        for _, context in CONTEXTS
    ]
    for decision in (d for d in denials if not d.allowed):
        assert decision.reason, f"{decision.action} denied with no reason"
        assert len(decision.reason) > 15, f"unhelpfully terse: {decision.reason!r}"


def test_the_checked_in_rego_matches_the_policy_table() -> None:
    """The whole point of generating it.

    A rule added in Python and not regenerated fails here rather than diverging quietly. Only the generated
    date line may differ, since regenerating on a later day must not fail CI.
    """
    assert REGO_PATH.exists(), f"{REGO_PATH} is missing; run: just policies"
    on_disk = REGO_PATH.read_text().splitlines()
    generated = rego_from_policy().splitlines()

    def without_date(lines: list[str]) -> list[str]:
        return [line for line in lines if not line.startswith("# Generated 20")]

    assert without_date(on_disk) == without_date(generated), (
        "infra/opa/policies/sio.rego is out of date with POLICY — run: just policies"
    )


# --- conformance, when OPA is present -----------------------------------------------------------
@pytest.mark.skipif(shutil.which("opa") is None, reason="the opa binary is not installed")
def test_opa_and_the_embedded_engine_agree(tmp_path: Path) -> None:
    """The real conformance check: every principal x action x context, both engines, same answer.

    Skipped without the binary rather than faked. A test that pretends to check conformance is worse than
    one that admits it cannot, because the first is believed.
    """
    engine = EmbeddedPolicyEngine()
    policy_file = tmp_path / "sio.rego"
    policy_file.write_text(rego_from_policy())

    disagreements: list[str] = []
    for name, principal in PRINCIPALS.items():
        for action in ACTIONS:
            for label, context in CONTEXTS:
                payload = {
                    "principal": {
                        "subject": principal.subject,
                        "tenant": principal.tenant_id,
                        "roles": sorted(principal.roles),
                        "clearance": principal.clearance,
                        "zones": sorted(principal.zones),
                        "pii_scope": principal.pii_scope,
                    },
                    "action": action,
                    "resource": "r",
                    "context": context,
                }
                result = subprocess.run(
                    [
                        "opa",
                        "eval",
                        "--data",
                        str(policy_file),
                        "--stdin-input",
                        "--format",
                        "raw",
                        "data.sio.authz.allow",
                    ],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                opa_says = result.stdout.strip() == "true"
                mine = engine.check(principal, action, "r", context=dict(context)).allowed
                if opa_says != mine:
                    disagreements.append(
                        f"{name} / {action} / {label}: embedded={mine} opa={opa_says}"
                    )

    assert not disagreements, "the two engines disagree:\n" + "\n".join(disagreements)


def test_the_conformance_matrix_is_not_trivially_small() -> None:
    """A conformance test over three cases proves nothing. This asserts the matrix has teeth."""
    assert len(PRINCIPALS) >= 8
    assert len(ACTIONS) >= 15
    assert len(CONTEXTS) >= 4
    assert len(PRINCIPALS) * len(ACTIONS) * len(CONTEXTS) >= 500


def test_a_custom_policy_can_be_supplied() -> None:
    """The engine takes its rules as an argument, so a deployment can narrow them without a fork."""
    strict = EmbeddedPolicyEngine([Rule("entities.read", roles=("admin",))])
    assert not strict.check(PRINCIPALS["operator"], "entities.read").allowed
    assert strict.check(PRINCIPALS["admin"], "entities.read").allowed
    assert not strict.check(PRINCIPALS["operator"], "alerts.read").allowed, "default deny"
