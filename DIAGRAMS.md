# Architecture diagrams

Two complementary views of the pipeline. The **high-level** chart shows the flow and the governance boundaries — the things every reader needs to understand to reason about a run. The **low-level** chart shows the module decomposition that mirrors the actual package tree, so you know which file owns each box on the high-level chart.

For prose context, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## High-level architecture

The pipeline is a linear sequence of eight stages, wrapped in two governance boundaries (the **sandbox** around file writes, the **gate suite** around the produced code). The non-deterministic LLM provider sits to the side; the deterministic audit store (filesystem + SQLite) sits below.

```mermaid
flowchart TD
    Spec[/"spec file<br/>(md / yaml / json)"/]

    subgraph PIPELINE["pipeline run (single process)"]
        direction TB
        Intake["1. intake<br/><sub>parse + validate</sub>"]
        Plan["2. plan<br/><sub>LLM → impacted_files</sub>"]
        Approve1{{"3. approval #1<br/><sub>cli / dashboard / auto</sub>"}}

        subgraph SANDBOX["sandbox boundary (plan.impacted_files)"]
            direction TB
            Codegen["4. codegen<br/><sub>LLM agent + tools</sub>"]
            Testgen["5. testgen<br/><sub>LLM → pytest files</sub>"]
            Repair["repair_N<br/><sub>LLM agent, ≤ MAX_REPAIR_ATTEMPTS</sub>"]
        end

        subgraph GATES["6. gates (fail-closed)"]
            direction LR
            Policy["policy"]
            Ruff["ruff"]
            Mypy["mypy"]
            Bandit["bandit"]
            Pytest["pytest<br/>+ AC coverage"]
        end

        Approve2{{"7. approval #2<br/><sub>cli / dashboard / auto</sub>"}}
        Finalize["8. finalize<br/><sub>deployment_evidence.json</sub>"]
    end

    LLM[("LLM provider<br/>mock | anthropic | openai")]

    subgraph AUDIT["audit (every stage writes here)"]
        direction TB
        Runs[("runs/&lt;run-id&gt;/<br/><sub>spec, plan, patches,<br/>prompts.jsonl, gate logs,<br/>deployment evidence</sub>")]
        DB[("audit.db<br/><sub>SQLite: runs, stages,<br/>prompt_calls, approvals,<br/>gate_results, metrics</sub>")]
    end

    Dashboard["dashboard (FastAPI)<br/><sub>read-mostly UI +<br/>approve/reject endpoint</sub>"]

    Spec --> Intake --> Plan --> Approve1 --> Codegen --> Testgen --> GATES
    GATES -->|all passed| Approve2 --> Finalize
    GATES -.->|any failed| Repair
    Repair --> GATES

    Plan -.-> LLM
    Codegen -.-> LLM
    Testgen -.-> LLM
    Repair -.-> LLM

    PIPELINE -.->|every stage| AUDIT
    AUDIT --> Dashboard
    Dashboard -.->|"dashboard mode:<br/>POST /approve"| Approve1
    Dashboard -.->|"dashboard mode:<br/>POST /approve"| Approve2

    classDef stage fill:#e8f0ff,stroke:#4a6fa5,color:#000
    classDef gov fill:#fff4e0,stroke:#b07020,color:#000
    classDef ext fill:#f0f0f0,stroke:#666,color:#000
    classDef approval fill:#e8ffe8,stroke:#3a8a3a,color:#000
    class Intake,Plan,Codegen,Testgen,Finalize,Repair stage
    class Policy,Ruff,Mypy,Bandit,Pytest gov
    class LLM,Runs,DB,Dashboard,Spec ext
    class Approve1,Approve2 approval
```

**Reading guide:**
- Solid arrows are control flow (stage → stage).
- Dashed arrows are I/O — LLM calls, audit writes, dashboard reads, and the dashboard-driven approval poll.
- The two governance boundaries are the **sandbox** (file paths must be in `plan.impacted_files`) and the **gate suite** (lint/types/tests/security/policy must all pass).
- The repair branch only fires when the gate suite has a failure; gates re-run after each repair attempt.

---

## Low-level architecture

Module-level view. The orchestrator at the centre calls into each package; each package's main file is shown. Subgraph boundaries match the `pipeline/` directory tree.

