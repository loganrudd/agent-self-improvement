# CLAUDE.md — detector/ (owner: Logan)

## What this stage does
Consumes the `TelemetryRecord` stream and detects DRIFT — a sustained shift in a windowed
channel aggregate — then emits a `DriftEvent`. This is the core of the project and the piece
that mirrors spacecraft anomaly detection pointed at agent telemetry.

## Contract — LOCKED (see /.claude/rules/01-contracts.md)
- **Consumes:** `TelemetryRecord` (from the harness / `events.jsonl`).
- **Emits:** `DriftEvent` -> appended to `events.jsonl`. Set `failure_mode` (from validity) and
  `failing_run_ids` (the degraded-window runs correction should learn from).
- **Build against:** `fixtures/mock_telemetry.jsonl` — you do NOT need the real harness.

## Locked decisions that constrain you
- **Statistical, NO model training** (rules/02). Rolling-window mean + threshold/z-score / EWMA.
- **Drift is detected over a WINDOW (~20-30 runs), never on a single query.** One bad query is an
  anomaly, not drift, and must not fire. The curve = the windowed aggregate.
- **Stratify accuracy by `difficulty`** — overall windowed accuracy fires the event, but track
  per-bucket accuracy too (it's the improvement signal the viewer/demo rely on).
- Diagnose `failure_mode`: validity high + accuracy low -> VALID_BUT_WRONG; validity low -> INVALID_SQL.

## What to build (files in this dir)
- `baseline.py`  — establish per-channel baseline (mean/std) from the steady Phase-1 window.
- `detector.py`  — maintain rolling windows; when a channel deviates past threshold, emit DriftEvent
                   with severity, window_mean, baseline_mean, failure_mode, failing_run_ids.

## You also own architecture
`contracts/schemas.py` is yours to commit hour 1 (then frozen). Help build `orchestrator.py` at
integration. Keep the detection core clearly yours — it's the centerpiece.

## Build/run
`python -m detector.detector --input fixtures/mock_telemetry.jsonl`

---
## FLEXIBLE — Logan's notes (add freely below)
<!-- window size, threshold choice, EWMA vs z-score, stratification approach... -->
