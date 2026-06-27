# Repo structure & what's locked vs flexible

## Locked (shared — change only by team agreement)
- `contracts/schemas.py` — FROZEN data contracts (the one file that breaks everyone if changed).
- `contracts/eventlog.py` — the single events.jsonl read/write helper.
- `.claude/rules/*.md` — architecture, contracts, tech decisions, compliance, workflow.
- root `CLAUDE.md` — imports the rules; auto-loaded in every Claude Code session here.

## Flexible (yours to build & extend)
- `harness/`     (Rohan)  — has its own CLAUDE.md: locked contract + a free notes section.
- `detector/`    (Logan)
- `correction/`  (Mihir)
- `viewer/`      (Yiwen)
Add implementation notes to YOUR directory's CLAUDE.md. Add personal/private notes to
`CLAUDE.local.md` (gitignored). You can add your own rule files under `.claude/rules/` for your
stage — prefix them so they sort after the shared ones (e.g. `10-detector-notes.md`).

## Start here
1. `pip install -e .`  (or `pip install -r requirements.txt`)
2. `python fixtures/generate_mocks.py`  — creates the mock data you build against.
3. Open your directory's `CLAUDE.md` and go. You do NOT need anyone else's stage running.

Full design rationale: see the build plan (three-person-build-plan.md / shared with the team).
