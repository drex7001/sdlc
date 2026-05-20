# Architecture

## Design philosophy

The prompt asks for an AI-native pipeline that demonstrates *governance, reliability and developer experience*, and is explicit that **architecture and execution matter more than UI polish**. The design therefore treats LLM steps as **non-deterministic and untrusted**, and wraps them in deterministic boundaries that can be reasoned about and audited.

```
spec ─► intake ─► plan ─► approval#1 ─► codegen ─► testgen ─► gates ─► approval#2 ─► finalize
                                                ↑                ↑
                                          deterministic     deterministic
                                            sandbox            gates
```

Each stage is a small function `(input, context) → output`. Side effects (LLM calls, filesystem writes, gate subprocesses) are injected — every stage can be unit-tested in isolation, and the integration test uses the mock provider to exercise the full flow without API keys.

## Key design decisions

### 1. Structured file operations, not LLM-emitted diffs

The codegen stage asks the LLM to return a JSON document of `{path, action, content}` entries — the **complete final file contents**, not a unified diff. Reasons:

- LLM-generated diffs are notoriously brittle (off-by-one line numbers, missing context lines, hallucinated hunks).
- The pipeline computes the actual diff from `before/after` for audit, so a real diff still ends up in `runs/<run-id>/patches/`.
- This also makes the sandbox check trivial: a flat list of paths to validate against `plan.impacted_files`.

### 2. The plan is a governance contract

`plan.impacted_files` is more than a hint — it is the **only** set of paths codegen and testgen are allowed to touch. The sandbox rejects anything outside this list before disk is touched. This collapses "what is the AI allowed to change?" into one auditable artefact that humans approve at checkpoint #1.

### 3. Two governance boundaries around LLM steps

| Boundary | Catches | Implementation |
| --- | --- | --- |
| **Sandbox** | Files written outside the approved set; path traversal; symlink escapes; absolute paths. | `pipeline/implementation/sandbox.py` |
| **Gates** | Lint, types, tests, security, custom policy violations. | `pipeline/gates/*` |

The custom **policy gate** is deliberately deterministic and complements the third-party tools. It catches things that pass `bandit` and `ruff` but matter for AI-generated code: hardcoded secrets, `eval`/`exec`, network/subprocess calls in tests, and a defence-in-depth re-check that paths are in `plan.impacted_files`.

### 4. Audit: SQLite index + filesystem artifacts

- **Filesystem** (`runs/<run-id>/`) holds the large payloads — every LLM request and response in `prompts.jsonl`, patches, gate stdout/stderr, the deployment evidence JSON.
- **SQLite** (`audit.db`) indexes runs/stages/approvals/gates/metrics for fast dashboard queries.
- Both are written in every stage. A run is reproducible: spec hash + prompt template version + provider + model are recorded, and the mock provider gives byte-identical replays.

### 5. Provider-pluggable LLM abstraction

The `LLMClient` protocol is one method: `complete(system, prompt, model, ...)`. Providers (`anthropic`, `openai`, `mock`) translate to native SDKs. The Anthropic implementation uses ephemeral prompt caching on the system prompt, so the (large) instructions amortise across the three LLM calls in one run. The mock provider dispatches on a sentinel embedded in each prompt template (`<<STAGE:PLAN>>` etc.) — it does not need to understand the spec, only which stage is asking. This is exactly what makes the full pipeline runnable in CI without API keys.

### 6. Versioned prompts

Prompts live under `pipeline/llm/prompts/v1/`. The version used by a run is recorded in `runs.prompt_version`. Rolling forward is a directory copy + an env var change, and any run can be replayed against its frozen version. A future eval harness would score N versions against a golden spec set.

### 7. Approval modes share one persistence model

`cli`, `dashboard`, and `auto` modes all write to the same `approvals` table. The CLI prompts on stdin, the dashboard exposes a POST endpoint, and `auto` records itself as approver=`auto`. The orchestrator does not branch on mode — it calls `request_approval()` which encapsulates the difference. This means the dashboard works even for a run that was started under CLI mode (the operator can switch terminals).

## Trade-offs

| Choice | Trade-off |
| --- | --- |
| Structured file ops instead of diffs | Slightly more output tokens; in return, dramatically more reliable application and trivial sandbox checks. |
| Single-process synchronous orchestrator | Easy to reason about, easy to test. Cannot run two pipelines simultaneously without DB-level locking. |
| SQLite + filesystem rather than a real DB | Zero deployment cost, great for a prototype. Not suitable for multi-tenant production audit. |
| Mock provider dispatches on sentinel | Decouples canned responses from prompt wording but means a typo in the sentinel breaks the demo silently — surfaced as a clear error message in the mock. |
| Five gates run sequentially | One slow gate cannot be parallelised; in return, the gate log is easy to follow. Parallel execution is straightforward to add. |
| No git operations | The pipeline mutates the target's working tree but does not commit. Demoing rollback is therefore a manual `git checkout`. |
| Generated tests are run inside the same process tree | Faster than spawning an isolated environment; the policy gate compensates by rejecting network/subprocess in tests. |

## Limitations

- **Repair loop is bounded and sandbox-scoped.** Up to `PIPELINE_MAX_REPAIR_ATTEMPTS` (default 2) repair attempts are made after gate failure (see `pipeline/implementation/repair.py`). The repair agent shares the same sandbox as codegen — if the real fix is outside `plan.impacted_files`, it cannot escape, and the run still halts. That's by design; the alternative widens the blast radius.
- **Approval is single-reviewer.** Production governance needs N-of-M, role-based approval and per-environment policies.
- **Dashboard has no auth.** Anyone who can reach the host can approve.
- **Sandbox is path-level, not capability-level.** Generated code can still do anything within an approved file. A production version would also sandbox imports/syscalls (e.g. via `RestrictedPython`, a subinterpreter, or a containerised executor).
- **Mock provider returns the same response regardless of spec.** It demonstrates pipeline mechanics, not LLM intelligence. Real provider runs respond to the actual spec.
- **No concurrent runs.** SQLite is single-writer; the orchestrator does not lock. Easy to fix with a row-level claim but not done.
- **Prompt registry is filesystem-based.** Fine for a prototype; production wants a registry with diffs, eval scores, and rollback.

## Future improvements

1. **Multi-agent orchestration.** Split planner / coder / tester / reviewer into named agents with explicit handoffs. The current sequential orchestrator is the natural starting point.
2. **Tool-using planner / testgen.** Codegen and repair already drive a multi-turn Anthropic `tool_use` loop via `pipeline/implementation/agent_tools.py`. Planning and testgen are still single-shot — promoting them would let the planner explore the repo before committing to `impacted_files`.
3. **Eval harness.** A `bench/` directory with N golden specs, expected gate outcomes, and a `pipeline bench` command that scores each prompt version.
4. **Spec → diff cost estimation.** Static analysis of `plan.impacted_files` could predict cost (LOC churn, blast radius) and require higher approval for large changes.
5. **Real-time dashboard updates.** Replace polling with Server-Sent Events from the orchestrator.
6. **Provenance signing.** Sign the `deployment_evidence.json` with a CI key so downstream systems can verify the run before deploying.
7. **Postgres + object storage backend.** Drop-in replacement for SQLite + filesystem when audit volume grows.

## Where to read the code

If you only read three files, read these:

- `pipeline/orchestrator.py` — the seven-stage flow, top to bottom.
- `pipeline/implementation/sandbox.py` — what "governance" actually looks like in code.
- `pipeline/audit/store.py` — how reproducibility is preserved.
