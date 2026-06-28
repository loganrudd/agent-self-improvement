# Plan 001: Baseline Estimator

**Status:** Draft
**Phase:** 1 of 6 (detector — see `docs/detector-plan.md`)
**Created:** 2026-06-27
**Files touched:**
- `detector/config.py` (new)
- `detector/rolling.py` (new)
- `detector/baseline.py` (modify — currently a TODO stub)
- `detector/tests/__init__.py` (new)
- `detector/tests/test_rolling.py` (new)
- `detector/tests/test_baseline.py` (new)

## Goal

From the first `baseline_len` warmup `TelemetryRecord`s, compute a frozen
per-channel `Baseline` of `(mean, std, n)` for `execution_accuracy`,
`query_valid` (0/1 rate), and `complexity_gap` — with `std` floored so a
zero-variance channel can't break Phase 2's downstream math — and ship the
two shared building blocks (`RollingStats`, `DetectorConfig`) that Phases 2–5
reuse. "Done" = `fit_baseline(records[:40])` returns the verified numbers and
all unit tests pass.

## Context

Explored in the prior session (see `docs/detector-plan.md` for the full phased plan):

- **Contracts are frozen** (`contracts/schemas.py`). We consume `TelemetryRecord`
  (has `execution_accuracy: float`, `query_valid: bool`, `generated_complexity`,
  `required_complexity`, and a `complexity_gap` computed property). We must not
  edit schemas.
- **Mock is verified.** `fixtures/mock_telemetry.jsonl` = 240 raw `TelemetryRecord`
  JSONL rows, 3 phases of 80 (baseline → degraded → recovery). First 40 are
  easy/medium only (20/20).
- **The std=0 edge case is real, not hypothetical.** Measured on `records[:40]`:
  - `execution_accuracy`: mean **0.95**, sample std **0.221**
  - `query_valid`: mean **1.0**, sample std **0.0**  ← zero variance
  - `complexity_gap`: mean **−0.425**, sample std **0.501**
  Any z-score on `query_valid` would divide by zero. This is the central design
  forcing-function of Phase 1, and the reason Phase 2 fires on absolute drop
  (z-score becomes report-only).
- **DRY anchor (per detector-plan §22):** overall window, per-difficulty windows,
  and the baseline fit all reduce a sequence to mean/std. That logic lives in
  exactly ONE place: `RollingStats`. No channel-plugin framework (premature for
  3 channels).
- `latency_ms`/`tokens` are flat noise across phases — Phase 1 does **not** track
  them as baseline channels (they never fire; adding them is dead weight).

## Approach

Built in dependency order; each step is independently testable before the next.

