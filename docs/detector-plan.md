# Detector — Phased Build Plan (owner: Logan)

> Drift-detection engine. Consumes `TelemetryRecord`, emits `DriftEvent`.
> This is a plan; implement one phase at a time, on approval, against
> `fixtures/mock_telemetry.jsonl`. See `.claude/rules/` for the locked constraints.

## Mock data facts this plan is grounded in
240 records, 3 phases of 80 (baseline → degraded → recovery):

| phase | acc | valid | complexity_gap | difficulty mix |
|---|---|---|---|---|
| P1 baseline (0:80) | 0.95 | 1.00 | −0.47 | easy/medium only |
| P2 degraded (80:160) | 0.20 | 0.69 | +2.44 | hard/extra only |
| P3 recovery (160:240) | 0.82 | 0.95 | −0.42 | hard/extra only |

- Degraded-window failures: **39 valid-but-wrong, 25 invalid-SQL, 16 correct** → dominant mode `VALID_BUT_WRONG`.
- `latency`/`tokens` are flat across phases (noise, not drift channels).
- Difficulty is **segmented by phase** (no interleaving) — stratification is "this tier's accuracy over time," not a per-bucket baseline test.
- `config_id` is `"c0"` throughout — detector is **config-agnostic**.
- Change-points are sharp; the detector never hardcodes their location, so this plan holds for either a 3-segment or a 4-tier descent arc.

## Shared building blocks & file layout (Phase 1 sets these up)
```
detector/
  config.py     # DetectorConfig dataclass (window, baseline_len, thresholds, cap) — plain dataclass, NOT a contract
  rolling.py    # one RollingStats helper (deque + running sum) — reused by overall, per-difficulty, and baseline
  baseline.py   # fit_baseline(records) -> Baseline
  detector.py   # Detector (stateful: update(record)->DriftEvent|None), classifier, stratifier, CLI main()
  tests/        # pytest, in my own dir (near-zero merge conflict per rules/04)
```
**DRY:** overall window, per-difficulty windows, and baseline fit all compute mean/std over a sequence → factor into ONE `RollingStats`. No generic channel-plugin framework (premature for 3–4 channels).

---

## Phase 1 — Baseline estimator (`baseline.py` + `rolling.py` + `config.py`)
**What:** From the first *N* warmup records compute per-channel `(mean, std, n)` for `execution_accuracy`, `query_valid` (0/1 rate), `complexity_gap`. Track `latency`/`tokens` but don't fire on them.

**Verify:** `fit_baseline(records[:40])` → accuracy ≈ 0.95, validity ≈ 1.0, gap ≈ −0.5. Unit tests on tiny lists.

**Edge case forcing a decision:** baseline `query_valid` is 1.00 for all 40 → sample std = 0 (same risk for accuracy). Floor std at an epsilon (optionally the Bernoulli `sqrt(p(1−p))`). This is also why Phase 2 leans absolute-drop over raw z-score.

**Decision 1 — baseline length:** **40**, frozen after fit, decoupled from the detection window. CLI param.

---

## Phase 2 — Windowed drift check on `execution_accuracy` → fires `DriftEvent` (`detector.py` core)
**What:** Stream records into a rolling window of last *W*. After the frozen baseline, fire **once** when the windowed mean drops past threshold. One-sided (drop only — recovery climbing must never fire). `NORMAL → DRIFTING` state machine + cooldown = fire-once-per-episode.

**Verify:** Full mock → **exactly one** event, fired at idx≈89 (first breach at idx=85, sustained for 5 records); `channel=="execution_accuracy"`, `window_mean≈0.60`, `baseline_mean≈0.95`, `severity≈0.35`. No event in baseline, none in recovery. Note: `window_mean` is the honest instantaneous snapshot at fire time — with W=25 the window is still half-baseline at idx=89 and doesn't reach the settled degraded level (~0.20) until idx=104 when the last baseline record slides out. See Plan 002 Tradeoff 1 for the full rationale. Unit tests: single bad query in baseline → no fire; sustained drop → fire once; recovery → no fire.

**Decision 2 — threshold method:**
- (a) **Absolute drop** — fire when `window_mean ≤ baseline_mean − drop_threshold` (~0.20–0.25). Low effort, robust to std=0, demo-legible. **← recommended (firing rule); compute a floored z-score for reporting only.**
- (b) Z-score — more principled, fiddlier, needs the std floor.
- (c) EWMA — smoother, extra `alpha` param, later refinement.

**Decision 3 — `min_sustained` debounce:** fire only after the breach holds ~5 consecutive records (guards the boundary wobble). Default ~5.

**Decision 4 — `severity`:** `max(0, baseline_mean − window_mean)` (raw drop; other fields derive the rest).

---

## Phase 3 — Per-difficulty stratification
**What:** Per-difficulty rolling windows in parallel with the overall one. Since difficulty is segmented, the baseline (easy/medium) has no hard/extra to compare against → stratification is **current-window accuracy split by difficulty, tracked over time** (the `hard 40%→80%` improvement curve). Detector surfaces the degraded value at fire time; viewer continues it through recovery.

