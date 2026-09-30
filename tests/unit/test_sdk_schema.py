"""The saved SDK contract follows routes without starting API services."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "sdk" / "ts" / "scripts" / "export_openapi.py"


def test_saved_sdk_schema_matches_api_factory(monkeypatch):
    exporter = runpy.run_path(str(EXPORTER))
    monkeypatch.setattr(sys, "argv", [str(EXPORTER), "--check"])
    assert exporter["main"]() == 0


def test_schema_drift_check_refuses_without_rewriting(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "openapi.json"
    stale.write_text("{}\n")
    exporter = runpy.run_path(str(EXPORTER))
    monkeypatch.setattr(sys, "argv", [str(EXPORTER), "--check", "--out", str(stale)])
    assert exporter["main"]() == 1
    assert "schema is stale" in capsys.readouterr().out
    assert stale.read_text() == "{}\n"
