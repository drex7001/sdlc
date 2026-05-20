"""Pydantic schema for the canonical FeatureSpec.

This is the contract every downstream stage relies on. Required fields are
enforced here so the validator can produce friendly errors instead of leaking
Pydantic stack traces.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints, field_validator

AC_ID_PATTERN = re.compile(r"^AC-\d+$")
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AcceptanceCriterion(BaseModel):
    id: str = Field(description="Stable ID, e.g. AC-1. Tests reference this.")
    description: NonEmptyStr

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not AC_ID_PATTERN.match(v):
            raise ValueError(f"acceptance_criteria id must match AC-<int>, got {v!r}")
        return v


class FeatureSpec(BaseModel):
    """Canonical, validated feature specification."""

    name: NonEmptyStr = Field(description="Short feature identifier (kebab-case).")
    objective: NonEmptyStr
    user_story: NonEmptyStr
    business_rules: list[NonEmptyStr] = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    non_functional: list[NonEmptyStr] = Field(min_length=1)
    out_of_scope: list[NonEmptyStr] = Field(min_length=1)

    @field_validator("acceptance_criteria")
    @classmethod
    def _unique_ac_ids(cls, v: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        ids = [ac.id for ac in v]
        if len(set(ids)) != len(ids):
            dupes = {x for x in ids if ids.count(x) > 1}
            raise ValueError(f"duplicate acceptance_criteria ids: {sorted(dupes)}")
        return v

    def content_hash(self) -> str:
        """Deterministic hash of spec content for audit / reproducibility."""
        payload = json.dumps(self.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()