**Verify:** At fire time hard ≈ 0.0 / extra ≈ 0.20 (still filling at fire — only ~5 records each in their per-difficulty window); easy/medium ≈ 0.96 (frozen at baseline — no easy/medium records arrive post-change-point, so their per-difficulty deques stay at the last W baseline values). Replay to end → hard ≈ 0.92, extra ≈ 0.72 (recovery curve climbed). Unit test: mixed-difficulty window → each bucket mean correct; empty bucket omitted (not 0.0).

**Decision 5 — where stratification lives:** **CLI/log output only** (frozen `DriftEvent` has no slot; won't hack the contract). Correction recovers difficulty via `failing_run_ids`; viewer stratifies telemetry itself.

---

## Phase 4 — Failure-mode diagnosis + `failing_run_ids`
**What:** At fire time classify each failing run (`query_valid==False` → `INVALID_SQL`; valid but `execution_accuracy==0` → `VALID_BUT_WRONG`), set `failure_mode` to the **majority** among failures, collect `failing_run_ids`.

**Verify:** Mock → `failure_mode==VALID_BUT_WRONG` (7 VALID_BUT_WRONG vs 3 INVALID_SQL in the detector's fire window `[65..89]`), ids non-empty, ≤ cap=8, every id a real `acc==0` run **inside the detector's fire window** (not the full `[80:160]` degraded segment). Note: the window straddles the change-point, so a baseline-phase failure (`run_0072`) legitimately appears in `failing_run_ids` — the streaming detector cannot know the change-point location (intentionally). Verify assertions are structural, not exact-list equality with the mock's offline-selected ids. Unit tests: all-invalid → `INVALID_SQL`; all-valid-wrong → `VALID_BUT_WRONG`; mixed → majority; tie → `VALID_BUT_WRONG`.

**Decision 6 — classification rule:** **count-based majority of actual failures** (data-driven, handles mixed windows, agrees with the mock) over the coarse "validity-low → invalid" heuristic (which would mis-call this 0.69-validity window).

**Decision 7 — `failing_run_ids` selection + cap:** collect window failures, **prioritize those matching the dominant mode**, cap at configurable **N (default 8–10)** to bound the teacher's workload.

---

## Phase 5 — Event-log wiring + standalone CLI ✓ DONE
**What:** `python -m detector.detector --input fixtures/mock_telemetry.jsonl`. Reads telemetry, runs detector, prints a readable summary (baseline → progress → fire moment + stratified breakdown), appends `DriftEvent` to `events.jsonl` via `contracts.eventlog.append_event`. Flags: `--window --baseline --drop-threshold --cap --output --format`.

**Gotcha:** mock is **raw `TelemetryRecord` JSONL**, but `eventlog.read_events` expects the typed envelope. Solved via `load_telemetry(path, fmt="auto")` — sniffs the first non-blank line for a `"type"` key (envelope) vs. raw; `--format {auto,raw,events}` is the explicit override. Both paths yield identical 240-record streams and the same single event.

**Edge cases + tests:** missing/empty input (exit 2 + stderr), fewer records than baseline_len (exit 2 + "need ≥ N" message), malformed lines (skip + warn to stderr), no-drift input (exit 0 + "no drift" line, nothing appended). Re-running stacks events (documented in `--help`).

**Verify:** `python3 -m pytest detector/tests/ -q` → **108 passed** (83 Phases 1–4 + 25 new CLI/loader/e2e tests). `read_events(only="drift")` returns exactly one event with `channel=="execution_accuracy"`, `failure_mode==VALID_BUT_WRONG`, `failing_run_ids` non-empty and ≤ cap=8.

---

## Phase 6 — *(optional, gated on time)* IsolationForest cross-check
Per `rules/02`, only if the simple version works and time remains. Fit `sklearn.IsolationForest` on the baseline window as a second opinion on the headline channel. Adds numpy/sklearn. **Recommendation: defer / likely skip.**

---

## Tests (cross-cutting — non-negotiable)
Unit tests ship with each phase; Phase 5 adds end-to-end. Failure-path coverage: std=0 baseline, single-query spike (no fire), recovery (no re-fire), short/empty/malformed input, mixed-difficulty stratification, each failure-mode branch. Err toward more tests.

---

## Decision summary (recommended column)
| # | Decision | Recommendation |
|---|---|---|
| 1 | Baseline length | 40, frozen, separate from window |
| 2 | Threshold method | Absolute drop (z-score as flag) |
| 3 | `min_sustained` debounce | ~5 consecutive breaching records |
| 4 | `severity` formula | `baseline_mean − window_mean` |
| 5 | Stratification location | CLI/log only (contract has no slot) |
| 6 | Failure-mode rule | Count-based majority of failures |
| 7 | `failing_run_ids` cap | 8–10, prioritized by dominant mode |
| — | Window size *W* | 25 |
| — | numpy vs stdlib | stdlib core |
