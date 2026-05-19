"""Spec parser: MD / YAML / JSON → dict.

Markdown specs use a simple, deterministic structure: H2 section headers
mapped to spec fields. List items become list entries; paragraphs become
string values. Acceptance criteria are parsed with an `AC-N:` prefix.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

SECTION_ALIASES = {
    "objective": "objective",
    "feature objective": "objective",
    "user story": "user_story",
    "business rules": "business_rules",
    "acceptance criteria": "acceptance_criteria",
    "non-functional requirements": "non_functional",
    "non functional requirements": "non_functional",
    "nfrs": "non_functional",
    "out-of-scope": "out_of_scope",
    "out of scope": "out_of_scope",
}

LIST_FIELDS = {"business_rules", "non_functional", "out_of_scope", "acceptance_criteria"}

AC_LINE = re.compile(r"^\s*(AC-\d+)\s*[:\-]\s*(.+)$")


class SpecParseError(ValueError):
    pass


def parse_spec_file(path: Path) -> dict[str, Any]:
    """Parse a spec file by extension. Returns a plain dict — no validation."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        return _normalize(yaml.safe_load(text) or {})
    if suffix == ".json":
        return _normalize(json.loads(text))
    if suffix == ".md":
        return _parse_markdown(text)
    raise SpecParseError(f"unsupported spec extension: {suffix}")


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Lowercase top-level keys + map aliases."""
    result: dict[str, Any] = {}
    for k, v in raw.items():
        key = SECTION_ALIASES.get(k.lower().strip(), k.lower().strip())
        result[key] = v
    if "acceptance_criteria" in result:
        result["acceptance_criteria"] = _coerce_ac(result["acceptance_criteria"])
    return result


def _coerce_ac(raw: Any) -> list[dict[str, str]]:
    """Allow ACs to be given as list of strings ("AC-1: foo"), dicts, or mixed."""
    if not isinstance(raw, list):
        raise SpecParseError("acceptance_criteria must be a list")
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append({"id": str(item["id"]), "description": str(item["description"])})
        elif isinstance(item, str):
            m = AC_LINE.match(item)
            if not m:
                raise SpecParseError(
                    f"acceptance_criteria string must start with 'AC-N:': {item!r}"
                )
            out.append({"id": m.group(1), "description": m.group(2).strip()})
        else:
            raise SpecParseError(f"unsupported acceptance_criteria entry: {item!r}")
    return out


def _parse_markdown(text: str) -> dict[str, Any]:
    """Parse a structured Markdown spec.

    Rules:
      - First `# H1` (if present) becomes `name`.
      - Each `## H2` opens a section. Section name normalized via SECTION_ALIASES.
      - Body lines until next H2 are collected.
      - List items (`- foo`) become list entries.
      - Otherwise the joined non-empty lines become a single string.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    name: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# ") and name is None and current is None:
            name = line[2:].strip()
            continue
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            current = SECTION_ALIASES.get(heading, heading.replace(" ", "_"))
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    result: dict[str, Any] = {}
    if name:
        result["name"] = _slugify(name)
    for key, lines in sections.items():
        list_items = [_strip_bullet(ln) for ln in lines if ln.lstrip().startswith(("-", "*"))]
        if key in LIST_FIELDS or list_items:
            if key == "acceptance_criteria":
                result[key] = _coerce_ac(list_items)
            else:
                result[key] = list_items
        else:
            joined = "\n".join(ln for ln in lines if ln.strip()).strip()
            if joined:
                result[key] = joined
    return result


def _strip_bullet(line: str) -> str:
    return line.lstrip().lstrip("-*").strip()


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
