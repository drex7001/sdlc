-- Schema for the pipeline audit database.
-- Filesystem under runs/<run_id>/ holds the large payloads; this DB indexes them.

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    spec_name         TEXT NOT NULL,
    spec_hash         TEXT NOT NULL,
    spec_source_path  TEXT NOT NULL,
    llm_provider      TEXT NOT NULL,
    llm_model         TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    status            TEXT NOT NULL,   -- pending | awaiting_approval | running | succeeded | failed | rejected
    current_stage     TEXT,
    approver          TEXT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    artifacts_dir     TEXT NOT NULL,
    target_dir        TEXT             -- nullable: NULL for runs from before this column existed
);

CREATE TABLE IF NOT EXISTS stages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,         -- running | succeeded | failed | skipped
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_ms INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_stages_run ON stages(run_id);

CREATE TABLE IF NOT EXISTS prompt_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms   INTEGER,
    artifact_path TEXT NOT NULL,       -- relative to artifacts_dir
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompts_run ON prompt_calls(run_id);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    checkpoint  TEXT NOT NULL,         -- plan | finalize
    decision    TEXT NOT NULL,         -- approved | rejected
    approver    TEXT NOT NULL,
    comment     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id);

CREATE TABLE IF NOT EXISTS gate_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    gate       TEXT NOT NULL,          -- ruff | mypy | pytest | bandit | policy
    status     TEXT NOT NULL,          -- passed | failed
    duration_ms INTEGER,
    summary    TEXT,
    artifact_path TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gates_run ON gate_results(run_id);

CREATE TABLE IF NOT EXISTS metrics (
    run_id        TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    total_tokens  INTEGER,
    total_duration_ms INTEGER,
    ac_total      INTEGER,
    ac_covered    INTEGER,
    gates_passed  INTEGER,
    gates_total   INTEGER
);
