# LOCKED RULE: Parallel workflow & git

## The trick that keeps four people unblocked: freeze seams, mock them
1. **Hour 1, together:** agree + commit `contracts/schemas.py`. Frozen after. Each person creates their input-seam mock (run `fixtures/generate_mocks.py`).
2. **Hours 1–5:** everyone builds their stage in isolation **against mocks**. Nobody runs anyone else's code yet.
3. **Integrate by swapping mocks for real outputs.** Same frozen schemas → it connects.

## Build against your mock
- Detector → `fixtures/mock_telemetry.jsonl`
- Correction → `fixtures/mock_drift_events.jsonl` (+ failing-case bundles)
- Viewer → `fixtures/mock_events.jsonl`
- Harness → real Spider data (it's the source; no input mock)

## INTEGRATION CHECKPOINT at hour 5–6 (do NOT skip)
Connect the rough pipe end-to-end — including the few-shot feedback path — while there's still time to fix schema mismatches. Four people integrating once at hour 10 is how teams fail. Connect early, ugly, then polish.

## Git
- One repo. Branch per person: `feat/harness`, `feat/detector`, `feat/correction`, `feat/viewer`.
- Commit + push frequently; pull before push.
- Work inside your own directory → near-zero merge conflicts. The only shared files are `contracts/schemas.py` (frozen) and `orchestrator.py` (built together).
- `contracts/schemas.py` changes get announced in chat BEFORE pushing.

## Demo path is a first-class feature (hour 9–11)
Pre-compute the replay stream, build the live change-point trigger, and **record a fallback video**. Never demo live without a backup.
