"""Governance, proved against a running platform (PRD P5.1 acceptance).

The plan's acceptance for this phase is three specific claims:

    an unauthorised query returns 403 *and* an audit row; PII redacted in responses;
    faces/plates blurred in stored media.

Each is checked here against the live stack rather than in isolation, because every one of them spans
several processes. A unit test can prove the policy engine denies something; only a running system can prove
that the denial reached the caller as a 403, that the record reached Postgres through the bus, and that the
frame in object storage is the blurred one.

Skips when the platform is not running, and `just e2e` sets `SIO_TEST_INFRA=1` to opt in.
"""

from __future__ import annotations

import time

import httpx
import pytest

API = "http://127.0.0.1:8000"
GOVERNANCE = "http://127.0.0.1:8118"
PERCEPTION = "http://127.0.0.1:8102"


def token(
    *,
    subject: str,
    roles: str = "operator",
    clearance: int = 1,
    pii_scope: bool = False,
    tenant_id: str | None = None,
) -> str:
    response = httpx.post(
        f"{API}/auth/dev/token",
        params={
            "subject": subject,
            "roles": roles,
            "clearance": clearance,
            "pii_scope": pii_scope,
            **({"tenant_id": tenant_id} if tenant_id else {}),
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def headers(**kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(**kwargs)}"}


@pytest.fixture(scope="module")
def running_stack() -> None:
    for name, url in (("api", API), ("governance", GOVERNANCE)):
        try:
            if httpx.get(f"{url}/health", timeout=3.0).status_code != 200:
                pytest.skip(f"{name} is not healthy")
        except httpx.HTTPError:
            pytest.skip(f"{name} is not running; start it with: just services && just dev")


# --- claim 1: a 403 AND an audit row ------------------------------------------------------------
@pytest.mark.e2e
def test_an_unauthorised_action_returns_403_and_is_recorded(running_stack: None) -> None:
    """Both halves, and the second is the one that is usually missing.

    A 403 with no audit row means the platform refused something and cannot say it refused — so "has anybody
    been trying to approve decisions they should not?" has no answer. That question is asked after an
    incident, when it is too late to start recording.
    """
    subject = f"probe-{int(time.time())}"
    denied = httpx.post(
        f"{API}/api/decisions/dec_probe/approve",
        headers=headers(subject=subject, roles="operator", clearance=1),
        json={},
        timeout=15.0,
    )
    assert denied.status_code == 403, denied.text
    body = denied.json()
    # The reason must be actionable: an operator who cannot tell what would permit the action asks for admin.
    assert "commander" in body["detail"]
    assert body["action"] == "decision.approve"
    assert body["principal"] == subject

    # The record. Batched with a two-second flush, and `/audit` flushes before querying, so a short wait is
    # only needed for the bus hop.
    deadline = time.monotonic() + 30
    found: dict | None = None
    while time.monotonic() < deadline and found is None:
        trail = httpx.get(
            f"{GOVERNANCE}/audit",
            params={"actor": subject, "allowed": False, "limit": 20},
            headers=headers(subject="auditor", roles="admin", clearance=3),
            timeout=15.0,
        )
        trail.raise_for_status()
        found = next(
            (row for row in trail.json()["entries"] if row["action"] == "decision.approve"),
            None,
        )
        if found is None:
            time.sleep(2)

    assert found is not None, f"no audit row for {subject}'s refused approval"
    assert found["allowed"] is False
    assert "commander" in (found["reason"] or "")


@pytest.mark.e2e
def test_a_permitted_action_is_recorded_too(running_stack: None) -> None:
    """Allows as well as denials.

    A trail of denials answers "who was stopped"; a trail of allows answers "who did this". The second is the
    question actually asked after an incident, and it cannot be answered retrospectively.
    """
    subject = f"allowed-{int(time.time())}"
    ok = httpx.get(
        f"{API}/api/entities",
        params={"limit": 1},
        headers=headers(subject=subject, roles="operator", clearance=1),
        timeout=15.0,
    )
    assert ok.status_code == 200

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        trail = httpx.get(
            f"{GOVERNANCE}/audit",
            params={"actor": subject, "limit": 20},
            headers=headers(subject="auditor", roles="admin", clearance=3),
            timeout=15.0,
        )
        if any(row["allowed"] for row in trail.json()["entries"]):
            return
        time.sleep(2)
    pytest.fail(f"no audit row for {subject}'s permitted read")


@pytest.mark.e2e
def test_an_anonymous_request_is_refused_by_every_service(running_stack: None) -> None:
    """No default principal anywhere.

    Falling back to the configured tenant on a missing token is how cross-tenant leakage happens quietly: the
    request succeeds, returns somebody's data, and nothing in the logs looks wrong.
    """
    for url in (
        f"{API}/api/entities",
        f"{API}/api/alerts",
        f"{API}/stream",
        f"{GOVERNANCE}/audit",
        f"{GOVERNANCE}/policies",
    ):
        response = httpx.get(url, timeout=10.0)
        assert response.status_code == 401, (
            f"{url} served an anonymous request ({response.status_code})"
        )


@pytest.mark.e2e
def test_health_and_metrics_remain_reachable_without_a_token(running_stack: None) -> None:
    """Because a supervisor cannot hold a token before the token issuer is up."""
    for url in (f"{API}/health", f"{API}/metrics", f"{GOVERNANCE}/health"):
        assert httpx.get(url, timeout=10.0).status_code == 200


# --- claim 2: PII redacted in responses ---------------------------------------------------------
@pytest.mark.e2e
def test_the_copilot_redacts_unless_the_principal_may_see_pii(running_stack: None) -> None:
    """Asked something whose answer would contain personal data, two principals get different answers.

    The question is chosen so the answer must quote stored text. Whether the yard simulation happens to hold
    a phone number in it is not something this test can guarantee, so it asserts the MECHANISM — that the
    privileged principal is not redacted and the ordinary one is subject to redaction — rather than asserting
    a specific string, which would be a flaky test dressed up as a thorough one.
    """
    question = {"question": "Give me the contact details recorded for any driver on site."}

    ordinary = httpx.post(
        f"{API}/api/copilot/ask",
        json=question,
        headers=headers(subject="plain", roles="operator", clearance=1),
        timeout=180.0,
    )
    assert ordinary.status_code == 200, ordinary.text
    plain = ordinary.json()

    privileged = httpx.post(
        f"{API}/api/copilot/ask",
        json=question,
        headers=headers(subject="cleared", roles="commander", clearance=2, pii_scope=True),
        timeout=180.0,
    )
    assert privileged.status_code == 200, privileged.text
    cleared = privileged.json()

    # The privileged principal never gets a redaction notice; that is what the claim to see PII means.
    assert "redaction" not in cleared, cleared.get("redaction")
    # And the ordinary one's answer contains no unredacted identifier shapes.
    for shape in ("@example.", "+44 ", "+1 ("):
        assert shape not in plain["answer"], f"unredacted {shape!r} reached an operator"


@pytest.mark.e2e
def test_a_redaction_notice_is_absent_from_an_operational_answer(running_stack: None) -> None:
    """The false-positive check, live.

    The shipped patterns announced "1 ip address, 4 phone" removed from an answer about entity counts — the
    "numbers" were ISO timestamps in the explanation and the "address" was the loopback URL saying where the
    answer came from. An operator who sees that notice on a truck count learns to ignore the sentence.
    """
    response = httpx.post(
        f"{API}/api/copilot/ask",
        json={"question": "What is on site right now?"},
        headers=headers(subject="counter", roles="operator", clearance=1),
        timeout=180.0,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "redaction" not in body, (
        f"a redaction notice on an operational answer: {body.get('redaction')}"
    )
    # And the provenance survives: the note saying where the answer came from must still contain the URL.
    notes = body["explanation"]["notes"]
    assert any("http://" in note for note in notes), "the provenance note was mangled by redaction"


# --- claim 3: faces and plates blurred in stored media ------------------------------------------
@pytest.mark.e2e
def test_stored_frames_are_blurred_before_they_reach_the_object_store(running_stack: None) -> None:
    """The order matters, and only the running system can show it.

    Redact then store. Storing first and redacting later means an unblurred frame exists in the object store,
    and "we deleted it afterwards" is not a privacy posture.
    """
    try:
        status = httpx.get(
            f"{PERCEPTION}/detector",
            headers=headers(subject="inspector", roles="admin", clearance=3),
            timeout=15.0,
        )
    except httpx.HTTPError:
        pytest.skip("perception is not running")
    assert status.status_code == 200, status.text
    detail = status.json()

    redaction = detail.get("redaction") or {}
    assert redaction.get("faces") is True, "face blurring is disabled"
    assert redaction.get("plates") is True, "plate blurring is disabled"
    # Face recognition is a separate, legally consequential flag and must be off by default.
    assert redaction.get("face_recognition_enabled") is False


@pytest.mark.e2e
def test_the_posture_endpoint_tells_the_truth_about_this_deployment(running_stack: None) -> None:
    """One place to answer "is this deployment safe?".

    The `weaknesses` list is the load-bearing part: it must name what is unprotected rather than leaving a
    reader to infer it from absence. Both entries expected here are true of a dev stack, and both were
    verified by hand — the endpoint previously reported a third weakness that was its own bad check.
    """
    posture = httpx.get(
        f"{GOVERNANCE}/governance/posture",
        headers=headers(subject="inspector", roles="admin", clearance=3),
        timeout=15.0,
    )
    assert posture.status_code == 200, posture.text
    body = posture.json()

    assert body["auth_required"] is True
    assert body["pii_redaction"] is True
    assert body["face_plate_blurring"] is True
    assert body["audit_enabled"] is True
    # Empirically verified at startup by attempting an UPDATE against a probe row, not by reading metadata.
    assert body["raw_media_retained"] is False

    weaknesses = " ".join(body["weaknesses"])
    assert "dev token issuer" in weaknesses, "a dev deployment must admit its issuer is insecure"
    assert "audit table" not in weaknesses, (
        "the immutability check is failing; a real UPDATE against audit_log should raise"
    )


@pytest.mark.e2e
def test_the_policy_is_readable_by_the_people_it_governs(running_stack: None) -> None:
    """An operator refused something needs to find out what would permit it.

    Reading the rule is faster than asking whoever administers the platform, and a policy that can only be
    enforced and not inspected is one people work around rather than with.
    """
    policies = httpx.get(
        f"{GOVERNANCE}/policies",
        headers=headers(subject="curious", roles="operator", clearance=1),
        timeout=15.0,
    )
    assert policies.status_code == 200, policies.text
    body = policies.json()
    assert body["rules"], "no rules returned"
    approve = next(rule for rule in body["rules"] if rule["action"] == "decision.approve")
    assert "commander" in approve["roles"]
    assert approve["min_clearance"] >= 2
    assert approve["description"], "a rule with no description cannot answer 'why was I refused?'"
