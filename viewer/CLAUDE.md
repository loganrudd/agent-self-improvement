# CLAUDE.md — viewer/ (owner: Yiwen)

## What this stage does
Reads `events.jsonl` and renders the live demo view: the recovery curve, the channel values,
and the SQL example panel. This is HOW WE SHOW the system — it is intentionally thin and is
NOT the product.

## Contract — LOCKED (see /.claude/rules/01-contracts.md, 03-compliance.md)
- **Consumes:** `events.jsonl` (telemetry + drift + correction) via `contracts.eventlog`
  (`read_events`, `tail_events`). You do NOT touch the detector/harness/correction internals.
- **Build against:** `fixtures/mock_events.jsonl` — you do NOT need the real loop running.

## Locked decisions that constrain you
- **The dashboard is NOT the main feature, and NOT Streamlit** (rules/03 — DQ risk). Keep it minimal.
- Render three things:
  1. **Recovery curve** — windowed `execution_accuracy` over runs, ideally STRATIFIED BY DIFFICULTY
     (hard-bucket line is the money shot). Annotate the drift-detected point and the correction point.
  2. **Channel panel** — current values: accuracy, validity rate, complexity gap, latency.
  3. **SQL example panel** — current question + generated SQL + result (right/wrong); show it go
     wrong during degradation and corrected after learning. This is the visceral bit.

## Suggested stack
FastAPI serving one HTML page with Chart.js polling an endpoint that reads events.jsonl.
(Plain + robust; plays to your UI experience. Avoid Streamlit.)

## Build/run
`uvicorn viewer.app:app --reload`  then open the page; point it at `fixtures/mock_events.jsonl`.

---
## FLEXIBLE — Yiwen's notes (add freely below)
<!-- chart choices, polling interval, layout, how you animate the replay... -->