```mermaid
flowchart LR
    CLI["cli.py<br/><sub>typer app:<br/>run | validate | status | approve</sub>"]
    Dash["dashboard/app.py<br/><sub>FastAPI + Jinja2</sub>"]
    Config["config.py<br/><sub>Settings + .env loader</sub>"]
    Orch["orchestrator.py<br/><sub>run_pipeline()</sub>"]

    subgraph INTAKE["pipeline/intake/"]
        Parser["parser.py<br/><sub>md/yaml/json → dict</sub>"]
        Validator["validator.py<br/><sub>REQUIRED_SECTIONS</sub>"]
        Schema["schema.py<br/><sub>FeatureSpec, AcceptanceCriterion</sub>"]
    end

    subgraph PLANNING["pipeline/planning/"]
        Planner["planner.py<br/><sub>plan_from_spec() → Plan</sub>"]
    end

    subgraph APPROVAL["pipeline/approval/"]
        Workflow["workflow.py<br/><sub>request_approval()<br/>cli | dashboard | auto</sub>"]
    end

    subgraph IMPL["pipeline/implementation/"]
        Codegen["codegen.py"]
        AgentTools["agent_tools.py<br/><sub>read/write/list tools</sub>"]
        Sandbox["sandbox.py<br/><sub>path traversal / symlink /<br/>impacted_files check</sub>"]
        Diff["diff.py"]
        RepairMod["repair.py<br/><sub>run_repair_agent()</sub>"]
        CGSchema["codegen_schema.py"]
    end

    subgraph TESTING["pipeline/testing/"]
        Testgen["testgen.py"]
    end

    subgraph GATES["pipeline/gates/"]
        Runner["runner.py<br/><sub>run_gates()</sub>"]
        PolicyGate["policy_gate.py"]
        RuffGate["ruff_gate.py"]
        MypyGate["mypy_gate.py"]
        BanditGate["bandit_gate.py"]
        PytestGate["pytest_gate.py<br/><sub>+ AC coverage</sub>"]
    end

    subgraph LLM["pipeline/llm/"]
        Client["client.py<br/><sub>LLMClient protocol +<br/>build_client()</sub>"]
        Mock["providers/mock.py<br/><sub>&lt;&lt;STAGE:*&gt;&gt; sentinels</sub>"]
        Anthropic["providers/anthropic.py<br/><sub>prompt caching</sub>"]
        OpenAI["providers/openai.py"]
        Prompts[("prompts/v1/<br/><sub>plan.md, codegen_agent.md,<br/>testgen.md, repair.md</sub>")]
    end

    subgraph AUDIT["pipeline/audit/"]
        Store["store.py<br/><sub>AuditStore, RunRecord</sub>"]
        SQL[("schema.sql<br/><sub>runs, stages, prompt_calls,<br/>approvals, gate_results, metrics</sub>")]
    end

    subgraph METRICS["pipeline/metrics/"]
        Recorder["recorder.py<br/><sub>MetricsRecorder</sub>"]
    end

    CLI --> Config
    CLI --> Orch
    Dash --> Config
    Dash --> Store

    Orch --> Parser --> Validator --> Schema
    Orch --> Planner
    Orch --> Workflow
    Orch --> Codegen
    Codegen --> AgentTools
    Codegen --> Sandbox
    Codegen --> Diff
    Codegen --> CGSchema
    Orch --> Testgen
    Testgen --> Sandbox
    Orch --> RepairMod
    RepairMod --> AgentTools
    RepairMod --> Sandbox
    Orch --> Runner
    Runner --> PolicyGate
    Runner --> RuffGate
    Runner --> MypyGate
    Runner --> BanditGate
    Runner --> PytestGate

    Planner --> Client
    Codegen --> Client
    Testgen --> Client
    RepairMod --> Client
    Client --> Mock
    Client --> Anthropic
    Client --> OpenAI
    Client --> Prompts

    Orch --> Store
    Orch --> Recorder
    Store --> SQL
    Workflow --> Store

    classDef entry fill:#e8f0ff,stroke:#4a6fa5,color:#000
    classDef core fill:#fff4e0,stroke:#b07020,color:#000
    classDef io fill:#f0f0f0,stroke:#666,color:#000
    class CLI,Dash,Config,Orch entry
    class Sandbox,Workflow,Runner core
    class Prompts,SQL io
```

