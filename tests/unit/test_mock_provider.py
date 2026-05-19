"""Mock provider: stable, sentinel-dispatched canned responses."""

from __future__ import annotations

import json

import pytest

from pipeline.llm.providers.mock import MockClient


def test_dispatches_plan_sentinel() -> None:
    resp = MockClient().complete(
        system="<<STAGE:PLAN>>",
        prompt="anything", model="mock-v1",
    )
    payload = json.loads(resp.text)
    assert "tasks" in payload and "impacted_files" in payload


def test_dispatches_codegen_sentinel() -> None:
    resp = MockClient().complete(
        system="<<STAGE:CODEGEN>>",
        prompt="anything", model="mock-v1",
    )
    payload = json.loads(resp.text)
    assert "files" in payload


def test_unknown_sentinel_raises() -> None:
    with pytest.raises(ValueError, match="stage sentinel"):
        MockClient().complete(system="no sentinel here", prompt="x", model="m")
