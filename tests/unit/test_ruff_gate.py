"""Ruff gate behavior."""

from __future__ import annotations

import types
from pathlib import Path

from pipeline.gates import base, ruff_gate


def test_ruff_gate_is_validation_only(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, list[str]] = {}

    def fake_find_spec(module: str) -> object:
        return object()

    def fake_run(args, **kwargs):
        captured["args"] = args
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(base.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(base.subprocess, "run", fake_run)

    outcome = ruff_gate.run(tmp_path)

    assert outcome.passed
    assert captured["args"][-2:] == ["check", "."]
    assert "--fix" not in captured["args"]
