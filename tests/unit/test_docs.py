"""The documented commands must exist (PRD P4.7 ship checkpoint).

The P4.7 acceptance is that a reviewer clones fresh, follows the quickstart, and gets the demo. That failed
once already for the cheapest possible reason: the quickstart listed `just dev` then `just demo` with no
`just seed` between them, so a reader following it exactly got "no zones. Run: just seed". A quickstart that
does not work as written is worse than none — the reader reasonably concludes the build is broken rather
than the documentation.

These tests are the guard. They are deliberately shallow: they check that every command the docs tell a
reader to type exists, not that it does the right thing. That is the failure mode documentation actually
has — commands get renamed and prose does not follow — and it is the one a reader cannot work around.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", ROOT / "docs" / "DEMO.md"]


def justfile_recipes() -> set[str]:
    """Recipe names, parsed from the Justfile.

    A recipe line starts at column zero and ends in a colon; `just --summary` would need `just` installed,
    and this test must pass in an environment that has only Python.
    """
    names: set[str] = set()
    for line in (ROOT / "Justfile").read_text().splitlines():
        match = re.match(r"^([a-z][a-z0-9-]*)(\s+[^:]*)?:", line)
        if match:
            names.add(match.group(1))
    return names


def documented_commands(path: Path) -> set[str]:
    """`just <recipe>` mentions in a document, from code blocks and prose alike.

    Prose counts. "Then run `just doctor`" is an instruction a reader will follow, and it rots exactly as
    readily as one inside a fenced block.
    """
    text = path.read_text()
    return {
        match.group(1)
        for match in re.finditer(r"just\s+([a-z][a-z0-9-]*)", text)
        # `just check` inside a sentence like "it is just checked" would be a false positive, but the
        # pattern requires the literal word `just` followed by a recipe-shaped token, and the recipe set
        # below filters the rest.
    }


def test_the_justfile_parses() -> None:
    recipes = justfile_recipes()
    assert len(recipes) > 15, f"only found {len(recipes)} recipes; the parser is probably wrong"
    for expected in ("setup", "check", "dev", "demo", "doctor", "services"):
        assert expected in recipes, f"{expected!r} is missing from the Justfile"


@pytest.mark.parametrize("path", DOCS, ids=lambda path: path.name)
def test_every_documented_command_exists(path: Path) -> None:
    """The failure this file exists for.

    A reader typing a command that does not exist has been actively misled, and it costs them their trust in
    everything else the document says.
    """
    recipes = justfile_recipes()
    mentioned = documented_commands(path)
    # Words that follow "just" in prose and are not commands.
    prose = {"a", "the", "as", "run", "it", "one", "before", "check-in"}
    missing = sorted(name for name in mentioned - prose if name not in recipes)
    assert not missing, (
        f"{path.name} tells the reader to run commands that do not exist: {missing}\n"
        f"available: {sorted(recipes)}"
    )


def test_the_quickstart_gets_to_a_running_demo() -> None:
    """The quickstart must name every step needed, in order, to reach the demo.

    Not a style check. This is the sequence that failed: `just dev` straight to `just demo` with nothing
    seeding the site in between. `just demo` now seeds when it needs to, which is the real fix — this test
    pins the ordering so a future edit cannot reintroduce a gap.
    """
    text = (ROOT / "README.md").read_text()
    quickstart = text[text.index("## Quickstart") : text.index("## What you get")]
    for command in ("just setup", "just services", "just dev", "just demo"):
        assert command in quickstart, f"the quickstart never tells the reader to run {command!r}"
    assert quickstart.index("just setup") < quickstart.index("just services")
    assert quickstart.index("just services") < quickstart.index("just dev")
    assert quickstart.index("just dev") < quickstart.index("just demo")


def test_the_demo_script_is_documented() -> None:
    """`just demo` is a deliverable, so it needs the document that makes it presentable."""
    demo = ROOT / "docs" / "DEMO.md"
    assert demo.exists()
    text = demo.read_text()
    # The three things a presenter cannot improvise.
    assert "what to say if it is slow" in text.lower(), "no note on slowness"
    assert "known limitations" in text.lower(), "no limitations section"
    for tab in ("alerts", "decisions", "missions", "forecast", "copilot"):
        assert tab in text.lower(), f"the script never mentions the {tab} panel"


def test_documented_ports_match_the_settings() -> None:
    """A document naming the wrong port sends the reader to a closed socket.

    Checked against the settings rather than a copy, so a port change fails here instead of in a demo.
    """
    from sio_core.config import Settings

    settings = Settings()
    text = "\n".join(path.read_text() for path in DOCS)
    for port in re.findall(r"(?:localhost|127\.0\.0\.1):(\d{4})", text):
        number = int(port)
        # 5173 is vite's, which the settings also own.
        # Read from the settings, so a port change fails here rather than in a demo. Governance was added
        # to this set by the test failing when GOVERNANCE.md started quoting :8118 — which is the lint
        # working: a document naming a port nothing listens on sends the reader to a closed socket.
        known = {
            settings.port_for(name)
            for name in ("api", "ingest", "copilot", "alerts", "governance", "decision", "workflow")
        } | {
            settings.web_port,
            3000,  # grafana, named in passing
            8080,  # keycloak, in the optional-provider instructions
            8181,  # opa, likewise
        }
        assert number in known, f"the docs point at :{number}, which nothing listens on"
