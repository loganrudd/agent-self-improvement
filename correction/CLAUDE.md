# CLAUDE.md — correction/ (owner: Mihir)

## What this stage does
On a `DriftEvent`, makes the agent LEARN from its own failures: collect the failing cases,
get corrected SQL from a stronger teacher model, turn them into few-shot examples, and inject
them so the agent recovers on the hard distribution — without forgetting easy-query skill.
This is the continual-learning / memory heart of the project (your DER++ wheelhouse).

## Contract — LOCKED (see /.claude/rules/01-contracts.md)
- **Consumes:** `DriftEvent` (carries `failing_run_ids` + `failure_mode`).
- **Emits:** `CorrectionAction` with `new_few_shot_examples` -> appended to events.jsonl and to
  the agent's `AgentConfig.few_shot_examples` (the harness uses them next).
- **Build against:** `fixtures/mock_drift_events.jsonl` (+ make a small bundle of fake failing cases).

## Locked decisions that constrain you
- **LEARN FROM FAILURES — NOT model-swap** (rules/02). Do NOT "fix" drift by reverting to a bigger
  model. The agent must end BETTER than it started on the hard distribution because it LEARNED.
- **Teacher model** = stronger Gemini tier, used to GENERATE corrected SQL for failing questions
  (a teacher, not a permanent swap). The base agent stays the agent; it just gains examples.
- **Anchor against forgetting**: keep good easy-query examples too, so injecting hard-query
  examples doesn't regress the easy bucket (this is the DER++/memory angle — make it real but
  minimal first; sophistication only if time remains).
- `failure_mode` tells you what to learn: VALID_BUT_WRONG -> logic/join examples; INVALID_SQL ->
  structural/syntax examples.

## What to build (files in this dir)
- `teacher.py`     — given a failing (question, schema), get correct SQL from the teacher model.
- `learner.py`     — build & anchor FewShotExamples from failures (anti-forgetting memory).
- `correction.py`  — DriftEvent + failing cases -> CorrectionAction (the learned examples).

## Minimal honest version (if behind at hr 8)
Collect failing questions -> teacher generates correct SQL ONCE -> add as few-shots -> done.
Still non-circular, still shows the agent improving. Add anchoring/DER++ rigor only if time allows.

## Build/run
`python -m correction.correction --drift fixtures/mock_drift_events.jsonl`

---
## FLEXIBLE — Mihir's notes (add freely below)
<!-- anchoring strategy, how many examples, teacher prompt, forgetting metric... -->
