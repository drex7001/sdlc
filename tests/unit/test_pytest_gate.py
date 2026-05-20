"""Pytest gate helpers."""

from __future__ import annotations

from pathlib import Path

from pipeline.gates.pytest_gate import compute_ac_coverage


def test_ac_coverage_accepts_common_docstring_formats(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_items.py").write_text(
        '''
def test_one():
    """AC: AC-1"""


def test_two():
    """AC-2: covers sequential ids."""
''',
        encoding="utf-8",
    )

    coverage = compute_ac_coverage(tests_dir, {"AC-1", "AC-2", "AC-3"})

    assert coverage["AC-1"] == ["test_items.py:3"]
    assert coverage["AC-2"] == ["test_items.py:7"]
    assert coverage["AC-3"] == []
