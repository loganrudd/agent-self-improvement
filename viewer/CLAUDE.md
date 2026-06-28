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

### Decisions (locked with Yiwen)
- **Replay model:** front-end replay. Backend reads the log once via `read_events` and serves
  the full precomputed series; the front-end reveals it cursor-by-cursor (no polling for the
  mock demo). Swap to a poll/tail at integration against a live-growing `events.jsonl`.
- **Recovery curve strata:** bold **overall** windowed line (the V) + a combined **hard/extra**
  line (the money shot); **easy/medium** faint, baseline-only. A stratum line renders only while
  that difficulty is present in the trailing window (so easy fades out after the change-point).
- **Example panel:** verdict derived from `execution_accuracy` + `query_valid`
  (correct / valid_but_wrong / invalid). The "learned" beat pairs a failing record with the
  teacher's `correct_sql` from the correction event, matched by question text.
- **Window:** 20 runs.

### Phase A — backend (DONE)
- `viewer/app.py`. Server-side windowing (math in Python, thin JS). Reads via
  `contracts.eventlog.read_events` only.
- `GET /api/state` → `{window, n_runs, log, runs[], drift, correction}`.
  - `runs[k]` = snapshot as of run k: `run_index, run_id, difficulty, is_hard, accuracy_raw,
    valid, acc_overall, acc_hard, acc_easy, validity_rate, complexity_gap, latency_ms,
    question, generated_sql, db_id, verdict`. `acc_hard`/`acc_easy` are null when that stratum
    isn't active in the window.
  - `drift` / `correction` carry `at` = run-count when they fired (x-position for the markers);
    `correction.examples` = the teacher's few-shot pairs for the example panel.
- `GET /` = placeholder page (real UI in Phase B).
- Log path: defaults to `fixtures/mock_events.jsonl`; override with `VIEWER_LOG` env var
  (point at `events.jsonl` at integration).
- Run: `.venv/bin/uvicorn viewer.app:app --reload` (note: `:8000` may be busy — use `--port 8011`).
