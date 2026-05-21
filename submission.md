# Submission — AI-native, Spec-driven Development Pipeline

**Candidate:** Ayodhya Ratnayake  
**Assessment:** Newton Russell Technical Assessment — AI-native, Spec-driven Development Pipeline (1.0V 2026)  
**Submitted:** 2026-05-21

---

## What Was Built

A fully working prototype of an AI-native, spec-driven development pipeline. A structured feature specification (Markdown, YAML, or JSON) enters one end; an implementation plan, generated code, automated tests, gate validation results, and a signed deployment-evidence bundle come out the other — with two human-approval checkpoints along the way.

### Pipeline flow

```
spec ─► intake ─► plan ─► approval#1 ─► codegen ─► testgen ─► gates ─► approval#2 ─► finalize
                                                                   ↻
                                                             repair (≤ N)
```

Eight stages in total: six work stages and two governance checkpoints. When any gate fails, a tool-using repair agent attempts fixes (bounded by `PIPELINE_MAX_REPAIR_ATTEMPTS`, default 2) before the run halts.

---

## Requirements Coverage

### 1. Spec Intake

- Accepts specs in **Markdown, YAML, and JSON** — all three formats are parsed by `pipeline/intake/parser.py`.
- Required sections enforced by `pipeline/intake/validator.py` before any LLM call is made: `name`, `objective`, `user_story`, `business_rules`, `acceptance_criteria`, `non_functional`, `out_of_scope`.
- Missing or empty sections produce a clear validation error listing exactly what is absent. The pipeline fails closed — no LLM tokens are spent on a malformed spec.
- Example specs: `specs/example.{md,yaml,json}` and a three-part incremental CRUD series in `specs/crud/`.

### 2. Planning Layer

- `pipeline/planning/planner.py` sends the spec to an LLM and produces a structured `Plan` object containing: implementation tasks, technical design summary, **impacted files** (the governance contract), risk considerations, and a test strategy.
- The plan is stored as `runs/<run-id>/plan.json` and becomes the single artefact that checkpoint #1 approves.
- Prompt template: `pipeline/llm/prompts/v1/plan.md`.

### 3. AI-assisted Implementation

- `pipeline/implementation/codegen.py` drives a **multi-turn, tool-using agent** (via `pipeline/implementation/agent_tools.py`). The agent can call `read_file`, `list_files`, and `write_files` tools across up to `PIPELINE_MAX_AGENT_TURNS` turns (default 12).
- The LLM returns **complete file contents** (not diffs) in a structured JSON envelope `{path, action, content}`. This avoids the brittleness of LLM-emitted unified diffs. The pipeline then computes a real diff from before/after for audit — patches land in `runs/<run-id>/patches/`.
- **Sandbox enforcement** (`pipeline/implementation/sandbox.py`) intercepts every proposed write. A write is rejected before disk if the path is not in `plan.impacted_files`, is an absolute path, escapes the target root via `..`, or resolves through a symlink. The sandbox is also re-applied during repair.
- A summary of generated changes (files touched, lines added/removed) is recorded in the stage metadata and shown in the dashboard.

### 4. Automated Test Generation

- `pipeline/testing/testgen.py` sends the plan + generated code to an LLM and produces pytest files.
- Generated tests carry an `AC: AC-N` marker in their docstring (e.g. `# AC: AC-1`). The pytest gate parses these markers and computes an **acceptance-criteria coverage report**: how many distinct AC IDs are exercised vs. declared in the spec.
- Test files are sandboxed to the `tests/` directory — the testgen stage cannot write test files to arbitrary locations.

### 5. Quality Gates

Five gates run sequentially after code generation; **all must pass** or the pipeline fails:

| Gate | Tool | What it catches |
|---|---|---|
| Lint | `ruff` | Style and correctness (E, F, I, B, UP, SIM rule sets) |
| Types | `mypy` | Static type errors |
| Tests | `pytest` | Test failures + AC coverage gaps |
| Security | `bandit` | Common Python security anti-patterns |
| Policy | custom (`pipeline/gates/policy_gate.py`) | Hardcoded secrets, `eval`/`exec`, network/subprocess calls in tests, defence-in-depth path re-check |

