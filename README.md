# AI-native, Spec-driven Development Pipeline

A prototype that transforms a structured feature specification into an implementation plan, code changes, automated tests, validation artefacts and deployment evidence — under deterministic governance controls.

The pipeline has seven stages:

```
spec ─► intake ─► plan ─► approval#1 ─► codegen ─► testgen ─► gates ─► approval#2 ─► finalize
```

Two governance boundaries wrap the non-deterministic LLM steps:

1. **Sandbox** — every file the LLM proposes to write must appear in `plan.impacted_files`. Anything else is rejected before disk.
2. **Gates** — `ruff` (lint), `mypy` (types), `pytest` (tests + coverage), `bandit` (security) and a custom **policy** gate must all pass. The pipeline fails closed.

Every run produces a directory under `runs/<run-id>/` containing the spec snapshot, plan, every LLM request/response, generated patches, gate logs, approvals, metrics and a deployment-evidence bundle. SQLite (`audit.db`) indexes all of this for the dashboard.

---

## Quick start

```bash
# 1. Set up
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" flask

# 2. Validate a spec (no LLM call)
pipeline validate specs/example.yaml

# 3. Run end-to-end against the bundled flask-status target, with the mock provider
PIPELINE_LLM_PROVIDER=mock pipeline run specs/example.yaml --approval-mode auto

# 4. Inspect what happened
pipeline status                                # list recent runs
pipeline status <run-id>                       # one run's stages, gates, approvals
ls runs/<run-id>/                              # raw artifacts

# 5. Browse runs in a dashboard
uvicorn dashboard.app:app --port 8000
# open http://localhost:8000

# 6. Reset a target before re-running (it gets mutated by the pipeline)
./scripts/reset_target.sh flask-status
```

### Demo: CRUD series

`targets/crud-api/` is a near-empty FastAPI app. The pipeline can build it up
one feature at a time. Each spec runs as a separate pipeline invocation, so
later specs see the code the earlier specs produced.

```bash
./scripts/reset_target.sh crud-api

PIPELINE_TARGET_DIR=./targets/crud-api \
  pipeline run specs/crud/01-create-item.yaml --approval-mode auto

PIPELINE_TARGET_DIR=./targets/crud-api \
  pipeline run specs/crud/02-list-items.yaml --approval-mode auto

PIPELINE_TARGET_DIR=./targets/crud-api \
  pipeline run specs/crud/03-delete-item.yaml --approval-mode auto
```

The mock provider's canned plan is keyed to `flask-status`; run the CRUD
series with `PIPELINE_LLM_PROVIDER=anthropic` (or `openai`) so the model
actually generates the FastAPI code.

### Repair loop

When any gate (ruff / mypy / pytest / bandit / policy) fails after codegen,
the pipeline drives a tool-using **repair agent** with the failing gate's log,
the current code, and the same sandbox the planner approved. It can read
files, inspect logs and `write_files` minimal corrections. Up to
`PIPELINE_MAX_REPAIR_ATTEMPTS` attempts (default 2) before the run fails for
real. Each attempt is its own audit stage (`repair_1`, `repair_2`).

### Using a real LLM provider

The pipeline auto-loads a `.env` file at the repo root on startup (no shell sourcing needed):

```bash
cp .env.example .env
# edit .env: set PIPELINE_LLM_PROVIDER=openai (or anthropic) and add your API key
pipeline run specs/example.yaml --approval-mode auto
```

The CLI prints the three model names at start-up so you can confirm `.env` was picked up:

```
provider=openai
models: plan=gpt-4o-mini codegen=gpt-4o testgen=gpt-4o-mini
```

To verify real API calls actually happened (vs the mock), three options:

```bash
# (a) Tokens > 0 in the summary
# (b) Per-stage model recorded in the audit DB:
sqlite3 audit.db "SELECT stage, provider, model, input_tokens, output_tokens FROM prompt_calls;"
# (c) Raw request/response pairs in the JSONL log:
jq '{stage, provider, model, usage}' runs/<run-id>/prompts.jsonl
```

Approval modes:

| Mode | When to use |
| --- | --- |
| `cli` (default) | Interactive — the pipeline blocks on stdin at each checkpoint. |
| `dashboard` | Pipeline writes `awaiting_approval` to the audit DB and polls. Open the dashboard, click Approve / Reject. |
| `auto` | No human — every checkpoint is auto-approved. Used in CI and demos. |

---

## Spec format

Specs can be Markdown, YAML or JSON. Required sections:

- `name` (kebab-case identifier)
- `objective`
- `user_story`
- `business_rules` (list)
- `acceptance_criteria` (list of `{id: "AC-N", description: "..."}`)

Optional:
- `non_functional`
- `out_of_scope`

See `specs/example.{md,yaml,json}` for canonical examples. The validator rejects missing sections **before** any LLM call.

---

## Project layout

