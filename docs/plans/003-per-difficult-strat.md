# Plan 003: Per-Difficulty Stratification (`Detector.stratified_means()`)

**Status:** Draft
**Phase:** 3 of 6 (detector — see `docs/detector-plan.md`)
**Created:** 2026-06-27
**Files touched:**
- `detector/detector.py` (modify — add per-difficulty windows + `stratified_means()` accessor)
- `detector/tests/test_detector.py` (modify — add `TestStratification`; reuse existing helpers, no duplication)
- `detector/config.py` (no change — Phase 3 reuses `cfg.window`, adds no knob)
- `docs/detector-plan.md` (modify — correct the Phase 3 verify line; see Tradeoff 3)
- `contracts/schemas.py` (**not touched** — frozen; stratification has no slot, by design — Decision 5)

## Goal

The `Detector` tracks one rolling accuracy window **per `Difficulty`** alongside
the overall window, and exposes `stratified_means() -> dict[Difficulty, float]`
returning the current per-bucket windowed accuracy (empty buckets omitted).
"Done" = replaying `fixtures/mock_telemetry.jsonl`, at fire time the accessor
shows `hard`/`extra` low and `easy`/`medium` high; replaying to the end shows
`hard`/`extra` climbed (the 0.2→0.8 recovery curve) — all 49 prior tests still
green, firing logic unchanged.

## Context

What we learned in the Explore step:

- **Phases 1–2 are done and green** (49 tests). The detector ([detector/detector.py](../../detector/detector.py))
  is a streaming `WARMUP → NORMAL → DRIFTING` machine. It already pushes
  `execution_accuracy` into one `RollingStats(maxlen=cfg.window)` on every **real**
  record (outages excluded at the top of `update()`). Phase 3 hooks the per-difficulty
  windows into that **same push path** — outage exclusion and warmup behaviour come
  for free.
- **DRY anchor already exists.** `RollingStats` ([detector/rolling.py](../../detector/rolling.py))
  is the one mean/std primitive. Per-difficulty stratification is *N more instances*
  of it — no new stats code (matches `detector-plan.md` line 31).
- **Difficulty is segmented by phase** (confirmed empirically on the mock):
  P1 [0:80] = easy/medium only, P2 [80:160] = hard/extra only, P3 [160:240] =
  hard/extra only. So stratification is **not** a per-bucket drift test (there is
  no hard/extra baseline to compare against) — it is "current per-difficulty window
  accuracy, tracked over time," i.e. the `hard 40%→80%` improvement curve.
- **Decision 5 is locked** (`detector-plan.md` line 67): stratification lives in
  **accessor / log output only**. The frozen `DriftEvent` has no slot and we will
  not hack the contract.
- **Empirically measured per-difficulty window means** (separate `maxlen=25` deque
  per difficulty — the design chosen, Tradeoff 1):

  | idx | easy | medium | hard | extra | note |
  |----:|-----:|-------:|-----:|------:|------|
  | <80 | ~0.96 | ~0.96 | *(absent)* | *(absent)* | baseline: hard/extra empty |
  | 89 (FIRE) | 0.96 (n=25) | 0.96 (n=25) | 0.00 (n=5) | 0.20 (n=5) | hard/extra just started filling |
  | 104 | 0.96 | 0.96 | 0.21 (n=14) | 0.18 (n=11) | windows still filling |
  | 159 | 0.96 | 0.96 | 0.20 (n=25) | 0.24 (n=25) | degraded, full |
  | 239 (END) | 0.96 | 0.96 | 0.92 (n=25) | 0.72 (n=25) | recovered |

  **Key correction:** under per-difficulty deques, `easy`/`medium` are **never empty
  after baseline** — they freeze at ~0.96 because no new easy/medium records arrive
  post-change-point. The plan's "easy/medium empty in degraded window" (line 65)
  describes a *different* design (one shared window sliced by difficulty) and is
  corrected here — see Tradeoff 3.

## Approach

Three steps, each runnable against the mock before the next. Firing logic is
**not touched** in any step — Phase 3 is purely additive observability.

### Step 1 — Per-difficulty rolling windows wired into the push path

In `detector/detector.py`:
- Import `Difficulty` from `contracts.schemas`.
- In `__init__`, add one window per difficulty:
  ```python
  self._strat_windows: dict[Difficulty, RollingStats] = {
      d: RollingStats(maxlen=self._cfg.window) for d in Difficulty
  }
  ```
- In `update()`, **immediately after** the existing `self._acc_window.push(...)`
  (so it sits *below* the outage early-return — outages already excluded, warmup
  records already included), add:
  ```python
  self._strat_windows[record.difficulty].push(record.execution_accuracy)
  ```
