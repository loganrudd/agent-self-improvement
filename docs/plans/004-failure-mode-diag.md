# Plan 004: Failure-Mode Diagnosis + `failing_run_ids`

**Status:** Draft
**Phase:** 4 of 6 (detector — see `docs/detector-plan.md`)
**Created:** 2026-06-27
**Files touched:**
- `detector/detector.py` (modify — add a record-retention window, a `_classify_failure` helper, and populate the two `DriftEvent` fields at fire time)
- `detector/tests/test_detector.py` (modify — **replace** the placeholder `test_phase4_fields_are_defaults`, add `TestFailureMode`; reuse existing helpers)
- `detector/config.py` (**no change** — `failing_ids_cap=8` already exists from Phase 1 setup)
- `docs/detector-plan.md` (modify — correct the Phase 4 verify line; see Tradeoff 4)
- `contracts/schemas.py` (**not touched** — frozen; `failure_mode` and `failing_run_ids` already exist on `DriftEvent`)

## Goal

At the moment the detector fires, it sets `DriftEvent.failure_mode` to the
**majority failure kind** among the failing runs in its window, and
`DriftEvent.failing_run_ids` to those runs' ids (prioritized by the dominant
mode, capped at `cfg.failing_ids_cap`). "Done" = replaying
`fixtures/mock_telemetry.jsonl` yields one event with
`failure_mode == VALID_BUT_WRONG`, `failing_run_ids` non-empty, `≤ cap`, and
every id a real `execution_accuracy == 0` run inside the detector's fire window
— all 62 prior tests still green, firing **timing** unchanged.

## Context

What we learned in the Explore step:

