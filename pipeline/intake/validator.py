"""Spec validation. Wraps Pydantic errors into a friendly report."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .parser import parse_spec_file
from .schema import FeatureSpec

REQUIRED_SECTIONS = (
    "name",
    "objective",
    "user_story",
    "business_rules",
    "acceptance_criteria",
    "non_functional",
    "out_of_scope",
)


class SpecValidationError(Exception):
    """Raised when a spec fails structural validation."""

    def __init__(self, errors: list[str]):
        super().__init__("spec validation failed:\n  - " + "\n  - ".join(errors))
        self.errors = errors


def _check_required_sections(data: dict[str, Any]) -> list[str]:
    return [f"missing required section: {section}" for section in REQUIRED_SECTIONS if section not in data]


def validate_spec(data: dict[str, Any]) -> FeatureSpec:
    """Validate a parsed spec dict and return a frozen FeatureSpec.

    Required-section check happens before Pydantic so authors get a clear
    list of missing sections rather than nested validation errors.
    """
    missing = _check_required_sections(data)
    if missing:
        raise SpecValidationError(missing)
    try:
        return FeatureSpec.model_validate(data)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        raise SpecValidationError(errors) from e


def load_and_validate(path: Path) -> FeatureSpec:
    return validate_spec(parse_spec_file(path))