**Reading guide:**
- `cli.py` and `dashboard/app.py` are the two entry points; both load `config.py` `Settings` and operate on `AuditStore`.
- `orchestrator.py` is the only module that calls every stage; treat it as the index into the rest of the package.
- The **sandbox** module is reused by codegen, testgen *and* repair — that is what makes "the repair agent shares the planner-approved sandbox" true in code, not just doctrine.
- `LLMClient` is one protocol with three implementations; the mock dispatches on `<<STAGE:*>>` sentinels in the prompt template so CI can run end-to-end without API keys.
- The audit store is the single index over both the SQLite tables and the per-run filesystem directory — the dashboard reads exclusively through it.

---

## Sequence diagram — one run, dashboard approval mode

This is the temporal view of a single `pipeline run spec.yaml --approval-mode dashboard` invocation. Every arrow corresponds to a real call in `pipeline/orchestrator.py` (and the modules it calls). `cli` mode replaces the dashboard-polling block with a blocking `input()` on stdin; `auto` mode skips it entirely and records `approver=auto`.

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant CLI as cli.py<br/>(typer)
    participant Orch as orchestrator<br/>.run_pipeline
    participant Intake as intake<br/>(parser+validator)
    participant Audit as AuditStore<br/>(audit.db + runs/)
    participant Plan as planner
    participant LLM as LLM provider<br/>(mock|anthropic|openai)
    participant Agent as ToolLoop<br/>(agent_tools)
    participant Sbx as sandbox<br/>.validate_paths
    participant Tgt as target_dir<br/>(working tree)
    participant Gates as gates.runner
    participant Apv as approval<br/>.workflow
    participant Dash as dashboard<br/>(FastAPI)

    Op->>CLI: pipeline run spec.yaml --approval-mode dashboard
    CLI->>Orch: run_pipeline(spec, settings, mode=DASHBOARD)
    Orch->>Intake: load_and_validate(spec_path)
    Intake-->>Orch: FeatureSpec (or SpecValidationError -> fail closed)
    Orch->>Audit: start_run() -> RunRecord, mkdir runs/run-id/
    Orch->>Audit: write_json_artifact(spec.json) + copy spec_source.*

    Note over Orch,Plan: 2. Plan
    Orch->>Plan: plan_from_spec(spec, target_dir, …)
    Plan->>LLM: complete(system=prompts/v1/plan.md, prompt=spec+tree)
    LLM-->>Plan: Plan JSON {tasks, design_summary, impacted_files, risks, test_strategy}
    Plan->>Audit: append prompts.jsonl + prompt_calls row
    Plan->>Audit: write_json_artifact(plan.json)
    Plan-->>Orch: Plan (validated)

    Note over Orch,Dash: 3. Approval #1 (plan)
    Orch->>Apv: request_approval(checkpoint=plan, mode=DASHBOARD)
    Apv->>Audit: set_run_status(awaiting_approval, stage=approval:plan)
    loop poll every 2 s (timeout 1800 s)
        Apv->>Audit: approvals_for_run(run_id)
        Audit-->>Apv: []
    end
    Op->>Dash: click Approve plan
    Dash->>Audit: POST /runs/id/approve/plan -> record_approval(decision=approved)
    Apv->>Audit: approvals_for_run(run_id)
    Audit-->>Apv: [{checkpoint=plan, decision=approved}]
    Apv->>Audit: set_run_status(running)
    Apv-->>Orch: ApprovalDecision

    Note over Orch,Tgt: 4. Codegen (tool-using agent)
    Orch->>Agent: run_loop(initial_user_message=spec+plan+tree)
    loop up to PIPELINE_MAX_AGENT_TURNS (default 12)
        Agent->>LLM: complete_with_tools(system, msgs, [list_files, read_file, write_files])
        LLM-->>Agent: tool_uses [list_files / read_file / write_files]
        alt tool = list_files / read_file
            Agent->>Tgt: read tree / file (path bounded to target_dir)
            Agent->>Audit: append prompts.jsonl (tool_result preview)
        else tool = write_files
            Agent->>Sbx: validate_paths(files, plan.impacted_files, target_dir)
            alt sandbox OK
                Sbx-->>Agent: ok
                Note over Agent: terminate loop
            else SandboxViolation
                Sbx-->>Agent: error
                Agent->>Audit: prompts.jsonl (is_error=true)
                Note over Agent: feed error back to model — 2 in a row triggers AgentLoopError
            end
        end
    end
    Agent-->>Orch: GeneratedChanges {files, summary}
    Orch->>Tgt: apply_changes — write file contents
    Orch->>Audit: write patches/codegen.patch (unified diff)
    Orch->>Audit: write codegen_output.json

    Note over Orch,Tgt: 5. Testgen (single-shot LLM, no tools)
    Orch->>LLM: complete(system=prompts/v1/testgen.md, prompt=plan+impl)
    LLM-->>Orch: GeneratedChanges (test files)
    Orch->>Sbx: validate_paths(test files)
    Sbx-->>Orch: ok
    Orch->>Tgt: apply_changes
    Orch->>Audit: write patches/testgen.patch

    Note over Orch,Gates: 6. Gates (fail-closed, sequential)
    Orch->>Gates: run_gates(target_dir, plan, impl, tests)
    Gates->>Tgt: subprocess: policy -> ruff -> mypy -> bandit -> pytest
    Tgt-->>Gates: stdout/stderr per gate
    Gates->>Audit: write gate_STAGE_GATE.log + gate_results rows
    Gates-->>Orch: GateReport {outcomes, all_passed}

    Note over Orch,Tgt: 6b. Repair loop (only if a gate failed)
    alt gate_report.all_passed == false
        loop up to PIPELINE_MAX_REPAIR_ATTEMPTS (default 2)
            Orch->>Agent: run_loop with REPAIR_TOOLS<br/>(adds read_gate_log)
            Note right of Agent: same sandbox = plan.impacted_files
            Agent->>LLM: complete_with_tools
            LLM-->>Agent: tool_uses
            Agent->>Audit: prompts.jsonl + prompt_calls (stage=repair_N.turnK)
            Agent-->>Orch: GeneratedChanges (fixes)
            Orch->>Tgt: apply_changes
            Orch->>Audit: write patches/repair_N.patch
            Orch->>Gates: run_gates(stage_label=gates_after_repair_N)
            Gates-->>Orch: GateReport
            alt all_passed
                Note over Orch: break
            end
        end
    end
    Orch->>Audit: write ac_coverage.json
    alt gates still failing OR AC coverage incomplete
        Orch->>Audit: set_run_status(failed)
        Orch-->>CLI: PipelineError -> exit 1
    end

    Note over Orch,Dash: 7. Approval #2 (finalize) — same flow as #1
    Orch->>Apv: request_approval(checkpoint=finalize, mode=DASHBOARD)
    Op->>Dash: click Approve finalize
    Dash->>Audit: POST /runs/run-id/approve/finalize
    Apv-->>Orch: ApprovalDecision

    Note over Orch,Audit: 8. Finalize
    Orch->>Audit: write deployment_evidence.json (plan + impl + tests + gates + AC coverage)
    Orch->>Audit: set_run_status(succeeded)
    Orch->>Audit: record_metrics (tokens, duration, AC, gates)
    Orch-->>CLI: RunResult
    CLI-->>Op: print run_id + tokens + AC coverage + gate counts
