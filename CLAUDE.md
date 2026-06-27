# CLAUDE.md — Agent Self-Improvement (AIEWF Hackathon, Sat Jun 27)

> This file is auto-loaded by Claude Code in every session in this repo.
> It imports the **locked team rules** below. Do not duplicate them here — edit the rule files.

## What we're building (one sentence)
A self-improvement layer for AI agents: it watches a **text-to-SQL agent** evaluated on **Spider**, detects when accuracy **drifts** as queries get harder, and makes the agent **learn from its own failures** (teacher-generated few-shot examples) to recover — no human in the loop. Theme: **The Self-Improvement Stack**.

## The loop (4 stages, 4 owners)
```
 HARNESS (Rohan) --TelemetryRecord--> DETECTOR (Logan) --DriftEvent--> CORRECTION (Mihir)
     ^                                                                      |
     |  AgentConfig.few_shot_examples (starts empty; correction appends)    |
     +----------------------------------------------------------------------+
              all stages append typed events -> events.jsonl --> VIEWER (Yiwen)
```
The growing `few_shot_examples` list **is** the agent learning. That feedback path is the spine of the project.

## Ownership (work inside your own directory)
| Stage | Owner | Dir | Consumes | Emits |
|-------|-------|-----|----------|-------|
| Harness / telemetry | Rohan | `harness/`    | Spider data | `TelemetryRecord` |
| Detector            | Logan | `detector/`   | `TelemetryRecord` | `DriftEvent` |
| Correction / learning | Mihir | `correction/` | `DriftEvent` + failing cases | `CorrectionAction` |
| Viewer              | Yiwen | `viewer/`     | `events.jsonl` | (UI) |

Each directory has its own `CLAUDE.md` with the stage's locked contract + a free space for your notes. **Add your implementation notes there, not here.**

## Locked team rules (shared — change only by team agreement)
@.claude/rules/00-architecture.md
@.claude/rules/01-contracts.md
@.claude/rules/02-tech-decisions.md
@.claude/rules/03-compliance.md
@.claude/rules/04-workflow.md

## First thing, every session
1. Read your stage's `CLAUDE.md`.
2. Build against your **mock fixture** (run `python fixtures/generate_mocks.py` once to create them) — you do NOT need anyone else's stage running.
3. Never edit `contracts/schemas.py` without announcing it to the team — it breaks everyone.

## Run from repo root
All imports assume repo root on path: `from contracts.schemas import TelemetryRecord`. Run commands from the repo root (or `pip install -e .`).