- No change to warmup / breach / fire logic.

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
print({k.value:(round(w.mean,3), w.n) for k,w in d._strat_windows.items()})
"
# expect: hard ~0.92 (n=25), extra ~0.72 (n=25), easy/medium ~0.96 (n=25)
```

### Step 2 — `stratified_means()` accessor (empty buckets omitted)

Add a public method to `Detector`:
```python
def stratified_means(self) -> dict[Difficulty, float]:
    """Current windowed execution-accuracy per difficulty.

    Returns only buckets with at least one record in the current window;
    an empty bucket (no records seen yet in this window) is OMITTED rather
    than reported as a misleading 0.0. Reflects the live window state — call
    it right after update() returns a DriftEvent to snapshot the fire moment.
    """
    return {d: w.mean for d, w in self._strat_windows.items() if w.n > 0}
```
(Empty-bucket = omit, per the settled decision. `RollingStats.mean` returns 0.0
when `n==0`, which is why we gate on `w.n > 0`.)

**Validate before Step 3:**
```bash
python3 -c "
import json
from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
d=Detector(DetectorConfig()); fire=None
for r in recs:
    ev=d.update(r)
    if ev and fire is None: fire={k.value:round(v,3) for k,v in d.stratified_means().items()}
end={k.value:round(v,3) for k,v in d.stratified_means().items()}
print('at fire :', fire)   # all 4 keys; hard~0.0 extra~0.2, easy/medium~0.96
print('at end  :', end)    # hard~0.92 extra~0.72 (climbed), easy/medium~0.96
"
```
Also confirm omission: a detector fed only baseline records returns a dict with
**no** `hard`/`extra` keys.

### Step 3 — Tests (`TestStratification` in `test_detector.py`)

Add to the existing file (reuses `_make_rec`, `_load_mock`, `_run_stream`,
`_baseline_stream` — `_make_rec` already accepts a `difficulty` param, so no
helper duplication). Three tiers, matching the Phase 2 convention:

**Tier A — mock-replay invariants (durable; survive threshold re-tuning):**
- Replay full mock; snapshot `stratified_means()` at the fire index and at end.
  - At fire: dict contains all four difficulties; `hard` and `extra` each strictly
    below `easy` and `medium` (the degraded signal is visible per-bucket).
  - At end: `hard` and `extra` each **strictly greater** than their fire-time value
    (the recovery curve climbed); `easy`/`medium` unchanged within tolerance.
- Baseline-only replay (`recs[:80]`): returned dict has no `hard`/`extra` keys
  (empty buckets omitted).

**Tier B — behavioral unit tests (hand-built, exact arithmetic):**
- Mixed-difficulty window with known values → each bucket mean is exactly correct
  (e.g. two `hard` accs `[0.0, 1.0]` → `hard == 0.5`; independent of other buckets).
- Empty bucket is omitted, not `0.0`: feed only `EASY` records → dict has `easy`
  only, and `EASY in d.stratified_means()` / `HARD not in d.stratified_means()`.
- Outage records do not enter stratified windows: scatter `_outage_rec()` (which
  carry `difficulty=EASY`) into an easy stream → `easy` mean stays 1.0 (outages
  excluded from the per-difficulty window, same as the overall window).
- Window eviction is per-bucket: push `> cfg.window` records of one difficulty →
  that bucket's `n == cfg.window` (old values evicted), confirming the bucket
  honours `maxlen`.

**Tier C — mock-pinned (commented as seed-7 specific, may re-tune):**
- At fire: `hard` in `[0.0, 0.2]`, `extra` in `[0.1, 0.4]`, `easy`/`medium` in
  `[0.9, 1.0]`.
- At end: `hard` in `[0.8, 1.0]`, `extra` in `[0.6, 0.85]`.

**Validate:** `python3 -m pytest detector/tests/ -v` — all green (49 prior + new),
firing-behaviour tests untouched and still passing.

### Step 4 — Correct `docs/detector-plan.md`

Update the Phase 3 verify line (line 65) from "easy/medium empty in degraded
window" to the truthful "easy/medium **frozen at the baseline ~0.96** (no
post-change-point easy/medium records); hard/extra populate from the change-point
and climb ≈0.2→0.8 through recovery." Mirrors how Plan 002 corrected its own
aspirational numbers.

## Tradeoffs Considered

1. **Per-difficulty deques vs. one shared window sliced by difficulty.**
   - (a) **Separate `RollingStats(maxlen=W)` per difficulty** — each bucket holds
     its own last `W` records. Trivial reuse of the Phase-1 primitive; routing is
     one line in the push path. Consequence: easy/medium **freeze** at their last
     `W` baseline values forever (no new easy/medium records arrive), so they never
     empty post-baseline.
   - (b) **One window of the last `W` records, split by difficulty on read.** Stores
     `(difficulty, accuracy)` tuples; `stratified_means()` recomputes the split each
     call. Consequence: easy/medium **do** go empty once the window slides fully past
     the change-point (idx≥104), matching the plan's original line 65.
   - **Chose (a).** It is the literal reading of the plan's "rolling windows in
     parallel with the overall one" (line 63), reuses `RollingStats` with zero new
     math (DRY), and the headline `hard`/`extra` curve is **identical under both**
     (in degraded/recovery the window is pure hard/extra anyway). (b)'s only
     difference is emptying easy/medium — which buys nothing here and adds a
     per-call recompute + a parallel storage shape. Maps to: explicit over clever,
     reuse the one primitive, no premature machinery.

2. **`stratified_means()` as a live accessor vs. a detector-stored fire snapshot.**
   - (a) **Live accessor only** — returns the current window state; the caller grabs
     the fire-time value by calling it right after `update()` returns an event
     (`if ev: snap = det.stratified_means()`). Zero extra state.
   - (b) **Detector stores `self._fire_stratification`** at fire, exposed as a
     property — foolproof "value at fire time" even if streaming continues.
   - **Chose (a)** for Phase 3. The live accessor *is* the deliverable the spec asks
     for, and at the fire instant the window state already **is** the degraded
     window, so a one-line snapshot at the call site is enough. (b) is a Phase 5
     convenience (orchestrator/CLI) — flagged in Open Questions, cheap to promote
     later. Maps to: avoid premature abstraction; YAGNI until the orchestrator needs it.

3. **Empty bucket: omit vs. report `0.0` vs. report `None`.** Settled: **omit**.
   Reporting `0.0` for a bucket with no samples is a lie (0% accuracy vs. "no data");
   `None` values force every call site to None-check. Omitting makes presence in the
   dict mean "this bucket has data in the current window," and `in` checks read
   naturally. (Empty buckets occur for hard/extra during the baseline phase.)

4. **Where the tests live: extend `test_detector.py` vs. new `test_stratification.py`.**
   - **Chose extend.** The stratification tests need `_make_rec`, `_load_mock`,
     `_run_stream` — already in `test_detector.py`. A new file would duplicate those
     helpers (DRY violation) or require extracting them to `conftest.py` first (scope
     creep for ~8 tests). One new `TestStratification` class keeps it cohesive. If
     the file later grows unwieldy, extract shared helpers to `conftest.py` then.

5. **Expose means only vs. means + counts (`n`).** Phase 3 ships `dict[Difficulty,
   float]` (means only) — that is what the recovery curve needs and what the spec
   names. Per-bucket `n` (useful for a CLI line like `extra (n=25): 0.72`) is a
   Phase 5 presentation concern; adding it now is speculative. YAGNI.

## Validation

Run from repo root:
```bash
# Step smoke checks are inline above.
python3 -m pytest detector/tests/ -v   # 49 prior + new, all green
```
Pass criteria:
- `stratified_means()` at fire time: all four difficulties present; `hard`/`extra`
  strictly below `easy`/`medium`.
- `stratified_means()` at end of mock: `hard`/`extra` strictly above their
  fire-time values (recovery curve climbed); `easy`/`medium` ≈ unchanged.
- Baseline-only stream: `hard`/`extra` omitted from the returned dict.
- Per-bucket arithmetic exact; outages excluded from per-difficulty windows;
  per-bucket `maxlen` eviction holds.
- Phase 1 + Phase 2 behaviour unchanged — exactly one `DriftEvent` at idx≈89,
  zero in baseline/recovery.

## Open Questions

- **Fire-time snapshot ownership (Tradeoff 2).** If the Phase 5 orchestrator/CLI
  wants a durable "stratification at fire" without snapshotting at the call site,
  promote to a stored `self._fire_stratification` + read-only property. Cheap add;
  deferred until there's a consumer.
- **Stratification has no `DriftEvent` slot (Decision 5).** Confirmed: correction
  recovers per-bucket context via `failing_run_ids` (Phase 4), and the viewer
  stratifies telemetry itself. No contract change. Flagging so Phase 5 wiring
  doesn't try to stuff stratified data into the event.
- **Anti-forgetting is not provable from this mock.** Because the segmented stream
  has no easy/medium records in recovery, the frozen easy/medium values are
  baseline-only — they show skill *wasn't measured again*, not that it *was
  retained*. The honest demo claim is the hard/extra recovery curve. If we later
  want the anti-forgetting story, the harness feed must interleave a few easy/medium
  probes into recovery — a harness change, out of detector scope.
