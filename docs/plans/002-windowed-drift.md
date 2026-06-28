# Plan 002: Windowed Drift Check (`detector.py` core)

**Status:** Draft
**Phase:** 2 of 6 (detector — see `docs/detector-plan.md`)
**Created:** 2026-06-27
**Files touched:**
- `detector/detector.py` (modify — currently a TODO stub; becomes the stateful `Detector`)
- `detector/tests/test_detector.py` (new)
- `detector/config.py` (no change — all Phase 2 fields already declared in Plan 001)
- `docs/detector-plan.md` (modify — correct the Phase 2 verify numbers; see Tradeoff 1)

## Goal

A stateful `Detector` that consumes a `TelemetryRecord` stream one record at a
time and fires **exactly one** `DriftEvent` on `execution_accuracy` when the
windowed mean sustains a drop past threshold after the change-point — and stays
silent in the baseline and recovery phases. "Done" = replaying
`fixtures/mock_telemetry.jsonl` through `Detector.update()` yields one event in
the degraded window (`80 ≤ idx < 160`), zero in baseline, zero in recovery, and
the regression-guard test proves it.

## Context

Explored across the prior sessions; the new facts that shape this phase:

- **Phase 1 is done and green** (`config.py`, `rolling.py`, `baseline.py`, 24
  tests pass). `fit_baseline(records, cfg)` returns a frozen `Baseline` with
  per-channel `(mean, floored-std, n)`. Phase 2 **reuses** `fit_baseline` and
  `RollingStats(maxlen=window)` — no new stats code.
- **Measured firing trajectory on the mock** (`window=25`, `drop_threshold=0.20`,
  `min_sustained=5`, baseline_mean = **0.95** → breach threshold **0.75**):

  | idx | window_mean(last 25) | breach? | note |
  |----:|---------------------:|:-------:|------|
  | 80  | 0.92 | no  | change-point (first degraded record) |
  | 85  | 0.72 | **yes** | first breach |
  | 89  | 0.60 | yes | **FIRE** (5th sustained breach) → `severity=0.35` |
  | 104 | 0.20 | yes | window fully degraded (settled level) |

  The window is still half-baseline at the fire instant, so the honest reported
  `window_mean ≈ 0.60`, **not** the plan's aspirational `0.2`. This is decided —
  see Tradeoff 1.
- **`baseline_len (40) > window (25)`**, so by the time the baseline freezes the
  rolling window is already full — there is no partial-window evaluation to guard
  against in the normal config (we still guard defensively; see Step 2).
- **Harness is real now (branch `feat/harness`, commit `e7f26a9`).** Same feed
  shape as the mock (80/80/80, sharp change-point, segmented difficulty), so the
  mock is structurally faithful. Two harness eval edge cases confirmed by Rohan:
  - **Gold-SQL failures** score `1.0` (`query_valid=True`, indistinguishable from a
    real success per-record). **Out of scope for the detector** — a false-negative
    *data-quality* risk handled as a verify-step check (read the runner's
    `gold-SQL failures` count), documented as a blind spot. No detector logic.
  - **API-error records** are per-record distinguishable: `query_valid=False` **and**
    `generated_sql.startswith("-- error:")`. A transient outage must not masquerade
    as drift → the detector treats these as **missing data** and excludes them from
    the window (see Step 3, Tradeoff 2).
- **Contracts frozen.** `DriftEvent` has slots for `failure_mode` and
  `failing_run_ids` but those are **Phase 4's** job — Phase 2 emits `NONE` / `[]`.
  Stratification is **Phase 3**. Event-log/CLI wiring is **Phase 5**. Phase 2's
  `update()` *returns* the event; it does not write `events.jsonl`.

## Approach

Built in dependency order; each step is independently runnable against the mock
before the next.

### Step 1 — `Detector` skeleton: warmup → baseline freeze → rolling window
In `detector/detector.py`, replace the stub with a stateful class:

