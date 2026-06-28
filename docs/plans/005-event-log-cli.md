# Plan 005: Event-Log Wiring + Standalone CLI

**Status:** Complete
**Phase:** 5 of 6 (detector — see `docs/detector-plan.md`)
**Created:** 2026-06-27
**Files touched:**
- `detector/detector.py` (modify — add an input loader, a summary printer, `run()` orchestration, `build_arg_parser()`, `main()`, and `if __name__ == "__main__"`)
- `detector/tests/test_cli.py` (**new** — CLI + loader + e2e round-trip tests; kept separate from `test_detector.py` so the core's 83 tests stay focused on detection logic)
- `contracts/schemas.py` (**not touched** — frozen; `DriftEvent` already carries every field we persist)
- `contracts/eventlog.py` (**not touched** — `append_event` / `read_events(only=...)` used as-is)
- `detector/config.py` (**not touched** — every CLI flag maps to an existing `DetectorConfig` field)
- `docs/detector-plan.md` (modify — mark Phase 5 done, correct any verify numbers if they drift; mirrors how Plans 002–004 reconciled their own numbers)

## Goal

`python -m detector.detector --input fixtures/mock_telemetry.jsonl` reads the
telemetry stream, drives the existing `Detector`, prints a readable summary
(baseline → progress → the fire moment + stratified breakdown), and appends the
one `DriftEvent` to `events.jsonl` via `contracts.eventlog.append_event` — such
that `read_events(only="drift")` returns exactly one event with the Phase-4
fields intact. "Done" = the command runs end-to-end on the mock, all edge-case
inputs fail loudly or exit cleanly (never silently), and the new CLI tests +
all 83 prior tests are green.

## Context

What we learned in the Explore step:

- **Phases 1–4 are done and green (83 tests).** The `Detector`
  ([detector/detector.py](../../detector/detector.py)) is a pure, stateful
  library: `update(record) -> DriftEvent | None`, plus `stratified_means()` for
  the per-difficulty breakdown. It fires once at idx≈89 on the mock with
  `failure_mode == VALID_BUT_WRONG` and a populated `failing_run_ids`. **Phase 5
  adds only a CLI shell around it — no change to detection logic.**
- **There is no entry point yet.** No `main()`, no `argparse`, no
  `if __name__ == "__main__"` exists anywhere in `detector.py`. The
  `python -m detector.detector` command advertised in
  [detector/CLAUDE.md:32](../../detector/CLAUDE.md#L32) does not run today.
- **The event-log helper is ready as-is** ([contracts/eventlog.py](../../contracts/eventlog.py)):
  `append_event(record, path)` wraps any contract record in a typed envelope and
  appends; `read_events(path, only="drift")` parses them back. No new format to
  invent (rules/00 forbids a second log format).
- **Every CLI flag already has a config field.** `DetectorConfig`
  ([detector/config.py](../../detector/config.py)) has `window=25`,
  `baseline_len=40`, `drop_threshold=0.20`, `failing_ids_cap=8`. The CLI is a
  thin `argparse → DetectorConfig(...)` mapping — no new config surface.
- **The headline gotcha — two input formats.**
  `fixtures/mock_telemetry.jsonl` is **raw `TelemetryRecord` JSONL** (first key
  is `run_id`), but `read_events` expects **typed envelopes** (first key `type`,
  payload under `data`). Both shapes exist in the repo —
  `fixtures/mock_events.jsonl` is the enveloped one. The standalone demo path
  reads raw; the orchestrator path reads enveloped. The CLI must handle both.
  The two are trivially distinguishable: an envelope line has a top-level
  `"type"` key, a raw record does not.
- **Test conventions to reuse** (test_detector.py): `_load_mock`, `_run_stream`,
  `_make_rec`, `_outage_rec`, and the three-tier (A regression / B unit /
  C mock-pinned) layout. New CLI tests follow the same shape; use `tmp_path` for
  output isolation so re-runs never pollute a shared `events.jsonl`.

## Approach

Five steps, each runnable against the mock before the next. Detection logic is
never touched — Phase 5 is pure glue around the finished `Detector`.

### Step 1 — Input loader (raw + enveloped, with autodetect)

Add to `detector/detector.py` a small loader that yields `TelemetryRecord`s from
a JSONL file, tolerant of both formats and of malformed lines:

```python
def _looks_enveloped(line: str) -> bool:
    """An eventlog envelope has a top-level "type"; a raw TelemetryRecord does not."""
    obj = json.loads(line)
    return isinstance(obj, dict) and "type" in obj and "data" in obj

def load_telemetry(path, fmt="auto"):
    """Yield TelemetryRecord from a JSONL file.

    fmt: "raw" (bare TelemetryRecord lines), "events" (typed envelopes,
    telemetry-only), or "auto" (sniff the first non-blank line).
    Malformed lines are skipped with a stderr warning; non-telemetry envelopes
    are skipped silently (the orchestrator log interleaves drift/correction).
    """
```

- `auto` sniffs the first non-blank line via `_looks_enveloped`; envelope ⇒ use
  `read_events(path, only="telemetry")`, raw ⇒ parse each line as
  `TelemetryRecord(**json.loads(line))`.
- Malformed line ⇒ count + warn to stderr, skip (don't abort the stream).
- Empty / missing file ⇒ raise a clear `SystemExit`-friendly error in Step 3's
  `run()`, not here (keep the loader a pure generator).

**Validate before Step 2:**
```bash
python3 -c "
from detector.detector import load_telemetry
raw = list(load_telemetry('fixtures/mock_telemetry.jsonl'))
env = list(load_telemetry('fixtures/mock_events.jsonl'))
print('raw count :', len(raw))    # 240
print('env count :', len(env))    # 240 (telemetry-only; drift/correction skipped)
print('same ids  :', [r.run_id for r in raw] == [r.run_id for r in env])  # True
"
```
Expect: 240 / 240, identical id order from both formats.

### Step 2 — Summary printer

Add a `print_summary(...)` (writes to stdout) covering the three demo beats from
the plan (detector-plan line 83): **baseline**, **progress**, **fire moment +
stratified breakdown**. Pure formatting — takes the baseline, the fired event,
its fire index, and `stratified_means()` snapshot; returns nothing.

- Baseline block: the frozen baseline means (accuracy / validity / gap) and N.
- Fire block: index, `channel`, `window_mean`, `baseline_mean`, `severity`,
  `failure_mode`, count of `failing_run_ids`.
- Stratified block: each difficulty bucket's current windowed accuracy (omit
  empty buckets — `stratified_means()` already does this).
- No-drift case: a single clear "no drift detected over N records" line.

Keep it plain text (no color/deps); explicit over clever.

**Validate before Step 3:** call `print_summary` from a one-off `python3 -c`
with a hand-built event and confirm the three blocks render. (Fuller validation
comes via Step 3's real run.)

### Step 3 — `run()` orchestration + edge-case handling

Add `run(args) -> int` (returns an exit code) that ties it together:

1. Resolve input path; **missing/empty file ⇒ print error to stderr, return 2.**
2. Build `DetectorConfig` from the flags.
3. Stream `load_telemetry(...)` into a fresh `Detector`; capture the first event
   and its index; snapshot `det.stratified_means()` **immediately** at fire.
4. **Fewer records than `baseline_len` ⇒** detector never leaves WARMUP; print a
   clear "need ≥ baseline_len records, got N" error, return 2 (do **not**
   silently print nothing).
5. On fire: `append_event(event, args.output)` and `print_summary(...)`.
6. No drift: print the no-drift summary, append nothing, return 0.

Default `--output` = `events.jsonl` (repo root, matches `eventlog.DEFAULT_LOG`).
Note in `--help` that append is additive (re-running stacks events) — tests use
`tmp_path` to stay isolated.

**Validate before Step 4:**
```bash
rm -f /tmp/ev.jsonl
python3 -m detector.detector --input fixtures/mock_telemetry.jsonl --output /tmp/ev.jsonl
python3 -c "
from contracts.eventlog import read_events
ev = read_events('/tmp/ev.jsonl', only='drift')
print('n drift     :', len(ev))                 # 1
e = ev[0]
print('channel     :', e.channel)               # execution_accuracy
print('failure_mode:', e.failure_mode)           # FailureMode.VALID_BUT_WRONG
print('n failing   :', len(e.failing_run_ids))   # <= cap (8)
"
```
Expect: summary prints the fire + stratified breakdown; exactly one drift event
round-trips with the Phase-4 fields intact.

### Step 4 — `argparse` + `main()` + module entry point

- `build_arg_parser()` with `--input` (required), `--output`
  (default `events.jsonl`), `--window`, `--baseline`, `--drop-threshold`,
  `--cap`, `--format {auto,raw,events}` (default `auto`). Defaults pulled from
  `DetectorConfig()` so the CLI and config never disagree (DRY).
- `main(argv=None) -> int` parses and calls `run`.
- `if __name__ == "__main__": raise SystemExit(main())`.

**Validate before Step 5:**
```bash
python3 -m detector.detector --help          # all flags listed with config defaults
python3 -m detector.detector --input fixtures/mock_events.jsonl --output /tmp/ev2.jsonl  # enveloped path works too
```

### Step 5 — Tests (`detector/tests/test_cli.py`) + doc update

New test module, same three-tier convention, `tmp_path` for all output:

**Tier A — loader invariants:**
- Raw and enveloped fixtures yield identical `run_id` order, 240 each.
- Malformed line in a temp file ⇒ skipped with warning, surrounding records
  still load.
- `auto` correctly classifies each fixture (raw vs. envelope).

**Tier B — `run()` / edge cases (hand-built temp inputs):**
- Missing file ⇒ exit code 2, stderr message, nothing appended.
- Empty file ⇒ exit code 2, clear message.
- Fewer than `baseline_len` records ⇒ exit code 2, "need ≥ N" message
  (not a silent success).
- No-drift input (all-good stream) ⇒ exit 0, no event appended, no-drift line
  printed.
- Re-running twice on the same `tmp_path` output appends two events (documents
  the additive contract).

**Tier C — e2e round-trip (the integration proof, mock-pinned):**
- Full mock through `main(["--input", mock, "--output", tmp])` ⇒
  `read_events(only="drift")` length 1, `channel == "execution_accuracy"`,
  `failure_mode == VALID_BUT_WRONG`, `failing_run_ids` non-empty and `≤ cap`,
  every id a real `execution_accuracy == 0` run.
- `--cap 3` flag is honored end-to-end (≤ 3 ids in the persisted event) —
  proves the flag→config→event wiring.

**Doc update:** flip Phase 5 to done in `docs/detector-plan.md`; reconcile the
verify line if any printed number differs from what actually renders.

**Validate:** `python3 -m pytest detector/tests/ -v` — 83 prior + new
`test_cli.py`, all green.

## Tradeoffs Considered

1. **Two input formats: autodetect vs. mandatory `--format`.**
   - (a) **Autodetect by sniffing the first line, with `--format` override
     (default `auto`).** Zero friction for the demo (`--input
     fixtures/mock_telemetry.jsonl` just works) and for the orchestrator
     (enveloped log just works); the explicit flag is the escape hatch when a
     file is ambiguous or a caller wants to assert intent.
   - (b) **`--format` required, no autodetect.** Most explicit, but makes the
     headline demo command longer and is a papercut every run; the formats are
     unambiguously distinguishable (`"type"` key), so forcing the human to
     specify is needless ceremony.
   - (c) **Support only raw; make the orchestrator pre-convert.** Simplest CLI,
     but punts the enveloped path the plan explicitly calls out (detector-plan
     line 85) and would surprise anyone pointing it at `events.jsonl`.
   - **Chose (a).** It satisfies both documented call paths with the least
     friction, and the override keeps it explicit-when-it-matters. Maps to:
     explicit over clever (a 3-line sniff, not magic), no premature abstraction
     (no pluggable-reader framework for two formats), thorough on the real
     gotcha the plan flagged.

2. **Where `main()` lives: `detector.py` vs. a new `cli.py`.**
   - `detector.py` keeps `python -m detector.detector` working (the command
     already in CLAUDE.md and the plan) with no extra indirection. A separate
     `cli.py` would be marginally cleaner separation but changes the advertised
     command and adds a file for ~80 lines of glue.
   - **Chose `detector.py`.** Honors the locked run command; the module already
     owns the detector, and the CLI is its natural front door. Avoids a
     premature split.

3. **New `test_cli.py` vs. extending `test_detector.py`.**
   - A separate module keeps the 83 detection-logic tests focused and avoids a
     file that mixes "does the math fire correctly" with "does argparse exit 2."
   - **Chose a new module.** Same three-tier convention, reuses the loader-level
     fixtures; keeps each test file's job legible. (Helpers like `_load_mock`
     are tiny — re-importing or duplicating the one-liner is cheaper than
     coupling the two suites.)

4. **Exit codes & error surfacing.** Edge cases (missing/empty/too-short input)
   **return a non-zero exit (2) and write to stderr**, rather than printing a
   friendly message and exiting 0. A demo that silently prints nothing on a
   too-short file is the exact failure mode the plan warns against
   (detector-plan line 87). Non-zero + stderr is the explicit, scriptable,
   testable choice and matches your "thorough on failure modes" preference.

5. **Append vs. truncate on `--output`.** `eventlog.append_event` only appends;
   re-running stacks events. Rather than add truncate logic (a second write
   path, more surface), we **keep append-only** (consistent with the
   orchestrator's one append-only log, rules/00) and document it in `--help`;
   tests isolate via `tmp_path`. Truncation can be added later if a real need
   appears — not premature now.

6. **Snapshotting `stratified_means()` at fire vs. reading it after the loop.**
   The per-difficulty windows keep sliding after the event (recovery records
   arrive), so reading them post-loop would show the *recovered* numbers, not
   the *fire-moment* breakdown the summary claims. We snapshot **immediately**
   when `update()` returns the event. Explicit and correct; costs one dict copy.

## Validation

Run from repo root:
```bash
# End-to-end demo (raw input, the headline command):
python3 -m detector.detector --input fixtures/mock_telemetry.jsonl --output /tmp/ev.jsonl
python3 -c "from contracts.eventlog import read_events; \
print(len(read_events('/tmp/ev.jsonl', only='drift')))"   # 1

# Enveloped input path:
python3 -m detector.detector --input fixtures/mock_events.jsonl --output /tmp/ev2.jsonl

# Full suite:
python3 -m pytest detector/tests/ -v   # 83 prior + test_cli.py, all green
```
Pass criteria:
- Headline command prints baseline → fire → stratified breakdown and appends
  exactly one drift event; `read_events(only="drift")` returns it with
  `channel == "execution_accuracy"`, `failure_mode == VALID_BUT_WRONG`,
  `failing_run_ids` non-empty and `≤ cap`.
- Both raw and enveloped inputs yield the identical 240-record stream and the
  identical single event.
- Missing / empty / too-short input ⇒ exit 2 + stderr message, nothing appended.
- No-drift input ⇒ exit 0, no event, "no drift" line.
- `--cap N` and the other flags flow through to the persisted event.
- Phases 1–4 behavior unchanged — one event at idx≈89, none in baseline/recovery.

## Open Questions

- **Default `--output` location.** Plan assumes repo-root `events.jsonl`
  (matches `eventlog.DEFAULT_LOG`). If the orchestrator wants a different shared
  path at integration, it's a one-flag change — defer until orchestrator wiring.
- **Progress output volume.** "progress" (detector-plan line 83) could be a
  per-record trickle or a few milestone lines (warmup-done, window-full,
  fire). Recommendation: **milestone lines only** — a 240-line trickle buries
  the fire moment in the demo. Confirm during execution.
- **`--format events` with a drift/correction-bearing log.** When pointed at a
  full orchestrator log, non-telemetry envelopes are skipped (we only feed
  telemetry to the detector). Correct for standalone runs; flagged so it isn't
  mistaken for data loss.