```
pipeline/                # The pipeline package
  intake/                # Parse + validate specs
  planning/              # Spec → Plan via LLM
  implementation/        # Plan → file changes + sandbox enforcement
  testing/               # Generated pytest files tagged with AC IDs
  gates/                 # ruff, mypy, pytest, bandit, custom policy
  approval/              # Two-checkpoint human workflow
  llm/                   # Provider-pluggable client + versioned prompts
  audit/                 # SQLite index + filesystem artifacts
  metrics/               # Per-run telemetry
  orchestrator.py        # Wires every stage together
  cli.py                 # Typer entrypoint (`pipeline run`)

dashboard/               # FastAPI + Jinja2 read-mostly UI
targets/                 # Bundled apps the pipeline modifies
  flask-status/          #   - Tiny Flask app for the rate-limit demo
  crud-api/              #   - FastAPI baseline for the layered CRUD spec series
specs/                   # Example feature specifications
tests/                   # Pipeline's own tests (unit + integration)
runs/                    # Per-run artifact directories (created at runtime)
docker/                  # Dockerfile + docker-compose.yml
.github/workflows/       # CI pipeline (mock provider, no API keys needed)
```

---

## Running with Docker

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d dashboard
# Run a pipeline against the bundled sample target
docker compose -f docker/docker-compose.yml run --rm pipeline run specs/example.yaml --approval-mode auto
# Browse runs at http://localhost:8000
```

---

## Environment variables

All are read from process env or from a `.env` file at the repo root (auto-loaded). See [`.env.example`](.env.example) for a ready-to-copy template.

| Variable | Default | Description |
| --- | --- | --- |
| `PIPELINE_LLM_PROVIDER` | `mock` | `mock`, `anthropic`, or `openai`. |
| `PIPELINE_LLM_MODEL` | provider-specific | Fallback model for every stage that doesn't override. |
| `PIPELINE_PLAN_MODEL` | falls back to `PIPELINE_LLM_MODEL` | Model used for the planning stage. |
| `PIPELINE_CODEGEN_MODEL` | falls back to `PIPELINE_LLM_MODEL` | Model used for code generation. |
| `PIPELINE_TESTGEN_MODEL` | falls back to `PIPELINE_LLM_MODEL` | Model used for test generation. |
| `PIPELINE_MAX_TOKENS` | `8192` | Max output tokens per LLM call. Bump if codegen is being truncated. |
| `PIPELINE_PROMPT_VERSION` | `v1` | Subdirectory under `pipeline/llm/prompts/`. Pinned per run. |
| `PIPELINE_TARGET_DIR` | `./targets/flask-status` | Repo the pipeline modifies. Set to `./targets/crud-api` for the CRUD series. |
| `PIPELINE_RUNS_DIR` | `./runs` | Where artifact directories are written. |
| `PIPELINE_AUDIT_DB` | `./audit.db` | SQLite index path. |
| `APPROVER` | `unknown@local` | Reviewer identity recorded in approvals. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Required for the corresponding real provider. |

The typical per-stage setup is "cheap model for planning + testgen, stronger model for codegen" — see `.env.example` for the recommended values.

---

## Development & testing

The pipeline's own test suite, lint and type checks are independent of any pipeline run.

```bash
# Activate the venv first
source .venv/bin/activate

# Unit + integration tests (use the mock provider — no API key needed)
pytest -v                          # everything
pytest tests/unit -q               # unit tests only (fast)
pytest tests/integration -q        # full end-to-end via mock
pytest -k sandbox                  # filter by name

# Lint and type-check the pipeline source
ruff check pipeline dashboard tests
mypy pipeline dashboard
```

The integration tests use a tmp-copied `targets/flask-status` so they don't depend on the on-disk state. CI runs all of the above on every push (see [`.github/workflows/pipeline-ci.yml`](.github/workflows/pipeline-ci.yml)).

### Cleaning between runs

A pipeline run **mutates the target dir in place** — new files appear, `__init__.py` gets the new blueprint registered. To re-run cleanly:

```bash
# Restore one target (or pass no arg to reset all)
./scripts/reset_target.sh flask-status
./scripts/reset_target.sh crud-api

# Optionally also drop the run artifacts and audit DB
rm -rf runs audit.db
```

`runs/` and `audit.db` are gitignored, so leaving them around between runs is fine — `pipeline status` will list every past run and the dashboard will show the full history.

---

## Demonstrating the governance boundaries

```bash
# 1. Spec validation aborts before any LLM call
echo "name: bad" > /tmp/bad.yaml
pipeline run /tmp/bad.yaml                     # fails with a friendly list of missing sections

# 2. Inspect a run's artifacts
ls runs/<run-id>/
cat runs/<run-id>/plan.json                    # the implementation plan
cat runs/<run-id>/gates.json                   # per-gate pass/fail + duration
cat runs/<run-id>/deployment_evidence.json     # the signed-off bundle
cat runs/<run-id>/prompts.jsonl                # every LLM request/response

# 3. SQLite index
sqlite3 audit.db "SELECT stage, status, duration_ms FROM stages WHERE run_id='<run-id>';"
```

---

## Notes & assumptions

- The pipeline does not require a git repo for the target. It diffs against the working tree.
- Anthropic provider uses prompt caching on the system message — repeated stages in the same run amortise instruction tokens.
- The mock provider returns the **same canned outputs regardless of spec**. It exists to exercise the pipeline mechanics deterministically; it is not an evaluator of LLM intelligence.
- Generated tests carry an `AC: AC-N` marker in their docstring. The pytest gate parses these to produce an acceptance-criteria coverage report.
- See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions, trade-offs, limitations and future improvements.