```
class DetectorState(Enum): WARMUP, NORMAL, DRIFTING
OUTAGE_SQL_PREFIX = "-- error:"   # soft coupling to harness sentinel (Open Q)

class Detector:
    def __init__(self, cfg: DetectorConfig): ...
    def update(self, record: TelemetryRecord) -> DriftEvent | None: ...
```

- One channel this phase: `execution_accuracy` (the headline). `query_valid` /
  `complexity_gap` are baselined in Phase 1 but Phase 2 does **not** fire on them.
- Maintain `RollingStats(maxlen=cfg.window)` for accuracy.
- **Warmup:** buffer the first `cfg.baseline_len` *real* records; when the buffer
  reaches `baseline_len`, call `fit_baseline(buffer, cfg)`, store the frozen
  `Baseline`, transition `WARMUP → NORMAL`. (Reuses Phase 1 — no new stats.)
- **Push to the window on every real record**, including during warmup, so the
  window is already warm (full) at the moment baseline freezes.
- This step does **not** fire yet — `update()` returns `None` always.

**Validate before Step 2:**
```bash
python3 -c "
import json
from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
d=Detector(DetectorConfig())
for r in recs: d.update(r)
print('baseline frozen:', d._baseline.execution_accuracy)  # mean~0.95
print('state:', d._state)                                   # NORMAL
"
```
Expect baseline mean ≈ 0.95 and no crash over all 240 records.

### Step 2 — Breach detection + `min_sustained` debounce + fire-once latch
- In `NORMAL`, on each real record after baseline freeze:
  - Guard: only evaluate when the window is full (`acc_window.n == cfg.window`) —
    defensive against a `baseline_len < window` misconfig.
  - `breached = acc_window.mean <= baseline.execution_accuracy.mean - cfg.drop_threshold`
  - Maintain a consecutive-breach counter: `+1` if breached, reset to `0` if not.
  - When the counter reaches `cfg.min_sustained`: build the `DriftEvent`, transition
    `NORMAL → DRIFTING` (**latched** — never fires again this phase), return the event.
- **One-sided:** the rule is a `<=` drop test only. A climb (recovery) can never
  satisfy it, and once `DRIFTING` is latched no further evaluation happens —
  recovery is doubly safe.
