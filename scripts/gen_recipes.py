#!/usr/bin/env python
"""Generate `docs/RECIPES.md` from the Justfile (Phase 8, Tier 6 docs cross-check).

Generated rather than written, and checked by a test, because a reference maintained by hand is one that is
wrong within a month — and a wrong reference is worse than none: it sends somebody to a command that no longer
exists, and they conclude the docs cannot be trusted rather than that one line is stale.

The descriptions come from the comments above each recipe, so there is exactly one place to change them and
`just --list` and the document cannot disagree.

    just recipes        # regenerate
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse() -> list[tuple[str, str, str, str]]:
    """`(section, name, args, description)` for every recipe, in Justfile order."""
    listing = subprocess.run(
        ["just", "--list"], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    ).stdout

    described: dict[str, tuple[str, str]] = {}
    for line in listing.splitlines()[1:]:
        if not line.strip():
            continue
        signature, _, comment = line.strip().partition("#")
        parts = signature.strip().split()
        if not parts:
            continue
        described[parts[0]] = (" ".join(parts[1:]), comment.strip())

    rows: list[tuple[str, str, str, str]] = []
    section = "General"
    for line in (REPO_ROOT / "Justfile").read_text().splitlines():
        heading = re.match(r"^#\s*-{3,}\s*(.+?)\s*$", line)
        if heading:
            section = heading.group(1).strip().strip("- ").title()
            continue
        recipe = re.match(r"^([a-z][a-z0-9-]*)(\s+\*?args)?:", line)
        if recipe and recipe.group(1) in described:
            name = recipe.group(1)
            args, comment = described.pop(name)
            rows.append((section, name, args, comment))
    # Anything `just --list` knows about that the parser did not place, rather than silently dropping it.
    rows.extend(("General", name, args, comment) for name, (args, comment) in described.items())
    return rows


def render(rows: list[tuple[str, str, str, str]]) -> str:
    lines = [
        "# Every `just` recipe",
        "",
        "Generated from the `Justfile` by `just recipes`, and **checked by a test**:",
        "`tests/unit/test_docs.py` fails if a recipe is added without appearing here.",
        "",
        "A reference maintained by hand is wrong within a month, and a wrong reference is worse than none —",
        "it sends somebody to a command that does not exist and they conclude the docs cannot be trusted.",
        "The descriptions come from the comments above each recipe, so `just --list` and this page cannot",
        "disagree.",
        "",
        f"There are **{len(rows)}**.",
        "",
    ]
    seen: list[str] = []
    for section, _, _, _ in rows:
        if section not in seen:
            seen.append(section)
    for section in seen:
        entries = [row for row in rows if row[0] == section]
        lines.extend([f"## {section}", "", "| recipe | what it does |", "|---|---|"])
        for _, name, args, comment in entries:
            label = f"`just {name}{' ' + args if args else ''}`"
            lines.append(f"| {label} | {comment or '—'} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    rows = parse()
    (REPO_ROOT / "docs" / "RECIPES.md").write_text(render(rows))
    print(f"wrote docs/RECIPES.md — {len(rows)} recipes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
