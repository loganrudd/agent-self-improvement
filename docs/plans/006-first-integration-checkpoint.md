# Plan 006: First Integration Checkpoint — harness → detector → correction → harness

**Status:** Draft
**Phase:** Integration checkpoint (rules/04 — the hour 5–6 "connect the rough pipe" milestone)
**Created:** 2026-06-27
**Files touched:**

_Created:_
- `docs/plans/006-first-integration-checkpoint.md` (this file)
- `correction/tests/test_correction.py` (new — contract-path unit tests)
- `correction/tests/fixtures/failing_cases.jsonl` (new — standalone failing-case bundle for the correction CLI)

_Modified (implemented):_
- `correction/teacher.py` (stub → MiniMax-M3 SQL generation, gold-verified)
- `correction/learner.py` (stub → verify + gold-fallback + anchoring → `list[FewShotExample]`)
- `correction/correction.py` (stub → `handle(event, failing_cases) -> CorrectionAction` + CLI)
- `orchestrator.py` (stub → **built in two passes**: first the skeleton + `--dry-run-heldout`
  headroom gate (Step 0.5), then the full two-pass live loop (Step 5))
- `harness/agent.py` (one-line: lift the `examples[:8]` prompt cap if anchors push past 8)

_Parked, not touched (off the contract path — kept so their tests stay green):_
- `correction/on_drift.py`, `graph.py`, `distill.py`, `repair.py`, `inject.py`, `store.py`, `contracts.py`

_Not touched (frozen / already correct):_
- `contracts/schemas.py` (frozen), `contracts/eventlog.py`, `detector/*`, `harness/feed.py`, `harness/runner.py` `_active_config` (feedback read already works), `viewer/*`

## Goal

One sentence: replaying a small stream end-to-end produces a single `events.jsonl`
containing `telemetry → drift → correction → telemetry`, where the recovery-phase
hard-bucket accuracy is **measurably higher** than the degraded-phase hard-bucket
accuracy — and the viewer renders that V — because the agent learned teacher-generated,
execution-verified few-shot examples, with **zero human in the loop**.

## Context

What we learned in the Explore step (see the session that produced this plan):

- **The repo is already integrated-ish.** `main` ("all added") has real code in all four
  stages and **154 tests pass**. The feature branches are *behind* main (stale). We are
  wiring code that already exists, not assembling branches.