The custom policy gate is deterministic and complements the third-party tools. It catches classes of AI-generated code risk that pass `bandit` and `ruff` but are undesirable in generated output. Gate stdout/stderr is saved to `runs/<run-id>/` and surfaced in the dashboard.

### 6. Human Approval Workflow

Two checkpoints: one after planning (before any code is written) and one after all gates pass (before deployment evidence is finalised). Three modes are supported:

| Mode | Behaviour |
|---|---|
| `cli` (default) | Pipeline blocks on stdin at each checkpoint |
| `dashboard` | Pipeline sets status to `awaiting_approval`; operator approves via the web UI |
| `auto` | Every checkpoint is auto-approved; used in CI and demos |

All modes write to the same `approvals` table — the orchestrator does not branch on mode. Approvals can also be recorded out-of-band after a run starts: `pipeline approve <run-id> plan approved`.

### 7. Auditability

Every run produces a directory `runs/<run-id>/` containing:

- `spec_snapshot.{yaml,json,md}` — frozen copy of the input spec (with its SHA-256 hash)
- `plan.json` — the approved implementation plan
- `prompts.jsonl` — every LLM request and response, with stage, provider, model, and token counts
- `patches/` — before/after diffs for each modified file
- `gates.json` — per-gate pass/fail, duration, and truncated log
- `approvals.json` — who approved what and when
- `deployment_evidence.json` — the signed-off bundle (spec hash, plan hash, gate results, approvals, prompt version, provider, model)
- `metrics.json` — token counts, latency per stage, estimated cost

**SQLite** (`audit.db`) indexes all of the above for fast dashboard queries across `runs`, `stages`, `approvals`, `gates`, `metrics`, and `prompt_calls` tables.

Reproducibility is pinned per run: spec hash + prompt template version + provider + model are all recorded. The mock provider gives byte-identical replays.

---

## Additionally Built (Beyond Requirements)

The following were not required by the spec but were included because they meaningfully improve governance, developer experience, or system quality.

### Multi-turn Tool-using Agent

The codegen and repair stages drive a real agent loop — not a single LLM call. The agent reads source files, lists directories, and writes files across up to `PIPELINE_MAX_AGENT_TURNS` turns. This is closer to how production AI coding assistants work and allows the model to inspect the existing codebase before deciding what to write.

### Gate-driven Repair Agent

When any gate fails, `pipeline/implementation/repair.py` drives a tool-using repair agent with access to the failing gate's log (`read_gate_log` tool), the current source files, and the original sandbox. Up to `PIPELINE_MAX_REPAIR_ATTEMPTS` attempts are made, each recorded as its own audit stage (`repair_1`, `repair_2`). The repair agent cannot escape the sandbox — if the real fix requires touching files outside `plan.impacted_files`, the run fails rather than silently widening the blast radius.

### Provider-pluggable LLM Abstraction

`pipeline/llm/client.py` defines a one-method `LLMClient` protocol. Three providers ship:

- **`mock`** — deterministic canned responses keyed to per-stage sentinels (`<<STAGE:PLAN>>` etc.); no API key; used in CI.
- **`anthropic`** — with ephemeral prompt caching on the system prompt, amortising large instruction tokens across the three LLM calls in one run.
- **`openai`** — standard chat completions.

Per-stage model selection is supported: a cheap model for planning and test generation, a stronger model for code generation.

### Versioned Prompt Templates

Prompts live under `pipeline/llm/prompts/v1/`. The version used is pinned in the run record. Rolling forward is a directory copy plus an env-var change, and any run can be replayed against its frozen version. This is the scaffold for a future eval harness.

### Observability Dashboard

A FastAPI + Jinja2 web UI (`dashboard/app.py`, ~900 lines) provides:

- Run list with status badges and timestamps
- Run detail with a live workflow diagram
- Artifact browser (prompts, patches, gate logs, evidence bundles)
- Spec upload and validation before submission
- Target project selection (bundled or custom path)
- Approve / Reject buttons for both checkpoints (dashboard approval mode)
- Polling-based live updates without WebSocket complexity

### GitHub Actions CI

`.github/workflows/pipeline-ci.yml` runs on every push and pull request:
- `ruff check` — lint
- `mypy` — type check
- `pytest` — full unit + integration suite
- End-to-end smoke test with the mock provider (no API keys needed)
- Artifact upload of the run directory

