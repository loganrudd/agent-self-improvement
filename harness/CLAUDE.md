# CLAUDE.md — harness/ (owner: Rohan)

## What this stage does
Runs the text-to-SQL agent over a Spider query stream and emits one `TelemetryRecord`
per run. Owns the difficulty-shift feed and the execution-based eval. This is the SOURCE
of the telemetry the whole loop watches.

## Contract — LOCKED (see /.claude/rules/01-contracts.md)
- **Emits:** `TelemetryRecord` (one per run) -> appended to `events.jsonl` via `contracts.eventlog.append_event`.
- **Reads:** `AgentConfig` — including `few_shot_examples`, which START EMPTY and GROW as
  correction feeds learned examples back. You MUST use `config.few_shot_examples` when
  prompting the agent — that's how recovery happens.
- You are the source; you have no input mock. Your input is real Spider data.

## Locked decisions that constrain you
- **Feed shape = change-point on a stratified stream** (rules/02): Phase 1 easy/med (high acc) ->
  shift to hard/extra (acc drops) -> Phase 3 same hard questions (acc recovers after learning).
  NOT random, NOT a gradual ramp.
- **Pre-compute the stream and support fast replay** for the demo — don't pay live latency for
  hundreds of calls. (Live-trigger the change-point; replay the rest.)
- **Eval = execution accuracy**: run generated + gold SQL against the Spider SQLite DB, compare
  RESULT SETS. Use Spider's eval logic for set comparison (row-order / duplicate normalization)
  or you'll get false mismatches. Don't string-compare query text.
- Base agent = weaker/faster Gemini tier (so it genuinely struggles on hard queries).

## What to build (files in this dir)
- `spider.py`  — load a Spider subset: schemas, questions (pooled by difficulty), gold SQL, SQLite DBs.
- `agent.py`   — text-to-SQL agent; prompt includes `config.few_shot_examples`; calls Gemini.
- `evaluator.py` — execute generated + gold SQL, compare result sets -> accuracy + validity;
                   compute generated/required complexity (joins+nesting).
- `feed.py`    — the change-point stream sampler (phase -> difficulty pool); replay mode.
- `runner.py`  — tie it together: for each item in the feed, run agent, eval, emit TelemetryRecord.

## Build/run
`python -m harness.runner`  (from repo root). Append records with `append_event(record)`.

---
## FLEXIBLE — Rohan's notes (add freely below)
<!-- design choices, Spider subset you picked, complexity heuristic, TODOs... -->
