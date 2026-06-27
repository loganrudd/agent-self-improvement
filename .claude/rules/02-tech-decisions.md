# LOCKED RULE: Technical decisions

**Do not change without team agreement.** These were debated and settled; re-litigating mid-build wastes time.

## Detector: statistical, NO model training
Rolling-window mean + threshold/z-score (or EWMA) per channel. We have no training data and ~12 hours; a statistical detector is more reliable and more defensible than a flaky trained model. (`sklearn.IsolationForest` fit on baseline runs is allowed ONLY if the simple version already works and time remains.)

## Drift is detected over a WINDOW, never on a single query
Per-query accuracy is binary/noisy. The detector operates on a rolling window (~20–30 runs) so the aggregate is smooth. A single bad query is an anomaly, not drift, and must NOT fire the drift detector. The recovery curve plots the **windowed aggregate**.

## The feed: a CHANGE-POINT on a stratified stream (not random, not a gradual ramp)
- Phase 1 baseline: mostly easy/medium Spider questions, agent on base config → stable high accuracy.
- Change-point: input distribution **shifts** to hard/extra-hard (models real production complexity creep).
- Phase 2 degraded: hard questions, base config → accuracy settles lower. Drift = the transition.
- Phase 3 recovery: learned examples injected → same hard questions → accuracy climbs.
Scale: pool ~30–50 easy/med + ~30–50 hard unique questions, **sampled into a few-hundred-run stream** so windows are smooth. **Pre-compute the stream and replay it fast** for the demo — don't pay live latency for hundreds of calls.

## Correction: LEARN FROM FAILURES — not model-swap
Degradation is a real distribution shift (harder queries); the fix is real adaptation. On drift: collect the specific failing (question, gold-SQL) cases → a stronger **teacher model** generates corrected SQL → those become few-shot examples → injected and anchored (anti-forgetting) so easy-query skill isn't lost. The agent must end **better than it started** on the hard distribution. Do NOT "fix" drift by reverting a model — that's circular and unimpressive.

## Metrics
- `execution_accuracy` (windowed) — the drift curve. Objective: execute generated + gold SQL, compare result sets; use Spider's eval to avoid row-order false mismatches.
- `execution_accuracy` stratified by `difficulty` — the contamination-free improvement signal. Build the demo on this.
- `query_valid` — diagnostic: valid-but-wrong (logic) vs invalid (syntax) → determines what to learn.
- complexity gap (`required` − `generated`) — label-free diagnostic (agent under-reaching).
- `latency_ms`, `tokens` — operational, free.

## Stack
- **Local only, no GCP.** Laptop demo. Scaling is a *verbal* answer (stateless, per-channel, Ray/KubeRay for fan-out), not a build task.
- **Spider** for data: a subset (handful of schemas + the question pools). Spider supplies questions + gold SQL + SQLite DBs (solves the database requirement).
- **Gemini**: weaker/faster tier as the base agent (so it genuinely struggles on hard queries), stronger tier as the teacher.