- `DriftEvent` fields:
  - `detected_at = record.timestamp` (the firing record's timestamp)
  - `channel = "execution_accuracy"`
  - `window_mean = acc_window.mean` (the honest instantaneous snapshot ≈ 0.60)
  - `baseline_mean = baseline.execution_accuracy.mean` (≈ 0.95)
  - `severity = max(0.0, baseline_mean - window_mean)` (≈ 0.35)
  - `failure_mode = FailureMode.NONE`, `failing_run_ids = []` (Phase 4 fills these)

**Validate before Step 3:**
```bash
python3 -c "
import json
from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
d=Detector(DetectorConfig()); fired=[]
for i,r in enumerate(recs):
    ev=d.update(r)
    if ev: fired.append((i,ev))
print('num events:', len(fired))                 # 1
i,ev=fired[0]
print('fire idx:', i, 'window_mean:', round(ev.window_mean,3), 'severity:', round(ev.severity,3))
"  # expect: 1 event, idx ~89, window_mean ~0.60, severity ~0.35
```

### Step 3 — Outage exclusion (API-error records)
- Add `_is_outage_record(r) -> bool`: `not r.query_valid and r.generated_sql.startswith(OUTAGE_SQL_PREFIX)`.
- At the very top of `update()`: if `_is_outage_record(record)`, **return `None`
  immediately** — do not buffer it toward warmup, do not push it to the window, do
  not advance the breach counter. The window becomes "last `W` *real* records."
- Consequence (correct behavior): a sustained outage stops the window advancing →
  it **cannot** fire from missing data, and detection resumes when real records
  return. This gives outage-suppression for free without a separate state.

**Validate before Step 4:** synthetic check — splice 10 `-- error:` records into the
baseline phase and confirm (a) no event fires there, (b) the post-outage baseline
mean is unchanged from Step 1.

### Step 4 — Tests (`detector/tests/test_detector.py`), two tiers

**Tier A — the regression guard (the headline artifact).** Replays the real mock
and asserts the core behavior in *invariant* form (survives threshold re-tuning):
```python
def test_mock_replay_fires_in_degraded_not_baseline():
    recs = _load_mock()                  # 240 real TelemetryRecords
    det = Detector(DetectorConfig())
    fire_idx = [i for i, r in enumerate(recs) if det.update(r) is not None]
    assert len(fire_idx) == 1                    # exactly one episode
    assert 80 <= fire_idx[0] < 160               # in the degraded window, not baseline/recovery
```
plus a companion that re-runs and inspects the single event:
```python
def test_mock_event_is_a_real_accuracy_drop():
    ev = _fire_once(_load_mock())
    cfg = DetectorConfig()
    assert ev.channel == "execution_accuracy"
    assert ev.window_mean < ev.baseline_mean - cfg.drop_threshold   # genuine breach
    assert ev.severity > 0
    assert ev.failure_mode == FailureMode.NONE and ev.failing_run_ids == []  # Phase 4 not yet
```

**Tier B — behavioral unit tests** on tiny hand-built streams (`_make_records`
helper, same idiom as `test_baseline.py`):
- single bad record in an otherwise-baseline stream → **no fire** (anomaly ≠ drift)
- sustained drop → fires **once**; continuing to feed degraded records → no second event
- a drop that lasts `< min_sustained` then recovers → **no fire** (debounce works)
- recovery climb after a fire → no re-fire (latch holds)
- all-`-- error:` window → **no fire** (outage excluded); scattered errors → excluded
  from the mean (mean computed over real records only)
- `baseline_len < window` misconfig → no crash, no fire before the window is full

**Tier C — mock-pinned (clearly commented as seed-specific, may need re-tune):**
- fire index in `[85, 95]`; `window_mean` in `[0.5, 0.7]`; `severity` in `[0.25, 0.45]`

**Validate:** `python -m pytest detector/tests/ -v` — all green (Phase 1's 24 + new).

## Tradeoffs Considered

1. **What `window_mean`/`severity` to report at fire — instantaneous vs settled.**
   With `window=25` the window is ~half-baseline when the sustained breach
   completes (idx 89 → mean 0.60), and doesn't reach the settled degraded level
   (0.20) until idx 104. Options: (a) report the **instantaneous** snapshot at fire
   (0.60 / sev 0.35); (b) latch fast but **delay emission** until the window
   saturates (0.20 / sev 0.75) — adds a pending sub-state and ~24 records of
   detection latency; (c) report a **degraded-run-only** mean — redefines the field
   and is noisy on small samples (0.10 over 10 records). **Chose (a)** — one
   meaning for the field ("the windowed aggregate at the instant drift was
   confirmed"), no hidden settling state, no dual accumulators. The dramatic
   0.2→0.8 story is the **viewer's** continuing curve, not this trigger snapshot.
   Cost: the plan's original `window_mean≈0.2, severity≈0.7` line is internally
   inconsistent (can't fire near idx 80 *and* read 0.2 with W=25) → this plan
   **corrects `docs/detector-plan.md`** to the truthful `≈0.60 / ≈0.35`. Maps to:
   explicit over clever, no premature machinery, edge-case honesty.

2. **API-error / outage handling — exclude-from-window vs explicit outage state.**
   Options: (a) **exclude** `-- error:` records from the window (one predicate at
   the top of `update()`); (b) an explicit `OUTAGE` state that detects "window is
   >X% errors" and suppresses firing; (c) do nothing (errors count as accuracy 0 →
   a live-precompute outage fires fake drift). **Chose (a)** — it achieves the
   outage-suppression that (b) builds a whole state machine for, as a side effect
   of the correct semantic (don't average in a non-measurement). It's safe because
   it's cleanly separable from *genuine* invalid SQL (which is `query_valid=False`
   but carries the model's real broken SQL, **not** the `-- error:` sentinel), and
   it makes Phase 4 correct for free (an outage won't be miscounted as
   `INVALID_SQL`). Maps to: thorough on failure modes, avoid premature machinery.

3. **Gold-SQL-failure inflation — detector logic vs verify-step check.** A gold-fail
   `1.0` is per-record identical to a real success on a frozen contract, so the
   detector cannot see it; it's a *false-negative* data-quality risk (inflated
   degraded accuracy could hold the window above breach). **Chose: no detector
   logic** — instead a Phase 5 verify gate reads Rohan's runner `gold-SQL failures`
   count; a high fraction means clean the demo subset, not change detection.
   Building firing logic on `complexity_gap` as a corroborator is a Phase 3
   diagnostic — premature here (YAGNI).

4. **Fire-once: latch vs cooldown/re-arm.** The demo stream has a single drift
   episode. A re-arming cooldown (fire again after the signal recovers and re-drops)
   is real-system polish with no Phase 2 payoff. **Chose: hard latch** (`DRIFTING`
   is terminal for the run). Flagged for later if a multi-episode feed appears.

5. **Test assertions: invariants vs pinned-to-seed-7.** Pinning to `window_mean==0.60`
   would make the suite brittle the moment `drop_threshold` or `window` is re-tuned
   against real harness numbers. **Chose two tiers** — behavioral invariants
   (counts, ranges, "in degraded window") as the durable guard, plus a small set of
   mock-pinned assertions explicitly commented as seed-specific. The Tier-A guard
   is the artifact a judge/interviewer can read as "proof the detector distinguishes
   drift from baseline noise."

6. **Fit baseline inside the `Detector` vs inject it.** `update()` is a streaming
   API, so the detector buffers the first `baseline_len` real records and calls the
   existing `fit_baseline` (DRY — no second baseline path). An injected pre-fit
   `Baseline` is a Phase 5 convenience (CLI may want it); not needed now.

## Validation

Run from repo root:
```bash
# Step-by-step smoke checks are inline in each step above.
# Full suite (Phase 1 + Phase 2):
python -m pytest detector/tests/ -v
```
Pass criteria:
- Replaying the full mock yields **exactly one** `DriftEvent`, fired at idx in
  `[85, 95]`, `channel == "execution_accuracy"`, `window_mean` in `[0.5, 0.7]`,
  `severity` in `[0.25, 0.45]`, `failure_mode == NONE`, `failing_run_ids == []`.
- **Zero** events in baseline (idx < 80) and **zero** in recovery (idx ≥ 160).
- Single-spike, short-breach, recovery-climb, and all-outage streams → no fire.
- A sustained drop fires once and only once.
- All Phase 1 tests still green.

## Open Questions

- **Soft coupling to the `-- error:` sentinel.** Excluding outages depends on the
  harness's `generated_sql.startswith("-- error:")` convention, which is **not** in
  the frozen `contracts/schemas.py`. Centralized in `OUTAGE_SQL_PREFIX` +
  `_is_outage_record()` so there's one place to change. **Action:** get a one-line
  agreement from Rohan that the sentinel is stable (ideally promote it to a shared
  constant). If he changes it, the guard silently no-ops — worth a belt-and-braces
  test that asserts the current sentinel is what the harness emits.
- **`detected_at` source.** Using `record.timestamp` (the firing record's time).
  In replay this is the precomputed timestamp; in the live orchestrator it's wall
  time of that run. Both are fine for ordering in `events.jsonl`; flagging so Phase 5
  doesn't second-guess it.
- **Does the rolling window need to seed from the baseline buffer explicitly?** No —
  pushing every real record into the `maxlen=window` deque as it arrives means the
  window already holds the last 25 baseline records when the baseline freezes
  (because `baseline_len=40 > window=25`). If a future config sets
  `baseline_len < window`, the Step-2 "window full" guard prevents a premature fire.
