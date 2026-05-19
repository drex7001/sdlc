"""AI-assisted implementation layer."""

from .codegen import FileChange, GeneratedChanges, generate_code
from .diff import ChangeSummary, apply_changes
from .sandbox import SandboxViolation, validate_paths

__all__ = [
    "ChangeSummary",
    "FileChange",
    "GeneratedChanges",
    "SandboxViolation",
    "apply_changes",
    "generate_code",
    "validate_paths",
]