- **The feedback path is already wired through the event log.** `harness._active_config`
  ([harness/runner.py:21-27](../../harness/runner.py#L21-L27)) re-reads the latest
  `CorrectionAction` from `events.jsonl` each item and swaps in its `few_shot_examples`;
  the agent prompt already consumes them ([harness/agent.py:62-68](../../harness/agent.py#L62-L68)).
  So "correction → harness" needs **no new code** — correction just has to append a
  `CorrectionAction` to the log.
- **The detector is contract-clean.** It emits one `DriftEvent` with `failure_mode` +
  `failing_run_ids` (cap 8) ([detector/detector.py:279-287](../../detector/detector.py#L279-L287)).
- **The viewer is contract-clean.** It reads `CorrectionAction.new_few_shot_examples`
  ([viewer/app.py:131-138](../../viewer/app.py#L131-L138)) and honors `VIEWER_LOG`.
- **The one real mismatch is correction.** The built correction stage is a Gemini
  knowledge-graph (`on_drift_event → CorrectionRule`, injected as prompt *text* —
  [correction/on_drift.py:22-50](../../correction/on_drift.py#L22-L50),
  [correction/inject.py](../../correction/inject.py)). Nothing downstream reads that:
  harness/agent/viewer all expect `CorrectionAction[FewShotExample]`. The contract-aligned
  files are empty stubs ([correction.py](../../correction/correction.py),
  [teacher.py](../../correction/teacher.py), [learner.py](../../correction/learner.py)).
  **Decision (locked): implement the contract path; park the graph machinery.**
- **`DriftEvent` carries only `failing_run_ids`, not the cases.** `TelemetryRecord` has
  `question`/`generated_sql`/`db_id` but **no `gold_sql`** ([schemas.py:71-75](../../contracts/schemas.py#L71-L75)).
  The orchestrator holds both the emitted records *and* the `FeedItem`s (which carry
  `gold_sql`), so it can map each `failing_run_id` → `(record, gold_sql)` and hand
  correction a complete bundle. **Decision (locked): orchestrator passes the failing
  cases directly; correction rebuilds schema via `harness.spider`.**
- **Teacher = MiniMax-M3 + execution-verify with gold fallback** (locked). Same family as
  the base agent, stronger tier. Because we verify teacher SQL by executing it against the
  DB and fall back to the dataset's `expected_sql` on a miss, every injected few-shot is
  guaranteed correct, and teacher quality becomes low-stakes (yield only, not correctness).
  Story for judges: "the same model family, prompted differently, generates its own
  corrections — and the base tier improves because of them." Base stays
  `MiniMax-M2.7-highspeed`; teacher = `MiniMax-M3`.
- **Subtlety in the feedback read:** `_active_config` *replaces* `few_shot_examples` with
  the latest correction's list (`model_copy(update=...)`), it does not append. For a single
  drift episode (this checkpoint) that's equivalent to appending to an empty list. It means
  the `CorrectionAction` must carry the **complete** curated set (hard examples **+** easy
  anchors) — anchoring lives inside the `CorrectionAction`, not in the harness.
- **`examples[:8]` cap.** The agent truncates the prompt to 8 examples
  ([agent.py:65](../../harness/agent.py#L65)) and the detector caps `failing_run_ids` at 8.
  If we add easy anchors on top of ~8 hard examples we exceed the slot budget — hence the
  one-line cap lift.
- **run_id formats differ.** Mock telemetry uses `run_0000`; the real harness emits
  `{question_id}_{uuid8}` ([runner.py:44](../../harness/runner.py#L44)). The orchestrator
  passes explicit bundles (no run_id parsing), so this only matters for correction's
  **standalone** CLI — which gets its own `failing_cases.jsonl` fixture rather than looking
  anything up.

## Approach

Built bottom-up so each layer is validated against the layer below before the orchestrator
ties them together. Run all commands from repo root with `MINIMAX_API_KEY` exported.

### Step 0 — Pre-flight: degradation signal (no code)

**Already done (step 0 numbers from session):**
- baseline: **0.88** avg accuracy (8 runs)
- degraded: **0.38** avg accuracy (8 runs)  ✓ clearly low
- recovery: **1.00** avg accuracy (8 runs)  ⚠ see Step 0.5

The base genuinely fails on hard queries. Proceed.

### Step 0.5 — BUILD FIRST: `orchestrator.py` skeleton + `--dry-run-heldout` (headroom gate)

**Why this is the first build:** `recovery=1.00` at n=8 is a small-sample red flag. The
LEARN/HELD-OUT split is difficulty-matched (both 18-20 hard + 5-7 extra) but at n=8 the
variance is huge. The base is deterministic (`temperature=0.0`), so base accuracy on the
held-out pool is a *fixed* number — if those 25 questions happen to be ones the base already
solves, the recovery climb is illusory at large n too. We must measure this **before**
investing in the correction stack. Building it as an orchestrator flag (not a throwaway
script) means the skeleton — arg-parsing, feed construction, base-config run — is reused
by the full loop in Step 5.

- **Build (`orchestrator.py`):**
  - `argparse`: `--n` (per-phase count, default 40), `--full` (80/phase), `--dry-run-heldout`
    (run only the held-out pool at base config, print accuracy, **exit** — no detector, no
    correction, no event-log writes), `--fresh` (truncate `events.jsonl` before a real run).
  - `_build_feed(n, full)` → calls `harness.spider.load_questions()` +
    `harness.feed.build_stream(...)` with the **same `seed=42`** the real run uses; returns
    the `FeedItem` list. Held-out items = `[it for it in items if it.phase == "recovery"]`.
  - `dry_run_heldout(items)`: build a fresh `AgentConfig(config_id="v0-base",
    model="MiniMax-M2.7-highspeed", few_shot_examples=[])`, call
    `harness.runner.run_item(item, config)` per held-out item (note: `run_item` does **not**
    touch `events.jsonl` and does **not** call `_active_config`, so this is contamination-free
    by construction), collect accuracy, print **overall + per-difficulty + n scored / n
    skipped** (gold-SQL failures).
- **Run:** `python orchestrator.py --n 40 --dry-run-heldout`
- **Validate / gate:** base accuracy on the held-out pool is **low** (≲0.5). If it's high
  (≳0.7), **stop** — re-seed or re-stratify the split (e.g. change `seed`/`learn_frac` in the
  feed, or curate a genuinely-hard held-out pool) before any correction work, because the
  recovery climb would otherwise be an artifact of pool difficulty, not learning. Record the
  number in Open Questions.

### Step 1 — `correction/teacher.py` (MiniMax-M3 SQL generation)

- **Build:** `generate_sql(question: str, schema_text: str, model: str | None = None) -> str`.
  Uses the same MiniMax OpenAI-compat client already wired in [agent.py:44-50](../../harness/agent.py#L44-L50)
  (`MINIMAX_API_KEY`, `TEACHER_MODEL` env var, default `MiniMax-M3`). Strip fences. No ReAct
  loop — one shot; verification + fallback happen in the learner, off the hot path.
- **Validate:** unit test with the OpenAI client monkeypatched to return a canned response →
  asserts fences stripped and the model id is read from `TEACHER_MODEL`. One live smoke
  (guarded/skippable without a key): a known hard question returns a `SELECT`.

### Step 2 — `correction/learner.py` (verify + gold-fallback + anchor)

- **Build:**
  - `make_examples(failing_cases, anchor_cases=(), teacher=teacher.generate_sql) -> list[FewShotExample]`.
  - For each failing case: rebuild schema via `harness.spider.schema_text(get_db_path(db_id))`,
    call the teacher, **verify** by executing teacher-SQL vs the case's `gold_sql` with
    `harness.evaluator.execution_accuracy`. On match → `FewShotExample(source="teacher")`; on
    miss → fall back to `gold_sql` (`source="gold"`); never inject unverified SQL.
  - Append the easy `anchor_cases` as `source="anchor"` (gold SQL of baseline successes) so
    easy-bucket skill is retained (the anti-forgetting beat; minimal-but-real per
    [correction/CLAUDE.md](../../correction/CLAUDE.md)).
  - Make the DB/verify dependency injectable so the unit tests don't need a live model or key.
- **Validate:** unit tests with a fake teacher — (a) teacher returns correct SQL → example
  uses it, `source="teacher"`; (b) teacher returns garbage → falls back to gold,
  `source="gold"`; (c) anchors appear with `source="anchor"`; (d) total count ≤ the agent
  prompt cap.

### Step 3 — `correction/correction.py` (`handle` + standalone CLI)

- **Build:** a lightweight internal `FailingCase` (`run_id, question, db_id, broken_sql,
  gold_sql, difficulty`) — *not* the graph path's `FailedRun`. `handle(event: DriftEvent,
  failing_cases, anchor_cases=()) -> CorrectionAction`: gate on `event.severity` (reuse the
  0.2 threshold idea from [on_drift.py:19](../../correction/on_drift.py#L19)), call
  `learner.make_examples`, return `CorrectionAction(triggered_by=event.channel,
  new_few_shot_examples=..., rationale=...)`. `__main__`: read
  `fixtures/mock_drift_events.jsonl` + the new `failing_cases.jsonl` bundle, print the action,
  optionally append to a log.
- **Validate:** `python -m correction.correction --drift fixtures/mock_drift_events.jsonl
  --cases correction/tests/fixtures/failing_cases.jsonl` prints a `CorrectionAction` with
  N>0 examples. Unit test asserts the returned `CorrectionAction` shape and that a
  below-threshold severity yields an empty action.

### Step 4 — harness feedback-read confirmation (+ cap lift)

- **Build:** if Step 2 emits >8 total examples, change `examples[:8]` →
  `examples[: config-or-larger]` in [agent.py:65](../../harness/agent.py#L65). Otherwise no
  change.
- **Validate:** unit test — write a `CorrectionAction` to a temp log, call
  `harness.runner._active_config(base)`, assert the returned config's `few_shot_examples`
  equals the action's list. (Proves the already-built feedback read works with our output.)

### Step 5 — `orchestrator.py` (two-pass live loop — extends the Step 0.5 skeleton)

- **Build:** the spine, reusing `_build_feed`, the arg-parser, and `--fresh` from Step 0.5.
  1. Start from a **fresh** `events.jsonl` (`--fresh` truncates it; append-only log must not
     carry prior runs).
  2. `_build_feed(...)` once; keep the `FeedItem` list (it has `gold_sql`). Also retain
     the held-out `FeedItem`s separately — needed for the clean comparison in validation
     (reuse `dry_run_heldout` from Step 0.5 for the without-examples leg).
  3. **Pass 1 (baseline + degraded):** for each item run the agent (base config), emit
     `TelemetryRecord` to the log, feed it to `detector.update()`. On the returned
     `DriftEvent`: append it, then map each `event.failing_run_ids` → its `(record, FeedItem)`
     to build `FailingCase` bundles; pick a few baseline successes as `anchor_cases`; call
     `correction.handle(...)`; append the `CorrectionAction`.
  4. **Pass 2 (recovery):** run the recovery (held-out) items. The harness's `_active_config`
     reads the freshly-appended `CorrectionAction` from the log → the agent now prompts with
     the few-shots → emit recovery telemetry.
  5. **Comparison print:** for the clean with-vs-without signal, also run the held-out items
     once at *base config* (no correction) and print both numbers side-by-side. This is the
     unambiguous improvement claim: same pool, same questions, same base model — only the
     few-shots differ.
- **Validate:** `python orchestrator.py --full` (≥40 baseline to satisfy `baseline_len=40`,
  ≥25 degraded to fill `window=25` and sustain `min_sustained=5` breaches) produces an
  `events.jsonl` whose event types, in order, are `telemetry…* → drift → correction →
  telemetry…*`, and where held-out accuracy **with examples > held-out accuracy without**.
  Print both numbers at the end.

### Step 6 — viewer over the live log (eyeball)

- **Do:** `VIEWER_LOG=events.jsonl .venv/bin/uvicorn viewer.app:app --port 8011` → open the page.
- **Validate:** `/api/state` returns non-null `drift` and `correction` marks; the curve shows
  the V (drop at the change-point, climb after the correction mark); the example panel shows a
  hard query going `valid_but_wrong`/`invalid` before and `correct` after.

## Tradeoffs Considered

1. **Contract path vs. graph path (the big one).** The graph machinery (rules → prompt text,
   Gemini repair/distill, networkx) is genuinely nice but is wired to *nothing* downstream and
   contradicts the frozen contract + the "few_shot_examples *is* the learning" spine
   (rules/00, rules/01). Adopting it means rewriting harness, agent, **and** viewer; the
   contract path means filling three stubs and writing one orchestrator. **Chosen:** contract
   path. Park (don't delete) the graph code so its 154-green tests stay green and it remains a
   post-checkpoint stretch. _Maps to your prefs: don't fight the frozen contract; ship the
   spine first; avoid premature abstraction._

2. **Teacher = MiniMax-M3 (same family) + verify-with-gold-fallback** vs. Gemini vs. gold-only.
   The core claim is "the *same model* improves by seeing its own corrected failures as
   examples." A same-family teacher (MiniMax-M3 → base MiniMax-M2.7) keeps that story clean:
   no third party involved, one model ecosystem. Gemini-as-teacher would muddy it ("did you
   just use a different, better model?"). Gold-only skips the teacher entirely — simpler, but
   the theme (rules/02) explicitly calls for "a stronger teacher model generates corrected
   SQL." Because we execute-verify and fall back to `expected_sql` on a miss, every injected
   few-shot is correct regardless of teacher yield — so credibility > yield here. **Chosen:**
   MiniMax-M3 teacher, gold as verification oracle + fallback. _Maps to your prefs: explicit
   improvement story, robust against teacher misses._

3. **Two-pass vs. live mid-stream detection.** Live interleaving would need `run_stream` to
   become a generator and the orchestrator to inject `detector.update()` per item — more
   surgery on the harness. Two-pass (run baseline+degraded, fire, correct, then run recovery)
   exercises the entire feedback path with the harness essentially unchanged. **Chosen:**
   two-pass for the checkpoint; live streaming is a polish item. _Maps to your prefs: simplest
   thing that proves the pipe._

4. **Orchestrator passes explicit bundles** vs. correction parsing `run_id` prefixes vs.
   correction re-reading `events.jsonl`. The orchestrator already holds the records *and* the
   `FeedItem`s (gold), so handing correction a complete `(record, gold_sql)` bundle is the
   explicit, no-coupling choice and survives the mock-vs-real run_id format gap. **Chosen:**
   explicit bundles. _Maps to your prefs: explicit over clever; no fragile string parsing._

5. **Correction imports `harness.evaluator`/`harness.spider`** (verify + schema) vs. the
   orchestrator pre-verifying. Correction owns "learn from failures" (rules/02), and verify +
   schema-build are the heart of that, so they belong in correction; the eval/schema helpers
   are genuinely shared utilities. Making the DB dependency injectable keeps correction unit
   tests hermetic. **Chosen:** correction imports them, dependency injected for tests. _Maps to
   your prefs: clear ownership; tests non-negotiable._

6. **Learn only from `event.failing_run_ids`** (≤8, detector-curated) vs. all degraded
   failures the orchestrator holds. Honoring the curated set keeps the detector→correction seam
   meaningful (the detector *chose* what to learn from) and bounds teacher calls. **Chosen:**
   the curated ids; revisit the cap if recovery is weak.

7. **Minimal anchoring** (1–2 easy anchors) vs. full DER++. With only ~8 prompt slots, full
   continual-learning rigor doesn't fit and isn't the checkpoint's point. **Chosen:** a couple
   of gold anchors now; DER++ depth only if time remains (rules/02, correction/CLAUDE.md).

8. **`--full` run (≥40/phase) vs. small-n.** At n=8 the detector never leaves WARMUP
   (`baseline_len=40`). The checkpoint *must* run `--full` (80/phase) or at least
   `--n 40` to satisfy warmup + window + sustained-breach requirements. Small-n is fine
   for Step 0's initial degradation check (just looking at raw accuracy, not the detector),
   but Steps 5–6 need enough records to fire. **Chosen:** `--full` for Steps 5–6; Step 0's
   degradation snapshot can stay small-n. _Discovered from DetectorConfig.baseline_len=40._

## Validation

The checkpoint passes when **all** hold:

- `python -m pytest -q` stays green (existing 154 + the new correction tests).
- `python orchestrator.py --full` writes one `events.jsonl` with event order
  `telemetry* → drift → correction → telemetry*` (exactly one drift, one correction).
- **Held-out HARD-bucket accuracy WITH few-shot examples > WITHOUT** (same pool, same
  questions, same base model, printed side-by-side by the orchestrator). The **hard bucket
  (base 0.581)** is the demo claim — the extra bucket is pinned at 1.00 (no headroom) and
  the overall 0.675 is diluted by it, so report hard explicitly. This is the clean
  with-vs-without comparison — it controls for pool difficulty and isolates the effect of
  the examples. The LEARN/HELD-OUT split ([feed.py:29-87](../../harness/feed.py#L29-L87))
  guarantees the held-out questions were never used as few-shot source.
  - Target: hard 0.58 → ~0.85 (fixing ~5 of the 8 failing unique questions). A moderate but
    honest V; do not inflate it by re-seeding to an artificially easy split.
- Every injected `FewShotExample` is execution-verified or gold (`source` ∈
  {`teacher`,`gold`,`anchor`}).
- `VIEWER_LOG=events.jsonl` viewer shows the V with drift + correction marks.

## Open Questions

- **Step 0 numbers (filled):** baseline=0.88, degraded=0.38 at n=8. Base genuinely fails on
  hard. ✓ (n=8 was noisy — see Step 0.5.)
- **Step 0.5 headroom gate (FILLED, n=40):** held-out base accuracy = **0.675 overall**,
  **0.581 hard (20 unique)**, **1.00 extra (5 unique)**, 0 skipped. Gate **passed** — real
  headroom on the hard bucket (~8 of 20 unique hard questions fail at base). Confirms the
  n=8 `recovery=1.00` was sampling noise.
  - **extra=1.00 is not a data bug:** the 5 extra questions are genuinely complex
    (INTERSECT/EXCEPT/multi-join) but template-matchable; **Spider difficulty ≠ model
    difficulty**. Extra has no headroom — exclude it from the improvement claim.
- **Degraded (LEARN) base accuracy at n=40 — UNMEASURED.** This is what fires the detector
  (needs baseline−degraded ≥ 0.20 sustained). n=8 gave 0.38 (drop ≈0.50 ≫ 0.20), so firing
  is very likely, but it's the main Step-5 risk. Optionally add a `--dry-run-degraded` mirror
  of Step 0.5 to de-risk before the full run.
- **MiniMax-M3 model ID:** confirm the exact API model string (check docs or a test call in
  Step 1; likely `MiniMax-M3` matching the docstring in [agent.py:9](../../harness/agent.py#L9)).
- **Anchor count:** 1–2 gold easy anchors to avoid crowding the prompt. Settle in Step 2;
  total (hard examples + anchors) should stay ≤ the cap.
- **Cap lift:** if hard examples + anchors exceed 8, lift `examples[:8]` in agent.py (Step 4).
  Confirm acceptable to harness owner before touching.
- **Log hygiene:** orchestrator must start from a clean `events.jsonl`; add a `--fresh` flag
  or rename the output (e.g. `events_run_{ts}.jsonl`) so re-runs don't stack corrections.
- **Severity threshold alignment:** detector fires ~0.35 on the mock; real degraded run at
  0.38 baseline suggests similar severity — should clear correction's ≥0.2 gate. Verify at
  Step 5.
- **Post-build experiment — weaker base model:** The "extra" bucket is pinned at 1.000
  (all 5 unique extra questions are template-matchable INTERSECT/EXCEPT; base solves them
  every time). A weaker base model (e.g., a smaller MiniMax tier) could deepen the V and add
  headroom on "extra", making the demo more dramatic. Preconditions before attempting: (a) the
  current base V is working end-to-end (Steps 1–5 green); (b) the candidate weaker model is
  tested for in-context learning — it must also *recover* with examples, not just fail without
  them (a flat-bottom curve is strictly worse than the current demo); (c) baseline accuracy
  on easy/med must stay ≥ 0.85 so the change-point is clean. Acceptance test: run
  `--dry-run-heldout` with the new model; keep whichever produces the deeper recovering V
  on the hard bucket specifically (20 questions, robust). This is a `model=` one-liner swap
  if the correction stack is already built. Attempt only if time allows post-Step 5.
