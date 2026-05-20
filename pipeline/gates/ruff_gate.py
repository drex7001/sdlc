"""Ruff lint gate."""

from __future__ import annotations

from pathlib import Path

from .base import GateOutcome, run_module


def run(target_dir: Path) -> GateOutcome:
    # `--fix` auto-corrects auto-fixable lint nits (UP045 Optional, I001 imports,
    # etc.) before the gate verdict. Ruff returns non-zero only when unfixable
    # issues remain, so the gate stays strict — but the LLM repair loop is no
    # longer burning budget on style violations a deterministic tool handles.
    return run_module("ruff", "ruff", ["check", ".", "--fix"], cwd=target_dir)
