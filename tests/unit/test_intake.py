"""Unit tests for spec intake (parser + validator)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.intake import SpecValidationError, load_and_validate, parse_spec_file, validate_spec


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


def test_yaml_parser_round_trip(example_spec_path: Path) -> None:
    data = parse_spec_file(example_spec_path)
    assert data["name"] == "rate-limit-status-endpoint"
    assert len(data["acceptance_criteria"]) == 4
    assert data["acceptance_criteria"][0]["id"] == "AC-1"


def test_json_parser_round_trip(tmp_path: Path) -> None:
    payload = {
        "name": "x", "objective": "o", "user_story": "u",
        "business_rules": ["r"],
        "acceptance_criteria": [{"id": "AC-1", "description": "d"}],
    }
    f = _write(tmp_path, "s.json", json.dumps(payload))
    data = parse_spec_file(f)
    assert data["name"] == "x"


def test_markdown_parser_with_ac_prefix(tmp_path: Path) -> None:
    f = _write(tmp_path, "s.md", """# Sample

## Objective
Do a thing.

## User Story
As a user I want X.

## Business Rules
- rule one
- rule two

## Acceptance Criteria
- AC-1: first thing happens
- AC-2: second thing happens
""")
    data = parse_spec_file(f)
    assert data["name"] == "sample"
    assert data["acceptance_criteria"] == [
        {"id": "AC-1", "description": "first thing happens"},
        {"id": "AC-2", "description": "second thing happens"},
    ]


def test_validate_missing_section_lists_all_missing() -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec({"name": "x"})
    msg = str(exc_info.value)
    assert "objective" in msg
    assert "user_story" in msg
    assert "acceptance_criteria" in msg


def test_validate_rejects_duplicate_ac_ids() -> None:
    with pytest.raises(SpecValidationError):
        validate_spec({
            "name": "x", "objective": "o", "user_story": "u",
            "business_rules": ["r"],
            "acceptance_criteria": [
                {"id": "AC-1", "description": "a"},
                {"id": "AC-1", "description": "b"},
            ],
        })


def test_validate_rejects_bad_ac_id_format() -> None:
    with pytest.raises(SpecValidationError):
        validate_spec({
            "name": "x", "objective": "o", "user_story": "u",
            "business_rules": ["r"],
            "acceptance_criteria": [{"id": "AC1", "description": "a"}],
        })


def test_load_and_validate_example_yaml(example_spec_path: Path) -> None:
    spec = load_and_validate(example_spec_path)
    assert spec.name == "rate-limit-status-endpoint"
    assert len(spec.acceptance_criteria) == 4
    assert spec.content_hash() == spec.content_hash()  # deterministic
