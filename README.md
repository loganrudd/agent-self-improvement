# Agent Self-Improvement

Drift detection and automated self-improvement for AI agents. A text-to-SQL agent
(evaluated on the Spider benchmark) is monitored in production; when its accuracy
drifts as query complexity rises, the system detects it and the agent **learns from
its own failures** to recover — autonomously.

Built for **The Self-Improvement Stack** track at AI Engineer World's Fair Hackathon 2026.

> **All code in this repository was written during the AI Engineer World's Fair
> Hackathon on June 27, 2026.** No pre-existing project code is included.

## Results

| Phase | Accuracy | Queries |
|---|---|---|
| Baseline (easy/medium) | **0.82** | 40 |
| Degraded (hard/extra, no correction) | **0.68** | 60 |
| Recovery (same hard queries + learned examples) | **0.75** | 60 |

14pp drop detected automatically. 7pp recovery — same difficulty, no human in the loop.

## Architecture

```
Harness ──TelemetryRecord──▶ Detector ──DriftEvent──▶ Correction
   ▲                                                       │
   └──────── learned few-shot examples (feedback) ─────────┘
                  all stages ──▶ events.jsonl ──▶ Viewer
```

- **Harness** (Rohan): MiniMax M2.7 text-to-SQL agent, Spider benchmark eval, difficulty-shift feed
- **Detector** (Logan): windowed z-score drift detection, fires `DriftEvent` when accuracy drops
- **Correction** (Mihir): matches failing runs to gold SQL, injects diverse few-shot examples
- **Viewer** (Yiwen + Rohan): FastAPI + Chart.js recovery curve, reasoning panel, SQL before/after

## The Self-Improvement Loop

1. Agent runs on easy/medium queries — establishes baseline accuracy
2. Query distribution shifts to hard/extra (simulates production complexity creep)
3. Detector fires when windowed accuracy drops 14pp below baseline
4. Correction stage collects failing runs, matches gold SQL, injects as few-shot examples
5. Agent retries same hard queries WITH learned examples — accuracy recovers
6. Agent's own `<think>` reasoning block cites the injected examples — visible self-improvement

## Quickstart

```bash
pip install -r requirements.txt

# Run the full self-improvement loop
python orchestrator.py --baseline 40 --degraded 60 --recovery 60

# Launch the viewer (in a separate terminal)
VIEWER_LOG=events.jsonl uvicorn viewer.app:app --port 8000
# open http://127.0.0.1:8000
```

## Layout

- `contracts/` — frozen shared schemas + event-log helper
- `harness/` — text-to-SQL agent, Spider eval, difficulty-shift feed
- `detector/` — windowed drift detection
- `correction/` — learn-from-failures loop
- `viewer/` — live recovery curve, reasoning panel, SQL example panel
- `fixtures/` — Spider subset prep, mock data generator
- `orchestrator.py` — wires all four stages into a single live loop

## Team

Rohan Chavan · Logan Rudd · Mihir Agarwal · Yiwen
