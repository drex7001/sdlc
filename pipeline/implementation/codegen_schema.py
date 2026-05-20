"""Shared Pydantic models for code-change proposals.

Kept in its own module so :mod:`pipeline.implementation.agent_tools` (the
tool-loop driver) and :mod:`pipeline.implementation.codegen` can both depend
on the schema without circular imports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FileChange(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    content: str = ""


class GeneratedChanges(BaseModel):
    files: list[FileChange] = Field(min_length=1)
    summary: str
