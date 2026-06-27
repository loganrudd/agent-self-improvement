# Agent Self-Improvement

Drift detection and automated self-improvement for AI agents. A text-to-SQL agent
(evaluated on the Spider benchmark) is monitored in production; when its accuracy
drifts as query complexity rises, the system detects it and the agent **learns from
its own failures** to recover — autonomously.

Built for **The Self-Improvement Stack** track.

> **All code in this repository was written during the AI Engineer World's Fair
> Hackathon on June 27, 2026.** No pre-existing project code is included.

## Architecture
```
Harness ──TelemetryRecord──▶ Detector ──DriftEvent──▶ Correction
   ▲                                                       │
   └──────── learned few-shot examples (feedback) ─────────┘
                  all stages ──▶ events.jsonl ──▶ Viewer
```

## Quickstart
```bash
pip install -r requirements.txt
python fixtures/generate_mocks.py     # generate mock data for parallel dev
# then run your stage — see each directory's CLAUDE.md
```

## Layout
- `contracts/` — **frozen** shared schemas + event-log helper
- `harness/` — text-to-SQL agent, Spider eval, difficulty-shift feed (Rohan)
- `detector/` — windowed drift detection (Logan)
- `correction/` — learn-from-failures loop (Mihir)
- `viewer/` — live recovery-curve + SQL example panel (Yiwen)
- `fixtures/` — mock data generator
