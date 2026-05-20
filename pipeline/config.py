"""Runtime configuration for the pipeline.

All values come from environment variables so the same code path works for
the CLI, dashboard and CI. A ``.env`` file at the repo root is loaded
automatically on import — useful for keeping API keys and per-stage model
choices out of your shell history.

Per-stage models
----------------
You can use a single model for the whole pipeline (``PIPELINE_LLM_MODEL``), or
override per stage with ``PIPELINE_PLAN_MODEL``, ``PIPELINE_CODEGEN_MODEL`` and
``PIPELINE_TESTGEN_MODEL``. The typical setup is "cheap model for planning and
test generation, stronger model for code generation".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# Loaded once at import time. Subsequent ``Settings.load()`` calls already see
# the values in the environment.
load_dotenv(REPO_ROOT / ".env", override=False)


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


# Provider-aware default models. These are sensible balanced choices; override
# per stage via env vars to optimise cost vs quality.
_DEFAULT_MODELS = {
    "anthropic": {
        "llm": "claude-sonnet-4-6",
        "plan": "claude-haiku-4-5-20251001",
        "codegen": "claude-sonnet-4-6",
        "testgen": "claude-haiku-4-5-20251001",
    },
    "openai": {
        "llm": "gpt-4o-mini",
        "plan": "gpt-4o-mini",
        "codegen": "gpt-4o",
        "testgen": "gpt-4o-mini",
    },
    "mock": {
        "llm": "mock-v1",
        "plan": "mock-v1",
        "codegen": "mock-v1",
        "testgen": "mock-v1",
    },
}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_model: str           # fallback when a stage doesn't override
    plan_model: str
    codegen_model: str
    testgen_model: str
    max_tokens: int
    runs_dir: Path
    audit_db: Path
    target_dir: Path
    prompts_dir: Path
    prompt_version: str
    approver: str
    max_repair_attempts: int

    @classmethod
    def load(cls) -> Settings:
        provider = os.environ.get("PIPELINE_LLM_PROVIDER", "mock")
        defaults = _DEFAULT_MODELS.get(provider, _DEFAULT_MODELS["mock"])
        llm_model = os.environ.get("PIPELINE_LLM_MODEL", defaults["llm"])
        return cls(
            llm_provider=provider,
            llm_model=llm_model,
            plan_model=os.environ.get("PIPELINE_PLAN_MODEL") or llm_model or defaults["plan"],
            codegen_model=os.environ.get("PIPELINE_CODEGEN_MODEL") or llm_model or defaults["codegen"],
            testgen_model=os.environ.get("PIPELINE_TESTGEN_MODEL") or llm_model or defaults["testgen"],
            max_tokens=int(os.environ.get("PIPELINE_MAX_TOKENS", "8192")),
            runs_dir=Path(os.environ.get("PIPELINE_RUNS_DIR", REPO_ROOT / "runs")),
            audit_db=Path(os.environ.get("PIPELINE_AUDIT_DB", REPO_ROOT / "audit.db")),
            target_dir=Path(os.environ.get("PIPELINE_TARGET_DIR", REPO_ROOT / "targets" / "flask-status")),
            prompts_dir=REPO_ROOT / "pipeline" / "llm" / "prompts",
            prompt_version=os.environ.get("PIPELINE_PROMPT_VERSION", "v1"),
            approver=os.environ.get("APPROVER", "unknown@local"),
            max_repair_attempts=_int_env("PIPELINE_MAX_REPAIR_ATTEMPTS", 2, low=0, high=5),
        )

    def model_for(self, stage: str) -> str:
        """Look up the model assigned to a stage. Falls back to ``llm_model``."""
        return {
            "plan": self.plan_model,
            "codegen": self.codegen_model,
            "testgen": self.testgen_model,
        }.get(stage, self.llm_model)