### Step 1 — `detector/config.py` (`DetectorConfig` dataclass)
Plain `@dataclass` (NOT a Pydantic contract — it's internal config, not a seam).
Fields with the plan's defaults:
- `baseline_len: int = 40`
- `window: int = 25`
- `drop_threshold: float = 0.20` (Phase 2)
- `min_sustained: int = 5` (Phase 2)
- `failing_ids_cap: int = 8` (Phase 4)
- `std_floor: float = 1e-6`

Only `baseline_len`, `window`, `std_floor` are exercised in Phase 1; the rest
are declared now so later phases don't reshape the dataclass. No logic, no I/O.

### Step 2 — `detector/rolling.py` (`RollingStats`)
The single shared mean/std helper. Deque (bounded by an optional `maxlen`) +
running sum / running sum-of-squares for O(1) `push`. Exposes:
- `push(x: float)` / extend
- `mean -> float`
- `std -> float` — **sample std (n−1)**, returns `0.0` for n<2
- `n -> int`
- a `floored_std(floor)` accessor (or the floor applied at read time by callers)

Decision: **sample std (n−1)**, matching the verified numbers above and standard
stats convention. With n≥40 the n vs n−1 difference is negligible, but we pick
one explicitly. The floor is applied by the *consumer* (baseline/detector), not
baked into `RollingStats`, so the raw helper stays a pure stats primitive.

For Phase 1, `RollingStats` is used unbounded (no `maxlen`) to aggregate the
warmup slice. Phase 2 reuses the same class *with* `maxlen=window`. One class,
two call sites — that's the DRY win.

### Step 3 — `detector/baseline.py` (`Baseline` + `fit_baseline`)
- `Baseline`: a frozen `@dataclass` with three named `ChannelStats` fields
  (`execution_accuracy`, `query_valid`, `complexity_gap`), where
  `ChannelStats = (mean, std, n)`. **DECIDED: named fields** (not a stringly-typed
  dict) — explicit and typo-proof (maps to "explicit over clever"). Add a helper
  `get(channel: str)` only if the detector later needs a generic loop.
- `fit_baseline(records: list[TelemetryRecord], cfg: DetectorConfig) -> Baseline`:
  1. Guard: `len(records) >= cfg.baseline_len` else raise a clear `ValueError`
     (Phase 5 surfaces this as a friendly CLI error).
  2. Take `records[:cfg.baseline_len]`.
  3. For each channel, feed values into a `RollingStats`:
     - `execution_accuracy` → `r.execution_accuracy`
     - `query_valid` → `1.0 if r.query_valid else 0.0`
     - `complexity_gap` → `float(r.complexity_gap)`
  4. Read `(mean, max(std, cfg.std_floor), n)` into the `Baseline`.
  5. Return it. Baseline is immutable once returned (frozen dataclass) — "frozen
     after fit" is enforced by the type, not convention.

### Step 4 — Tests (`detector/tests/`)
`test_rolling.py`:
- mean/std on a known list matches `statistics.mean`/`stdev`
- n<2 → std 0.0 (no crash)
- all-identical values → std 0.0 (the zero-variance case)
- `maxlen` eviction keeps only the last k (proves Phase 2 reuse works)

`test_baseline.py` (loads the real mock via a small local raw-JSONL reader in the
test, since `eventlog.read_events` expects envelopes — see Open Questions):
- `fit_baseline(records[:40])`: acc mean ≈ 0.95 (±0.01), validity ≈ 1.0, gap ≈ −0.43
- `query_valid` std comes back == `std_floor` (NOT 0.0) — the floor is the
  headline assertion
- tiny hand-built lists: 5 records with known values → exact mean/std
- fewer records than `baseline_len` → raises `ValueError`
- empty list → raises `ValueError`

## Tradeoffs Considered

- **`Baseline` shape: named fields vs `dict[str, ChannelStats]`.** Dict is more
  extensible and lets the detector loop generically over channels; named fields
  are explicit and typo-proof. Chose **named fields + a `get()` escape hatch** —
  with exactly 3 fixed channels, the generality of a dict buys nothing and costs
  type safety. (Preference: explicit over clever; avoid premature abstraction.)
- **Std floor location: inside `RollingStats` vs in the consumer.** Baking it into
  the helper would make `RollingStats.std` lie (never returns true 0). Keeping the
  floor in `fit_baseline` keeps the primitive honest and lets Phase 2 choose its
  own flooring policy. Chose **consumer-side flooring**.
- **Sample (n−1) vs population (n) std.** Negligible at n=40; picked **sample** to
  match `statistics.stdev` so tests can assert against the stdlib directly.
- **Track latency/tokens in the baseline?** They're flat noise and never fire.
  Adding them is dead config. Chose **not to** — can add later if a channel ever
  needs them (YAGNI).
- **numpy vs stdlib.** stdlib `statistics` + a hand-rolled running-sum deque is
  plenty for 240 records and avoids a dependency in the detection core (rules/02
  reserves numpy/sklearn for the optional Phase 6). Chose **stdlib**.
- **Build config.py now vs defer Phase-2+ fields.** Declaring all fields now (even
  unused ones) avoids reshaping the dataclass every phase and documents the
  detector's full parameter surface in one place. Low cost, chose **declare now**.

## Validation

Run from repo root after each step:

```bash
# Step 2+3 smoke check (the verified target numbers):
python3 -c "
import json
from contracts.schemas import TelemetryRecord
from detector.config import DetectorConfig
from detector.baseline import fit_baseline
recs=[TelemetryRecord(**json.loads(l)) for l in open('fixtures/mock_telemetry.jsonl')]
b=fit_baseline(recs[:40], DetectorConfig())
print(b)
# expect: acc mean~0.95 std~0.221, valid mean~1.0 std==std_floor, gap mean~-0.425 std~0.501
"

# Step 4 — unit tests:
python -m pytest detector/tests/ -v
```

Pass criteria:
- accuracy mean in [0.94, 0.96], validity mean == 1.0, gap mean in [−0.5, −0.40]
- `query_valid` std == `cfg.std_floor` (proves the floor fired, not a raw 0.0)
- short/empty input raises `ValueError` with a readable message
- all `pytest` tests green

## Open Questions

- **Raw-JSONL loading.** The mock is raw `TelemetryRecord` JSONL, but
  `eventlog.read_events` expects typed envelopes. Phase 1 only needs records in
  tests, so I'll inline a 2-line `[TelemetryRecord(**json.loads(l)) for l in f]`
  reader there. The reusable loader + `--format` autodetect is **Phase 5's** job,
  not Phase 1's — flagging so we don't build it twice.
- **`std_floor` value — RESOLVED: fixed `1e-6` epsilon.** (Considered Bernoulli
  `sqrt(p(1−p))` for the binary channels but chose the simpler fixed floor: Phase 2
  fires on absolute drop, so the exact floor barely affects firing; the reported
  z-score is diagnostic only. Note `p=1.0` makes Bernoulli sd 0 anyway, so it would
  still need an epsilon backstop.)