- **Phases 1–3 are done and green** (62 tests). The detector
  ([detector/detector.py](../../detector/detector.py)) is a streaming
  `WARMUP → NORMAL → DRIFTING` machine. It fires exactly once at **idx 89** on
  the mock and currently emits the two Phase-4 fields as hardcoded defaults —
  [detector.py:131-133](../../detector/detector.py#L131): `failure_mode=NONE`,
  `failing_run_ids=[]`. Phase 4 replaces **only those two literals**; the
  breach/streak/state logic is untouched.
- **The window holds floats, not records.** `_acc_window` and `_strat_windows`
  are `RollingStats` instances ([detector/rolling.py](../../detector/rolling.py))
  that store accuracy floats for mean/std. They cannot answer "which `run_id`
  failed and how." Phase 4's one structural need is to **retain per-record
  identity** for the current window so it can classify failures at fire time.
  This is the central design decision — see Tradeoff 1.
- **Outages are already excluded before the window.** `_is_outage_record` returns
  early at the top of `update()` ([detector.py:71](../../detector/detector.py#L71)),
  so any record-retention deque we add **below** that point inherits clean data —
  no harness API-errors can pollute the failure classification.
- **`FailureMode` is frozen and ready** —
  [schemas.py:24-29](../../contracts/schemas.py#L24): `VALID_BUT_WRONG`,
  `INVALID_SQL`, `NONE`.
- **Empirically measured fire-window breakdown** (replayed the mock; window is
  `recs[65:90]`, 25 records, no outages present):

  | quantity | value |
  |---|---|
  | fire index | 89 |
  | window range | `[65 .. 89]` (straddles the change-point at 80) |
  | total failures (`acc == 0`) in window | **10** |
  | `VALID_BUT_WRONG` | 7 — `run_0072, 0080, 0083, 0084, 0085, 0088, 0089` |
  | `INVALID_SQL` | 3 — `run_0081, 0082, 0087` |
  | **majority mode** | **`VALID_BUT_WRONG`** (7 > 3) ✓ matches the mock event |
  | `failing_run_ids` (dominant-first, cap 8) | `run_0072, 0080, 0083, 0084, 0085, 0088, 0089, 0081` |

- **Critical gotcha — the window straddles the change-point.** With `W=25` and
  fire at idx 89, the window is `[65..89]` = ~15 baseline records + ~10 degraded.
  `run_0072` is a **baseline-phase** `VALID_BUT_WRONG` failure that legitimately
  sits in the window and lands in `failing_run_ids`. This means the detector's
  output is **not** the mock's hand-picked list
  (`run_0080..0088` = "first 8 zeros in the full `[80:160]` segment",
  [generate_mocks.py:86](../../fixtures/generate_mocks.py#L86)). They overlap but
  differ. The verify assertion must therefore be **structural** ("non-empty,
  ≤ cap, every id a real `acc==0` run in the window, majority = VALID_BUT_WRONG"),
  **not** exact-list equality with the mock — see Tradeoff 4.
- **A placeholder test must be inverted, not kept.**
  `test_phase4_fields_are_defaults` ([test_detector.py:112-117](../../detector/tests/test_detector.py#L112))
  currently asserts `failure_mode == NONE` and `failing_run_ids == []`. After
  Phase 4 those become populated; this test is **replaced** with the real
  assertions (Step 3).

## Approach

Three implementation steps + a doc fix, each runnable against the mock before the
next. Firing **timing** is not touched in any step — Phase 4 only fills two
fields that were already being emitted.

### Step 1 — Retain window records + the classification helper (no field change yet)

In `detector/detector.py`:

- Add a module-level pure function next to `_is_outage_record`:
  ```python
  def _classify_failure(record: TelemetryRecord) -> FailureMode:
      """Classify ONE run. Failure is strict execution_accuracy == 0
      (binary mock; matches detector-plan Decision 6).
        - acc != 0          -> NONE (not a failure)
        - acc == 0, invalid -> INVALID_SQL  (SQL didn't parse/run)
        - acc == 0, valid   -> VALID_BUT_WRONG (ran, wrong result set)
      """
      if record.execution_accuracy != 0.0:
          return FailureMode.NONE
      return FailureMode.INVALID_SQL if not record.query_valid else FailureMode.VALID_BUT_WRONG
  ```
- In `__init__`, add a parallel record window sized to the detection window:
  ```python
  self._record_window: deque[TelemetryRecord] = deque(maxlen=self._cfg.window)
  ```
  (Add `from collections import deque` import.)
- In `update()`, **immediately after** the existing
  `self._strat_windows[...].push(...)` line (i.e. **below** the outage
  early-return, alongside the other window pushes), append the record:
  ```python
  self._record_window.append(record)
  ```
- No change to warmup / breach / fire logic yet; fields still emit defaults.

**Validate before Step 2:**
```bash
python3 -c "
import json
from contracts.schemas import TelemetryRecord, FailureMode
from detector.config import DetectorConfig
from detector.detector import Detector, _classify_failure
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
d=Detector(DetectorConfig())
for r in recs: d.update(r)
print('record window len:', len(d._record_window))         # 25
print('last run in window:', d._record_window[-1].run_id)  # run_0239
print('a degraded run:', _classify_failure(recs[81]))      # the actual mode of idx 81
"
```
Expect: window length 25, helper returns a valid `FailureMode` per the record's
fields. (No `DriftEvent` field change observable yet — Phase-2/3 tests stay green.)

### Step 2 — Populate `failure_mode` + `failing_run_ids` at fire

In `_handle_normal`, replace the two default literals in the `DriftEvent(...)`
construction ([detector.py:131-133](../../detector/detector.py#L131)) with values
computed from `self._record_window`. Factor the computation into a small private
method so the fire path stays readable:

```python
def _diagnose_failures(self) -> tuple[FailureMode, list[str]]:
    """At fire time: majority failure mode + prioritized, capped run ids
    from the current window. Returns (NONE, []) if the window holds no
    strict failures (defensive — shouldn't happen once a drop fired on the
    binary mock, but possible with partial-accuracy streams)."""
    failures = [
        (r.run_id, _classify_failure(r))
        for r in self._record_window
        if r.execution_accuracy == 0.0
    ]
    if not failures:
        return FailureMode.NONE, []

    counts = Counter(mode for _, mode in failures)
    # Deterministic tie-break: VALID_BUT_WRONG wins a tie (the learnable
    # logic case; also the dominant real-world mode). Explicit, not Counter-order.
    dominant = max(
        counts,
        key=lambda m: (counts[m], m == FailureMode.VALID_BUT_WRONG),
    )
    dom_ids = [rid for rid, mode in failures if mode == dominant]
    other_ids = [rid for rid, mode in failures if mode != dominant]
    failing_ids = (dom_ids + other_ids)[: self._cfg.failing_ids_cap]
    return dominant, failing_ids
```
Then in the fire branch:
```python
failure_mode, failing_run_ids = self._diagnose_failures()
...
return DriftEvent(
    ...,
    failure_mode=failure_mode,
    failing_run_ids=failing_run_ids,
)
```
(Add `from collections import Counter`.)

**Validate before Step 3:**
```bash
python3 -c "
import json
from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.detector import Detector
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
d=Detector(DetectorConfig()); ev=None
for r in recs:
    e=d.update(r)
    if e: ev=e; break
print('failure_mode :', ev.failure_mode)        # FailureMode.VALID_BUT_WRONG
print('n ids        :', len(ev.failing_run_ids)) # 8 (== cap)
print('ids          :', ev.failing_run_ids)
zeros={r.run_id for r in recs if r.execution_accuracy==0.0}
print('all real fails:', all(i in zeros for i in ev.failing_run_ids))  # True
"
```
Expect: `failure_mode == VALID_BUT_WRONG`, 8 ids (cap), all real `acc==0` runs.

### Step 3 — Tests (`TestFailureMode` + replace the placeholder)

In `detector/tests/test_detector.py` (reuse `_make_rec`, `_load_mock`,
`_run_stream`, `_baseline_stream`; `_make_rec` already takes `execution_accuracy`
and `query_valid`, so no helper duplication). Mirror the Phase 2/3 three-tier
convention.

**Replace** `test_phase4_fields_are_defaults` with `test_phase4_fields_populated`:
the single mock event now has `failure_mode == VALID_BUT_WRONG` and a non-empty
`failing_run_ids`.

**Tier A — mock-replay invariants (durable):**
- Replay full mock → the one event: `failure_mode == VALID_BUT_WRONG`;
  `failing_run_ids` non-empty and `len ≤ cfg.failing_ids_cap`; every id is a real
  `execution_accuracy == 0` run; every id is within the fire window
  (`run_id` ∈ `{recs[65..89]}`). **No exact-list equality with the mock** (the
  window straddles the change-point; see Tradeoff 4 / docstring).

**Tier B — `_classify_failure` unit (hand-built, exact):**
- `acc=1.0, valid=True`  → `NONE`.
- `acc=0.0, valid=False` → `INVALID_SQL`.
- `acc=0.0, valid=True`  → `VALID_BUT_WRONG`.
- `acc=0.0, valid=True` (a "valid but wrong" run) is **not** misread as invalid.

**Tier B — `_diagnose_failures` majority + selection (hand-built windows):**
- All-invalid window → `INVALID_SQL`; all returned ids are invalid runs.
- All-valid-wrong window → `VALID_BUT_WRONG`.
- Mixed 3 invalid / 2 valid-wrong → `INVALID_SQL` (majority); dominant-mode ids
  come first.
- **Tie** 2/2 → `VALID_BUT_WRONG` (documented tie-break).
- Failures **> cap**: build a window with `cap+3` failures → exactly `cap` ids
  returned, all dominant-mode (prioritization honored).
- **No failures** in window (all `acc>0`) → `(NONE, [])` (defensive branch).
- Outage runs never reach the window (they're `acc==0, invalid` shaped but
  excluded upstream): scatter `_outage_rec()` into a failing stream → outage
  `run_id`s appear in **neither** `failing_run_ids` nor the counts.

**Tier C — mock-pinned (commented seed-7 specific):**
- Fire event `failing_run_ids` length `== 8`; majority `VALID_BUT_WRONG` with the
  7/3 split observed (assert via reconstructing the window, not a literal id list).

**Validate:** `python3 -m pytest detector/tests/ -v` — 62 prior (minus the 1
replaced + its replacement) + new `TestFailureMode`, all green; firing-timing
tests untouched and still passing.

### Step 4 — Correct `docs/detector-plan.md`

The Phase 4 verify line (line 74) says ids are "all real failing runs in the
**degraded window**." That is inaccurate: the detector's window straddles the
change-point and can include a baseline-phase failure (`run_0072`). Update it to
"all real `acc==0` failing runs in the **detector's fire window** `[65..89]`
(which straddles the change-point — a baseline failure may legitimately appear);
ids overlap but are **not identical** to the mock's hand-picked
`[:8]`-of-`[80:160]` selection." Mirrors how Plans 002/003 corrected their own
aspirational numbers.

## Tradeoffs Considered

1. **How to retain per-record identity for the window.** The window currently
   holds floats only; classification needs `run_id` + `query_valid` + `acc`.
   - (a) **Parallel `deque[TelemetryRecord]` (maxlen=W).** Store the whole record.
     Zero projection logic; full info available if Phase 5/correction wants more
     (difficulty, sql). Cost: 25 records held — trivial.
   - (b) **Parallel `deque[tuple[str,bool,float]]`** of just the three needed
     fields. Marginally leaner; but introduces a projection that can silently
     drift from the schema and reads less obviously than "the records in my
     window."
   - (c) **Generalize `RollingStats` to carry payloads.** Couples the numeric
     primitive to record shape — violates the plan's "ONE RollingStats for
     mean/std" DRY intent (detector-plan line 31) and complicates the baseline
     fit that reuses it.
   - **Chose (a).** With `W=25` the memory is a non-issue, and "keep the records
     in the window" is the most *explicit* statement of intent — no tuple-packing
     cleverness, no projection to keep in sync, no coupling of the stats helper.
     Maps to: explicit over clever, reuse `RollingStats` as-is for numbers, no
     premature abstraction. (b) is a micro-optimization with a maintenance cost;
     (c) over-engineers the shared primitive.

2. **`_acc_window` (floats) + `_record_window` (records): is that duplication?**
   They overlap (both hold the window's accuracies) but serve different concerns:
   `RollingStats` gives O(1) running-sum mean + the std-floor logic and is reused
   by the baseline fit; the record deque exists solely for *identity at fire
   time*. Collapsing them — computing the mean by iterating the record deque each
   record — would drop the O(1) update and re-implement stats already centralized
   in `RollingStats`. Keeping two windows with distinct jobs is the lesser evil
   than smearing stats logic across the fire path. Not a real DRY violation.

3. **Classification source = `query_valid` + `acc` only (no SQL parsing).** The
   contract defines `FailureMode` straight off the validity/accuracy channels
   ([schemas.py:24-29](../../contracts/schemas.py#L24)); parsing `generated_sql`
   to second-guess `query_valid` would duplicate the harness's job and add a
   fragile SQL-dialect dependency. Strict `acc == 0` for "failure" is locked
   (Decision 6, your call this session) — it matches the binary mock; a record
   with `0 < acc < 1` is classified `NONE` (not a failure) and simply isn't
   collected, which the defensive empty-failures branch handles.

4. **Verify target: structural assertion vs. exact mock-id equality.** The mock's
   `failing_run_ids` are "first 8 zeros in the full `[80:160]` degraded segment"
   ([generate_mocks.py:86](../../fixtures/generate_mocks.py#L86)) — a *generator*
   convenience, computed with global knowledge the streaming detector doesn't
   have. The detector can only see its window `[65..89]`, which **straddles the
   change-point** and includes the baseline failure `run_0072`. Asserting exact
   equality would force the detector to reproduce the generator's offline
   selection — wrong by construction.
   - **Chose structural assertions** ("non-empty, ≤ cap, all real `acc==0` runs in
     the window, majority VALID_BUT_WRONG, dominant-first"). They survive
     window/threshold re-tuning and assert the *behavior we actually want*. The
     7/3 split and length-8 are pinned only in Tier C, flagged seed-specific.

5. **Tie-break when `VALID_BUT_WRONG == INVALID_SQL`.** `Counter.most_common`
   ties resolve by insertion order — implicit and fragile. We make it **explicit**:
   tie → `VALID_BUT_WRONG` (the "needs logic/join examples" case, and the dominant
   real-world mode), via a `max(..., key=(count, is_valid_but_wrong))` secondary
   key. Deterministic regardless of `Counter` internals; covered by a Tier-B test.

6. **`failing_run_ids` ordering: chronological vs. sorted.** Within the dominant
   mode we preserve **window (chronological) order**, then append other-mode ids,
   then cap. Chronological is the natural reading order and avoids an arbitrary
   sort; correction treats the list as a set of cases to learn from, so order is
   not semantically load-bearing — keep it simple and stable.

## Validation

Run from repo root:
```bash
# Step smoke checks are inline above.
python3 -m pytest detector/tests/ -v   # all green (Phase 4 tests added)
```
Pass criteria:
- Mock replay → exactly one `DriftEvent` (timing unchanged: idx 89) with
  `failure_mode == VALID_BUT_WRONG`, `failing_run_ids` non-empty, `len ≤ cap`,
  every id a real `execution_accuracy == 0` run inside the fire window.
- `_classify_failure`: each of the three branches exact; `NONE` for `acc != 0`.
- `_diagnose_failures`: all-invalid → INVALID_SQL; all-valid-wrong →
  VALID_BUT_WRONG; mixed → majority; tie → VALID_BUT_WRONG; `> cap` → capped &
  dominant-prioritized; no-failures → `(NONE, [])`.
- Outage runs appear in neither the counts nor `failing_run_ids`.
- Phases 1–3 behavior unchanged — one event at idx 89, zero in baseline/recovery,
  `stratified_means()` unaffected.

## Open Questions

- **Should baseline-phase failures in the straddling window be excluded?**
  Currently `run_0072` (a baseline `VALID_BUT_WRONG`) lands in `failing_run_ids`
  because it's genuinely in the detector's window. Excluding it would require the
  detector to know the change-point location — which it deliberately never
  hardcodes (detector-plan line 20). Recommendation: **keep it** — the detector
  honestly reports the failures in its window, and a real wrong-SQL case is fine
  for the teacher to learn from. Flagged for awareness, not a blocker.
- **Cap default 8 vs. window failure count (10 on the mock).** With 10 failures
  and cap 8, two are dropped (the 3rd INVALID + nothing else, given dominant-first
  order). If correction wants the full INVALID set for diagnosis, raise
  `failing_ids_cap` — a one-line `DetectorConfig` change, no code change. Deferred
  until correction states a need.
- **Fire-time snapshot durability** (shared with Plan 003 Open Q): `failing_run_ids`
  is computed from the live `_record_window` *at fire*; once latched to DRIFTING we
  never recompute, so the emitted `DriftEvent` is the durable record. No stored
  snapshot needed unless Phase 5 wants to re-inspect post-fire.
