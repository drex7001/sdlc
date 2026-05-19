"""Custom policy gate catches things ruff/bandit miss."""

from __future__ import annotations

from pipeline.gates.policy_gate import run
from pipeline.implementation.codegen import FileChange, GeneratedChanges

_NOOP = FileChange(path="src/noop.py", action="create", content="x = 1\n")


def _impl(files: list[FileChange]) -> GeneratedChanges:
    return GeneratedChanges(files=files or [_NOOP], summary="test")


def _tests(files: list[FileChange]) -> GeneratedChanges:
    return GeneratedChanges(
        files=files or [FileChange(path="tests/test_noop.py", action="create", content="x=1\n")],
        summary="test",
    )


def test_clean_changes_pass() -> None:
    impl = _impl([FileChange(path="src/a.py", action="create", content="x = 1\n")])
    tests = _tests([FileChange(path="tests/test_a.py", action="create", content="def test_x(): assert True\n")])
    outcome = run(impl_changes=impl, test_changes=tests, plan_impacted={"src/a.py", "tests/test_a.py"})
    assert outcome.passed


def test_aws_key_is_rejected() -> None:
    impl = _impl([FileChange(path="src/a.py", action="create", content='KEY = "AKIAABCDEFGHIJKLMNOP"\n')])
    outcome = run(
        impl_changes=impl, test_changes=_tests([]),
        plan_impacted={"src/a.py", "tests/test_noop.py"},
    )
    assert not outcome.passed
    assert "AWS access key" in outcome.output


def test_eval_in_impl_is_rejected() -> None:
    impl = _impl([FileChange(path="src/a.py", action="create", content="eval('1+1')\n")])
    outcome = run(
        impl_changes=impl, test_changes=_tests([]),
        plan_impacted={"src/a.py", "tests/test_noop.py"},
    )
    assert not outcome.passed
    assert "eval/exec" in outcome.output


def test_network_in_tests_is_rejected() -> None:
    tests = _tests([FileChange(
        path="tests/test_a.py", action="create",
        content="import requests\ndef test_x():\n    requests.get('http://example.com')\n",
    )])
    outcome = run(impl_changes=_impl([FileChange(path="src/a.py", action="create", content="x=1\n")]),
                  test_changes=tests, plan_impacted={"src/a.py", "tests/test_a.py"})
    assert not outcome.passed
    assert "network calls" in outcome.output


def test_path_outside_plan_is_rejected_defence_in_depth() -> None:
    impl = _impl([FileChange(path="src/secret.py", action="create", content="x=1\n")])
    outcome = run(
        impl_changes=impl, test_changes=_tests([]),
        plan_impacted={"tests/test_noop.py"},  # secret.py absent on purpose
    )
    assert not outcome.passed
    assert "not in plan.impacted_files" in outcome.output
