"""Spec intake: parse, validate, freeze."""

from .parser import parse_spec_file
from .schema import AcceptanceCriterion, FeatureSpec
from .validator import SpecValidationError, load_and_validate, validate_spec

__all__ = [
    "AcceptanceCriterion",
    "FeatureSpec",
    "SpecValidationError",
    "load_and_validate",
    "parse_spec_file",
    "validate_spec",
]
