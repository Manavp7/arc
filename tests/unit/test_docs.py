"""Documentation that cannot silently go stale (Tier 6, Phase 8).

Docs rot in a specific way: not by being deleted, but by staying while the thing they describe changes. A
reference listing a recipe that no longer exists is worse than no reference — somebody runs the command, it
fails, and they conclude the documentation cannot be trusted rather than that one line is old.

So the parts that *can* be checked mechanically are. These are lints, not prose review: they cannot tell whether
an explanation is good, only whether it still refers to something real.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _recipe_names() -> set[str]:
    """Every recipe `just` knows about.

    Read from `just --list` rather than parsed from the Justfile, so this checks what a user would actually
    find — including anything `just` resolves that a regex over the file would miss.
    """
    result = subprocess.run(
        ["just", "--list"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:  # pragma: no cover - `just` is a dev dependency
        pytest.skip("just is not installed")
    names: set[str] = set()
    for line in result.stdout.splitlines()[1:]:
        signature = line.strip().partition("#")[0].strip()
        if signature:
            names.add(signature.split()[0])
    return names


def test_every_recipe_appears_in_the_reference() -> None:
    """The check that keeps `docs/RECIPES.md` honest.

    Before this, 26 of the 52 recipes appeared in no document at all — including `just eval` and `just bench`,
    which are the two the plan's own acceptance criteria name. Nobody had done anything wrong; a reference kept
    by hand simply falls behind, quietly, and the gap is invisible until somebody goes looking for a command.
    """
    reference = (DOCS / "RECIPES.md").read_text()
    missing = sorted(name for name in _recipe_names() if f"`just {name}" not in reference)
    assert not missing, (
        f"these recipes are not in docs/RECIPES.md: {missing}. Run `just recipes` to regenerate it — the "
        f"descriptions come from the comments above each recipe in the Justfile."
    )


def test_the_reference_lists_nothing_that_no_longer_exists() -> None:
    """The other direction, which matters more.

    A missing entry is a gap; a stale one is a lie. Somebody runs the command, it fails, and the conclusion
    they draw is about the documentation rather than about the line.
    """
    reference = (DOCS / "RECIPES.md").read_text()
    listed = set(re.findall(r"`just ([a-z][a-z0-9-]*)", reference))
    # `just` on its own, and prose references like `just check` inside the preamble, are fine.
    stale = sorted(listed - _recipe_names())
    assert not stale, (
        f"docs/RECIPES.md lists recipes that do not exist: {stale}. Run `just recipes`."
    )


def test_every_recipe_has_a_description() -> None:
    """`just --list` is the first thing somebody runs.

    A recipe with no comment shows up there as a bare name, which tells the reader nothing and — worse —
    suggests the recipe is unimportant. Eleven were bare before Phase 8.
    """
    result = subprocess.run(
        ["just", "--list"], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:  # pragma: no cover
        pytest.skip("just is not installed")
    undescribed = [
        line.strip().split()[0]
        for line in result.stdout.splitlines()[1:]
        if line.strip() and "#" not in line
    ]
    assert not undescribed, (
        f"these recipes have no comment above them in the Justfile, so `just --list` shows a bare name: "
        f"{undescribed}"
    )


def test_every_doc_the_readme_links_to_exists() -> None:
    """A broken link in the README is the first thing a new reader hits."""
    readme = (REPO_ROOT / "README.md").read_text()
    broken: list[str] = []
    for target in re.findall(r"\]\((docs/[^)#]+|[A-Z_]+\.md)\)", readme):
        if not (REPO_ROOT / target).exists():
            broken.append(target)
    assert not broken, f"the README links to files that do not exist: {broken}"


def test_every_service_readme_is_linked_or_findable() -> None:
    """A per-service README nobody can find is one nobody reads.

    Not asserting that the top-level README links every one — that would be a wall of links — only that each
    service has one, which the architecture test already requires, and that the docs directory points at the
    services collectively.
    """
    readme = (REPO_ROOT / "README.md").read_text()
    assert "services/" in readme, "the README should point at the services directory"


@pytest.mark.parametrize(
    "document",
    [
        "CONNECTORS.md",
        "GPU_SWAP.md",
        "PLUGINS.md",
        "SDK.md",
        "DEMO.md",
        "RECIPES.md",
        "MODELS.md",
        "MACOS_CHECKLIST.md",
    ],
)
def test_the_documents_the_plan_promises_exist(document: str) -> None:
    """Each of these is named in the plan as a deliverable."""
    path = DOCS / document
    assert path.exists(), f"docs/{document} is promised by the plan and missing"
    assert len(path.read_text()) > 500, f"docs/{document} looks like a stub"


# --- the licence table (R6) -------------------------------------------------------------------------
def test_every_downloaded_model_has_a_licence_row() -> None:
    """R6: the licence table must cover what the platform actually downloads.

    Checked against `scripts/fetch_models.py` rather than against a list in the test, so a model added to the
    fetcher without a licence row fails here. That direction matters more than it sounds: one of the shipped
    weights is AGPL-3.0, and the cost of discovering a copyleft dependency during a procurement review is
    measured in weeks.
    """
    fetcher = (REPO_ROOT / "scripts" / "fetch_models.py").read_text()
    models = set(re.findall(r'filename="([^"]+\.onnx)"', fetcher))
    assert models, "could not find any model filenames in scripts/fetch_models.py"

    table = (DOCS / "MODELS.md").read_text()
    missing = sorted(name for name in models if name not in table)
    assert not missing, (
        f"these models are downloaded but have no licence row in docs/MODELS.md: {missing}. "
        f"R6 requires the table to be complete."
    )


def test_the_licence_table_names_the_copyleft_dependency() -> None:
    """The one fact in that table somebody has to act on.

    A licence table that lists everything as "open source" is decoration. AGPL-3.0 reaches network use, so a
    commercial deployment needs either an enterprise licence or a different detector — and the document has to
    say so plainly enough that nobody reaches procurement without knowing.
    """
    table = (DOCS / "MODELS.md").read_text()
    assert "AGPL-3.0" in table, "the Ultralytics weights are AGPL-3.0 and the table must say so"
    assert "Enterprise" in table or "enterprise" in table, (
        "the table should name the way out, not just the problem"
    )
