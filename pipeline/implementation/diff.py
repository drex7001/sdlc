"""Apply generated file changes and produce diffs + summary."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from ..audit import AuditStore, RunRecord
from .codegen import GeneratedChanges


@dataclass
class ChangeSummary:
    files_created: list[str]
    files_modified: list[str]
    files_deleted: list[str]
    diff_text: str

    def to_markdown(self, headline: str) -> str:
        lines = [f"# Change summary\n\n{headline}\n"]
        if self.files_created:
            lines.append("## Created\n" + "\n".join(f"- {p}" for p in self.files_created))
        if self.files_modified:
            lines.append("\n## Modified\n" + "\n".join(f"- {p}" for p in self.files_modified))
        if self.files_deleted:
            lines.append("\n## Deleted\n" + "\n".join(f"- {p}" for p in self.files_deleted))
        return "\n".join(lines) + "\n"


def apply_changes(
    *,
    changes: GeneratedChanges,
    target_dir: Path,
    run: RunRecord,
    audit: AuditStore,
    kind: str = "codegen",
) -> ChangeSummary:
    """Apply file operations to target_dir. Writes patches into the audit dir.

    ``kind`` is "codegen" or "testgen"; it becomes part of the patch filename
    so the two stages do not collide.
    """
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    diff_chunks: list[str] = []

    for change in changes.files:
        full = target_dir / change.path
        if change.action == "delete":
            before = full.read_text(encoding="utf-8") if full.exists() else ""
            if full.exists():
                full.unlink()
                deleted.append(change.path)
            diff_chunks.append(_unified_diff(change.path, before, ""))
            continue

        before = full.read_text(encoding="utf-8") if full.exists() else ""
        after = change.content
        if change.action == "create":
            if full.exists():
                raise FileExistsError(
                    f"codegen wanted to create {change.path} but it already exists"
                )
            created.append(change.path)
        else:  # modify
            if not full.exists():
                raise FileNotFoundError(
                    f"codegen wanted to modify {change.path} but it does not exist"
                )
            modified.append(change.path)

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(after, encoding="utf-8")
        diff_chunks.append(_unified_diff(change.path, before, after))

    diff_text = "".join(diff_chunks)
    audit.write_artifact(run, f"patches/{kind}.patch", diff_text)
    summary = ChangeSummary(
        files_created=created, files_modified=modified, files_deleted=deleted, diff_text=diff_text
    )
    if kind == "codegen":
        audit.write_artifact(run, "change_summary.md", summary.to_markdown(changes.summary))
    return summary


def _unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
