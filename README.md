# Agent Self-Improvement

**Drift detection and automated self-improvement for AI agents.**

A text-to-SQL agent runs in "production" against the [Spider](https://yale-lily.github.io/spider)
benchmark. When its accuracy **drifts** as queries get harder, the system detects the
degradation, makes the agent **learn from its own failures** — a teacher model turns the
agent's mistakes into few-shot examples — and the agent **recovers on questions it has never
seen**. No human in the loop.

> Built for **The Self-Improvement Stack** track at the AI Engineer World's Fair Hackathon.
> **Every commit in this repository was written during the event (June 27, 2026).** No
> pre-existing project code is included. The text-to-SQL agent is throwaway scaffolding —
> the detection + learning loop is the build.

---

## Results

The agent learned from its own failures and **nearly doubled its accuracy on hard queries it
had never seen as examples** — same questions, same difficulty, no human in the loop and no
model swap:

| Hard-bucket execution accuracy (same 30 held-out questions, same eval) | Accuracy |
|---|---|
| Base agent — no examples | 0.300 |
| Teacher model (MiniMax-M3) — the stronger model used to generate examples | 0.421 |
| **Base agent after self-correction — learned few-shot examples** | **0.567** |
| **Improvement over base** | **+0.267** |

The self-improved base agent **exceeded its own teacher** on the same questions — because
5 of the 10 injected examples are execution-verified gold SQL that the teacher itself couldn't
produce, scaffolding the weaker model beyond the stronger one's unaided performance.

In one unattended run the detector fired automatically at the change-point
(`severity=0.295`, windowed accuracy `0.48` vs `0.775` baseline), correction synthesized 10
few-shot examples (3 teacher-verified, 5 gold-fallback, 2 anti-forgetting anchors), and the
agent recovered. The held-out questions are drawn from a pool disjoint from the examples'
source, so this is out-of-sample generalization — not memorization.

---

## The idea: a feedback spine

The agent's state is one `AgentConfig`, and the only thing that changes about it is a list:

```python
class AgentConfig(BaseModel):
    config_id: str
    model: str
    few_shot_examples: list[FewShotExample] = []   # starts EMPTY, grows via correction
```

`few_shot_examples` **starts empty**. On drift, correction appends learned examples; the
harness reads them on the next runs. Recovery happens because the agent *learned* — nothing
is reverted, no bigger model is swapped in. **That growing list is the self-improvement.**

---

## Architecture

Four typed stages, connected only through frozen [Pydantic contracts](contracts/schemas.py)
and a single append-only event log:

```
  Harness ──TelemetryRecord──▶ Detector ──DriftEvent──▶ Correction
     ▲                                                       │
     └────────── learned few-shot examples (feedback) ───────┘
                  every stage ──▶ events.jsonl ──▶ Viewer
```

| Stage | Consumes | Emits | What it does |
|-------|----------|-------|--------------|
| **[Harness](harness/)** | Spider data + `AgentConfig` | `TelemetryRecord` | Runs the agent over a change-point query stream; execution-based eval |
| **[Detector](detector/)** | `TelemetryRecord` | `DriftEvent` | Windowed statistical drift detection; classifies the failure mode |
| **[Correction](correction/)** | `DriftEvent` + failing cases | `CorrectionAction` | Teacher generates corrected SQL → verified → few-shot examples |
| **[Viewer](viewer/)** | `events.jsonl` | (web UI) | Thin live view: recovery curve + SQL example panel |

The seams are the only coordination points; each stage is built and tested in isolation
against mock fixtures, then connected by [`orchestrator.py`](orchestrator.py).

---

## How the loop works

**1. The feed is a change-point on a stratified stream** ([`harness/feed.py`](harness/feed.py)).

- *Baseline* — easy Spider questions, base agent → stable high accuracy (~0.78).
- *Change-point* — the input distribution shifts to **hard / extra-hard** questions
  (models real-world complexity creep).
- *Degraded* — hard questions, base agent → accuracy settles lower. **The transition is the drift.**
- *Recovery* — same hard distribution, now with learned examples → accuracy climbs.

Hard/extra questions are split **by database** into a disjoint **LEARN** pool (the only source
of few-shot examples) and a **HELD-OUT** pool (the benchmark). The agent can never regurgitate
an example it was handed — recovery is an out-of-sample generalization claim.

**2. The detector finds drift over a window, never on one query** ([`detector/detector.py`](detector/detector.py)).

A single bad query is noise; sustained degradation is drift. The detector is a state machine —
`WARMUP` (freeze a baseline) → `NORMAL` (rolling window) → `DRIFTING` (latched). It fires only
when the windowed mean stays a threshold below baseline for several consecutive records, then
emits a `DriftEvent` carrying the severity, the windowed-vs-baseline means, the dominant
**failure mode** (`valid_but_wrong` vs `invalid_sql`), and the specific `failing_run_ids` to
learn from. Transient API outages are filtered out so they can't fire false drift.

**3. Correction makes the agent learn from its failures** ([`correction/learner.py`](correction/learner.py)).

For each failing case: a stronger **teacher model** generates corrected SQL, which is then
**execution-verified against the gold query** — if the teacher's SQL doesn't produce the right
result set, it's discarded and the gold SQL is used instead. A few **anti-forgetting anchors**
(easy successes) are kept so learning hard queries doesn't regress easy ones. The result is a
`CorrectionAction` whose examples are injected back into `few_shot_examples`.

---

## Quickstart

```bash
# 1. Install (Python ≥ 3.10)
pip install -e .          # or: pip install -r requirements.txt

# 2. Generate the mock fixtures (lets any stage run standalone)
python fixtures/generate_mocks.py

# 3. Set the model API key (base + teacher both use the MiniMax OpenAI-compatible API)
export MINIMAX_API_KEY=sk-...
```

The Spider subset and SQLite databases are already checked in under
[`fixtures/`](fixtures/). To rebuild them from a full Spider download, see
[`fixtures/prepare_spider.py`](fixtures/prepare_spider.py).

**Models** (override via env): base agent `MiniMax-M2.7-highspeed`, teacher
`MiniMax-M3` (`TEACHER_MODEL`). The base tier is deliberately weak so it genuinely
struggles on hard SQL.

---

## Running the loop

```bash
# Full end-to-end demo: baseline → change-point → drift → correction → recovery
python orchestrator.py --full --fresh

# Cheap validation gates (run these before a full run — they save API calls):
python orchestrator.py --probe              # ~22 calls: do schema-relevant examples help?
python orchestrator.py --dry-run-heldout    # base accuracy on held-out (confirms headroom)
python orchestrator.py --dry-run-degraded   # confirms the detector will fire
```

Each stage also runs standalone:

```bash
python -m detector.detector --input fixtures/mock_telemetry.jsonl
python -m correction.correction --drift fixtures/mock_drift_events.jsonl
python -m harness.runner --full
uvicorn viewer.app:app --port 8011          # then open http://127.0.0.1:8011
```

The viewer reads `fixtures/mock_events.jsonl` by default; point it at a live run with
`VIEWER_LOG=events.jsonl`.

---

## How we measure honestly

The improvement claim is only as good as the measurement. Three guardrails make it defensible:

- **Stratified by difficulty.** Recovery questions are *all* hard, so the *overall* accuracy
  curve looks low by construction — the honest signal is **hard-bucket accuracy WITHOUT vs WITH
  learned examples**, comparing the *same* questions at the *same* difficulty.
- **Unique-question accuracy.** The stream samples with replacement; at `temperature=0` the
  model is deterministic, so repeated questions are de-duplicated before averaging. This removes
  windowed-mean noise that otherwise looks like phantom "learning."
- **Out-of-sample by schema.** Examples come only from the LEARN split; the benchmark is the
  disjoint HELD-OUT split, and a same-database filter drops any cross-schema example as noise.

A full run prints the bottom line — windowed drift detection plus the side-by-side comparison:

```
[detector] Drift detected! channel=execution_accuracy, severity=0.295  (window 0.48 vs baseline 0.775)
[correction] CorrectionAction: 10 examples — 3 teacher, 5 gold, 2 anchor

  Self-improvement result (hard bucket, 30 unique held-out questions):
    WITHOUT examples (base)  : 0.300
    WITH examples (recovered): 0.567
    Delta                    : +0.267
  ✓ Agent improved on hard queries after learning from its own failures.
```

---

## Project layout

```
contracts/      FROZEN shared schemas + the events.jsonl read/write helper
harness/        text-to-SQL agent, Spider execution-eval, change-point feed
detector/       windowed drift detection + failure-mode classification
correction/     teacher → verify → anchor → few-shot examples
viewer/         FastAPI + Chart.js live view (server-side windowing; not Streamlit)
fixtures/       Spider subset, SQLite DBs, mock generators
orchestrator.py wires the full live loop
```

## Testing

```bash
pytest        # 212 tests across all stages
```

Tests are hermetic — external model calls and DB lookups are injected, so the full suite runs
offline without an API key.

## Tech stack

Python · Pydantic (typed contracts) · SQLite + Spider EX metric (execution-based eval) ·
MiniMax (OpenAI-compatible API) for base + teacher models · FastAPI + Chart.js (viewer).
Local-only by design; horizontal scaling (stateless, per-channel detectors, Ray fan-out) is
an architecture property, not a build dependency.

## Team

| Stage | Owner |
|-------|-------|
| Harness / telemetry | Rohan |
| Detector | Logan |
| Correction / learning | Mihir |
| Viewer | Yiwen |
</content>