### Docker Containerisation

`docker/Dockerfile` and `docker/docker-compose.yml` define two services: `dashboard` and `pipeline`. A full demo runs with three commands.

### Cost and Token Metrics

Every run records input/output token counts per stage. An optional pricing configuration (`PIPELINE_LLM_PRICING_JSON` or generic per-token rates) produces estimated USD cost breakdowns — including cache creation and cache read token rates for the Anthropic provider.

### Incremental CRUD Demo Series

`specs/crud/` contains three layered specs (`01-create-item.yaml`, `02-list-items.yaml`, `03-delete-item.yaml`) that build up a FastAPI CRUD API one feature at a time. Each spec is a separate pipeline run; later runs see the code produced by earlier ones. This demonstrates that the pipeline is compositional, not a one-shot demo.

### Comprehensive Test Suite

The pipeline has its own 20-file test suite independent of any pipeline run:

- **14 unit tests** — sandbox, intake, gates, agent tools, repair loop, mock provider, audit store, planner, testgen, normalisation, dashboard.
- **2 integration tests** — full happy-path and repair-loop end-to-end runs via mock provider; no API keys needed.

All tests are run in CI on every push.

---

## Deliverables Checklist

| Deliverable | Status |
|---|---|
| Source code repository | Included |
| README with setup and execution instructions | `README.md` |
| Example feature specification | `specs/example.{md,yaml,json}`, `specs/crud/` |
| Demonstration of an end-to-end run | `README.md` Quick Start; Docker; CI artifacts |
| Architectural explanation (design decisions, trade-offs, limitations, future improvements) | `ARCHITECTURE.md` |
| GitHub Actions integration | `.github/workflows/pipeline-ci.yml` |
| Containerised setup | `docker/` |
| Agent orchestration | Multi-turn tool-use in codegen + repair |
| Evaluation metrics | Token counts, latency, estimated cost per stage |
| Prompt/version management | `pipeline/llm/prompts/v1/`, pinned per run |
| Observability dashboard | `dashboard/` |

---

## Future Architecture

See [FUTURE.md](FUTURE.md) for the full document.

[FUTURE.md](FUTURE.md) captures the architectural patterns and capabilities observed across the frontier of AI coding tools — Claude Code (leaked TypeScript source), OpenAI Codex CLI (open source), and Cursor — and what they point to for the next generation of spec-driven pipelines. Covers: PTY emulation, three-tier permission systems, blast radius prediction, AST-aware semantic search, shadow workspaces and Git worktrees, multi-agent orchestration, Guardian AI real-time approval, self-healing loops, Skills systems, MCP integration, prompt caching economics, stateful session persistence, browser-in-the-loop validation, atomic DAG refactoring, live async developer feedback, and WASM/container runtime isolation.

---

## Viewing the Architecture Diagrams

Architecture diagrams are in [DIAGRAMS.md](DIAGRAMS.md), written in [Mermaid](https://mermaid.js.org/) and embedded in Markdown code fences. To render them inside VS Code, install the official [Mermaid Chart](https://marketplace.visualstudio.com/items?itemName=MermaidChart.vscode-mermaid-chart) extension by the Mermaid open source team, then open `DIAGRAMS.md` and press `Ctrl+Shift+V` (or `Cmd+Shift+V` on Mac) to open the Markdown preview — the diagrams will render inline.

---

## Notes and Assumptions

- The pipeline does not require a git repository for the target. It diffs against the working tree in memory.
- The mock provider returns the same canned outputs regardless of spec. It demonstrates pipeline mechanics and CI determinism; it does not evaluate LLM intelligence.
- The repair agent shares the same sandbox as codegen — it cannot escape `plan.impacted_files`. This is a deliberate safety decision, not a limitation to remove.
- The dashboard has no authentication. This is appropriate for a local prototype; production deployment would require at minimum HTTP basic auth or an SSO proxy in front of the approval endpoints.
- Anthropic prompt caching is used on the system prompt across all three LLM calls in a run, amortising the large instruction tokens. This is visible in `prompt_calls.cache_creation_input_tokens` and `cache_read_input_tokens` in the audit DB.