```

**Notes that the diagram compresses:**
- Every LLM round-trip writes both to `runs/<run-id>/prompts.jsonl` (full transcript) and `audit.db` `prompt_calls` (indexable summary). For the agent loop, each turn becomes its own `prompt_calls` row (`stage="codegen.turn3"`, etc.) — see `agent_tools.ToolLoop._persist_turn`.
- The `cli` mode replaces steps 14–19 with a single `input()` on stdin; the `auto` mode replaces them with one `record_approval(approver="auto")` call and no waiting.
- Sandbox violations during `write_files` are returned to the model as a `tool_result` error so it can retry — only **two consecutive** sandbox errors abort the stage. See `ToolLoop.run_loop` (`consecutive_sandbox_errors`).
- All stage timings are persisted in `audit.db` `stages` rows; the orchestrator's `_stage()` helper wraps every block in start/finish calls so even failures get a duration.

---

## Data-flow diagram

What enters, what changes, what is produced. Boxes with rounded corners are processes; square/cylindrical boxes are data stores; the dashed perimeter is the run's audit footprint.

```mermaid
flowchart LR
    %% --- Inputs ---
    SpecFile[/"spec.{md,yaml,json}<br/>name, objective, user_story,<br/>business_rules, acceptance_criteria,<br/>non_functional, out_of_scope"/]
    TargetIn[("target_dir/<br/>(existing working tree)")]
    EnvFile[/".env<br/>provider, models,<br/>budgets, paths"/]

    %% --- Processes (rounded) ---
    Parse(["parser.parse_spec_file<br/>→ dict"])
    Validate(["validator.validate_spec<br/>→ FeatureSpec<br/>(min_length=1 on all lists)"])
    PlanGen(["planner.plan_from_spec<br/>LLM call (single shot)"])
    Approve1{{"approval.request_approval<br/>checkpoint=plan"}}
    CodegenAgent(["codegen.generate_code<br/>+ ToolLoop (multi-turn)"])
    SandboxCheck{{"sandbox.validate_paths<br/>vs plan.impacted_files"}}
    ApplyCode(["diff.apply_changes<br/>kind=codegen"])
    TestgenStage(["testgen.generate_tests<br/>LLM call (single shot)"])
    ApplyTests(["diff.apply_changes<br/>kind=testgen"])
    GatesRun(["gates.runner.run_gates<br/>5 subprocesses"])
    RepairAgent(["repair.run_repair_agent<br/>+ ToolLoop<br/>(repair_1 … repair_N)"])
    ApplyRepair(["diff.apply_changes<br/>kind=repair_N"])
    ACCalc(["pytest_gate.compute_ac_coverage<br/>parse 'AC: AC-N' markers"])
    Approve2{{"approval.request_approval<br/>checkpoint=finalize"}}
    Finalize(["orchestrator._finalize<br/>(bundle assembly)"])

    %% --- Intermediate data shapes (in-memory) ---
    SpecObj[["FeatureSpec<br/>+ content_hash()"]]
    PlanObj[["Plan<br/>{tasks, design_summary,<br/>impacted_files, risks, test_strategy}"]]
    Changes1[["GeneratedChanges<br/>{files[path, action, content],<br/>summary}"]]
    Changes2[["GeneratedChanges<br/>(tests)"]]
    GateObj[["GateReport<br/>{outcomes[name, passed,<br/>duration, summary], all_passed}"]]
    Fixes[["GeneratedChanges<br/>(repair fixes)"]]

    %% --- Run audit footprint (filesystem) ---
    subgraph RUN_FS["runs/&lt;run-id&gt;/ (per-run filesystem)"]
        direction TB
        ASpec[/"spec.json<br/>spec_source.{md,yaml,json}"/]
        APlan[/"plan.json"/]
        APromptsJ[/"prompts.jsonl<br/>(every LLM turn + tool_result)"/]
        APatches[/"patches/<br/>codegen.patch<br/>testgen.patch<br/>repair_N.patch"/]
        ACodeOut[/"codegen_output.json<br/>(turns, tool_calls, files)"/]
        AGateLogs[/"gate_gates_{ruff,mypy,pytest,<br/>bandit,policy}.log<br/>+ gates.json<br/>(+ gate_gates_after_repair_N_*.log)"/]
        AACov[/"ac_coverage.json"/]
        AEvidence[/"deployment_evidence.json"/]
    end

    %% --- Run audit footprint (SQLite) ---
    subgraph RUN_DB["audit.db (SQLite tables)"]
        direction TB
        TRuns[("runs<br/>spec_hash, provider, model,<br/>prompt_version, current_stage, status")]
        TStages[("stages<br/>name, status, duration_ms")]
        TPrompts[("prompt_calls<br/>stage, tokens, latency, artifact")]
        TApprovals[("approvals<br/>checkpoint, decision, approver")]
        TGateRes[("gate_results<br/>gate, status, summary, artifact")]
        TMetrics[("metrics<br/>tokens, duration, ac, gates")]
    end

    %% --- External providers ---
    LLMSvc[("LLM provider<br/>mock | anthropic | openai")]

    %% --- Output: mutated target ---
    TargetOut[("target_dir/<br/>(mutated in place)<br/>+ new files,<br/>+ tests/test_*.py")]

    %% --- Edges ---
    SpecFile --> Parse --> Validate --> SpecObj
    EnvFile -.-> PlanGen
    EnvFile -.-> CodegenAgent
    EnvFile -.-> TestgenStage
    SpecObj --> PlanGen
    TargetIn -. snapshot tree .-> PlanGen
    PlanGen <-.-> LLMSvc
    PlanGen --> PlanObj
    PlanObj --> Approve1

    Approve1 --> CodegenAgent
    SpecObj --> CodegenAgent
    PlanObj --> CodegenAgent
    TargetIn -. read tools .-> CodegenAgent
    CodegenAgent <-.-> LLMSvc
    CodegenAgent --> Changes1 --> SandboxCheck
    PlanObj -. impacted_files .-> SandboxCheck
    SandboxCheck -->|ok| ApplyCode
    SandboxCheck -.->|violation| CodegenAgent

    PlanObj --> TestgenStage
    Changes1 --> TestgenStage
    TestgenStage <-.-> LLMSvc
    TestgenStage --> Changes2 --> ApplyTests
    PlanObj -. impacted_files .-> ApplyTests

    ApplyCode --> TargetOut
    ApplyTests --> TargetOut
    ApplyCode --> APatches
    ApplyTests --> APatches

    TargetOut --> GatesRun
    GatesRun --> GateObj
    GatesRun --> AGateLogs

    GateObj -. all_passed=false .-> RepairAgent
    PlanObj --> RepairAgent
    AGateLogs -. read_gate_log tool .-> RepairAgent
    RepairAgent <-.-> LLMSvc
    RepairAgent --> Fixes --> ApplyRepair
    ApplyRepair --> TargetOut
    ApplyRepair --> APatches
    ApplyRepair -. trigger .-> GatesRun

    TargetOut --> ACCalc --> AACov
    GateObj --> Approve2
    AACov --> Approve2
    Approve2 --> Finalize
    PlanObj --> Finalize
    Changes1 --> Finalize
    Changes2 --> Finalize
    GateObj --> Finalize
    Finalize --> AEvidence

    %% Audit fan-out (dashed = every stage writes here)
    Validate -.-> ASpec
    Validate -.-> TRuns
    PlanGen -.-> APlan
    PlanGen -.-> APromptsJ
    PlanGen -.-> TPrompts
    Approve1 -.-> TApprovals
    CodegenAgent -.-> APromptsJ
    CodegenAgent -.-> TPrompts
    CodegenAgent -.-> ACodeOut
    TestgenStage -.-> APromptsJ
    TestgenStage -.-> TPrompts
    GatesRun -.-> TGateRes
    RepairAgent -.-> APromptsJ
    RepairAgent -.-> TPrompts
    Approve2 -.-> TApprovals
    Finalize -.-> TMetrics

    %% Every stage writes a stages row
    PlanGen -.-> TStages
    CodegenAgent -.-> TStages
    TestgenStage -.-> TStages
    GatesRun -.-> TStages
    RepairAgent -.-> TStages
    Finalize -.-> TStages

    classDef proc fill:#e8f0ff,stroke:#4a6fa5,color:#000
    classDef gov fill:#fff4e0,stroke:#b07020,color:#000
    classDef artifact fill:#f5f5f5,stroke:#888,color:#000
    classDef ext fill:#fce8e8,stroke:#a04040,color:#000
    classDef inmem fill:#e8ffe8,stroke:#3a8a3a,color:#000
    class Parse,Validate,PlanGen,CodegenAgent,TestgenStage,GatesRun,RepairAgent,ACCalc,Finalize,ApplyCode,ApplyTests,ApplyRepair proc
    class SandboxCheck,Approve1,Approve2 gov
    class SpecFile,EnvFile,TargetIn,TargetOut,LLMSvc ext
    class SpecObj,PlanObj,Changes1,Changes2,GateObj,Fixes inmem
    class ASpec,APlan,APromptsJ,APatches,ACodeOut,AGateLogs,AACov,AEvidence,TRuns,TStages,TPrompts,TApprovals,TGateRes,TMetrics artifact
```

**What this view makes visible that the high/low-level diagrams hide:**

1. **The plan is the contract.** `plan.impacted_files` feeds *three* sandbox checks (codegen, testgen, repair). One human approval at checkpoint #1 governs every subsequent disk write.
2. **The audit footprint is two-sided.** Heavy payloads (full prompt transcripts, gate logs, patches, the evidence bundle) live on the filesystem under `runs/<run-id>/`; SQLite holds indexable metadata only. Both are written from inside each stage — the audit fan-out is not a separate "logging" stage.
3. **The repair branch is a true loop in the data-flow sense.** Gate logs flow *back into* the LLM via the `read_gate_log` tool, repair output goes back through the same sandbox, and gates are re-run on the mutated target — all bounded by `PIPELINE_MAX_REPAIR_ATTEMPTS` so the loop must terminate.
4. **Reproducibility inputs.** A run is fully replayable from `spec.json` + `runs` row (`spec_hash`, `provider`, `model`, `prompt_version`) + `prompts.jsonl`. Everything else is a deterministic function of those.
