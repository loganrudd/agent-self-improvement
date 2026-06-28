# PLAN.md — Correction Module (Knowledge-Graph Self-Improvement)

> Build target for Claude Code. Owner: Mihir. Module: everything downstream of Logan's DriftEvent.
> Detection (Logan) and the 5-step Text-to-SQL harness (Rohan/Ian) are FIXED. Do not modify them.
> Stack: Python, networkx + JSON persistence (NOT a graph DB), HuggingFace/API for the repair model.

---

## 0. What this module does (one paragraph)

When the agent produces wrong SQL, a larger reasoning model runs a ReAct loop to repair it.
The repair is distilled into a **correction rule** and written to a knowledge graph, scoped to the
database schema (and promoted to a global rule if it recurs across schemas). On every new run, the
agent first queries the graph for relevant corrections and injects them into context before generating
SQL. The agent improves the more it runs: seen traps stop recurring. No weight updates, no human labels.

**Why it is not basic RAG:** the memory unit is a *distilled correction* (trap → fix), not a past query.
Retrieval is a schema-scoped + intent-triggered graph traversal, not similarity lookup. The store is
written continuously by an automated repair loop, not indexed once.

---

## 1. Inputs / outputs (contracts — freeze these first)

```python
# INPUT from harness on a failed run
class FailedRun(BaseModel):
    run_id: str
    db_id: str
    question: str
    broken_sql: str
    execution_error: str | None     # SQLite error text, if any
    expected_result: list | None    # gold rows from Spider
    observed_result: list | None    # what broken_sql returned
    schema: dict                    # tables -> columns for this db_id

# INPUT from Logan
class DriftEvent(BaseModel):
    channel: str        # eval_score | tool_call_order | step_completion | latency_ms
    severity: float
    db_id: str

# OUTPUT of this module, consumed by the harness on the next run
class CorrectionContext(BaseModel):
    db_id: str
    question: str
    injected_rules: list[str]       # formatted correction lines for the prompt
    rule_ids: list[str]             # for hit-tracking
```

---

## 2. Repo layout (this module only)

```
correction/
├── contracts.py        # the models above — FREEZE first
├── graph.py            # knowledge graph: add_rule, get_rules, promote
├── repair.py           # big-model ReAct repair loop on a FailedRun
├── distill.py          # broken vs fixed SQL -> CorrectionRule
├── inject.py           # read hook: get_rules -> formatted context block
├── store.py            # JSON load/dump of the graph
├── on_drift.py         # seam: DriftEvent + FailedRun -> write rule
└── tests/
    ├── test_graph.py
    ├── test_inject.py
    └── fixtures/
        ├── sample_failed_run.json
        └── seed_rules.json
```

---

## 3. The knowledge graph (graph.py)

Use `networkx.DiGraph`. Persist as JSON via store.py.

### Node types
```
schema:{db_id}:{table}              # table node
schema:{db_id}:{table}.{column}     # column node
rule:{db_id}:{n}                    # per-database correction rule
rule:global:{n}                     # transferable correction rule
```

### CorrectionRule (the memory unit)
```python
class CorrectionRule(BaseModel):
    id: str
    scope: Literal["db", "global"]
    db_id: str | None               # None if global
    trap: str                       # what the agent did wrong
    fix: str                        # the correction
    trigger: str                    # keyword/table cue for retrieval
    applies_to: list[str]           # table/column node ids
    source: Literal["react_repair", "seed"]
    hits: int = 0                   # incremented when it helps
    seen_dbs: list[str] = []        # for promotion logic
```

### API
```python
def add_rule(rule: CorrectionRule) -> None
def get_rules(db_id: str, question: str) -> list[CorrectionRule]
    # return rules where (scope==global OR db_id matches)
    #   AND trigger matches question (keyword/table in question, case-insensitive)
def bump_hit(rule_id: str) -> None
def maybe_promote(rule: CorrectionRule) -> None
    # if same trap+fix seen on >=2 distinct db_ids -> create rule:global clone
```

Keep `get_rules` matching simple: lowercase substring match of `trigger` (and any
`applies_to` table name) against the question. Do not build embeddings tonight.

---

## 4. Repair loop (repair.py)

```python
def repair(failed: FailedRun) -> str:
    """Big reasoning model + ReAct. Returns corrected SQL."""
    # ReAct loop, max 3 iterations:
    #   Thought: read schema + execution_error + observed vs expected
    #   Action: propose SQL
    #   Observation: execute against the SQLite DB, compare to expected_result
    #   stop when result matches expected OR max iters
    # Model: use the strong model (API route — MiniMax/GPT-class) for repair only.
    # Return best SQL found.
```

This is the only place the big model is used. It is the teacher that generates the correct
trajectory. Keep it off the hot path — repair runs only on confirmed failures.

---

## 5. Distillation (distill.py)

```python
def distill(failed: FailedRun, fixed_sql: str) -> CorrectionRule:
    """Diff broken vs fixed SQL, extract a reusable rule."""
    # Ask the model (cheap call) to output JSON ONLY:
    #   { "trap": "...", "fix": "...", "trigger": "...", "applies_to": ["table.col", ...] }
    # given broken_sql, fixed_sql, schema, question.
    # Parse JSON, build CorrectionRule(scope="db", db_id=failed.db_id, source="react_repair").
```

Prompt must say: output JSON only, no prose, no markdown fences. Parse defensively.

---

## 6. The seam (on_drift.py) — Logan integration

```python
def on_drift_event(event: DriftEvent, failed: FailedRun) -> CorrectionRule | None:
    if event.severity < SEVERITY_THRESHOLD:        # gate noise
        return None
    fixed_sql = repair(failed)                      # big model fixes it
    rule = distill(failed, fixed_sql)               # extract rule
    rule.seen_dbs = [failed.db_id]
    add_rule(rule)
    maybe_promote(rule)                             # cross-db -> global
    return rule
```

- `eval_score` drift  -> semantic rule (wrong column/table/join)
- `tool_call_order` / `step_completion` drift -> procedural rule (order/skip)
- Channel sets a `category` tag on the rule; severity gates whether to write.

---

## 7. Read hook (inject.py) — runs on EVERY new run, before write_sql()

```python
def build_context(db_id: str, question: str) -> CorrectionContext:
    rules = get_rules(db_id, question)
    lines = [f"- {r.fix} (avoid: {r.trap})" for r in rules]
    for r in rules: bump_hit(r.id)
    return CorrectionContext(db_id=db_id, question=question,
                             injected_rules=lines,
                             rule_ids=[r.id for r in rules])
```

Injected block format in the prompt:
```
Known corrections for this schema:
- Use department.name via join on dept_id (avoid: column dept_name)
- Table names are singular in this DB (avoid: pluralizing)
```

Read always runs; write only on confirmed drift. Single failures are noise; Logan's
windowed detection is the quality filter on what gets committed to the graph.

---

## 8. Build order (strict — validate the trick before polishing)

```
STEP 0  contracts.py — freeze the 4 models above.                         [30 min]

STEP 1  PROVE THE PITCH. graph.py + store.py + inject.py.                  [90 min]
        Hand-write ONE rule into seed_rules.json.
        Show inject.py changes the agent's prompt on a matching question.
        Show it fires on a SECOND db_id (transfer) when scope=global.
        >>> If transfer works, the pitch stands. If not, fall back to db-only.

STEP 2  repair.py — big-model ReAct repair on sample_failed_run.json.     [90 min]
        Confirm it turns a broken query into a passing one.

STEP 3  distill.py — broken+fixed -> CorrectionRule JSON.                  [60 min]
        Confirm an auto-generated rule matches the hand-written one's shape.

STEP 4  on_drift.py — wire DriftEvent + FailedRun -> add_rule.            [45 min]
        Now failures auto-write rules. Loop is closed.

STEP 5  maybe_promote — cross-db promotion to global.                      [45 min]
        If shaky, seed 1-2 global rules manually; keep promotion best-effort.

STEP 6  Integration with Logan + harness on real outputs.                 [remaining]
        Tune drift so a trap is hit reliably and recovery is visible.
        Record fallback demo video the moment the loop is stable.
```

---

## 9. Demo (3 beats)

```
1. Empty graph. Agent hits the dept_name trap. Logan flags eval_score drift.
   Repair runs, rule written — node appears in the graph view live.
2. Same DB, related question. inject.py fires the rule. Agent avoids the trap. Recovered.
3. NEW db_id. A promoted global rule fires and prevents a failure before it happens.
   "It learned a lesson on one schema and applied it to one it had never seen."
```

Beat 3 is the win. Protect it above all polish.

---

## 10. Scope discipline / non-goals

- networkx + JSON only. No Neo4j, no vector DB, no embeddings. Backend is a pitch line.
- No live fine-tuning. No weight updates anywhere. This is system-level adaptation.
- `get_rules` matching is substring/keyword only tonight.
- If promotion (Step 5) is unstable: seed global rules, demo still lands.
- Honest limitation to state in Q&A: must fail a pattern once to learn it; routes around
  seen error classes, does not reason about novel ones. That is the flywheel as designed.

---

## 11. Q&A answers (rehearse)

- **"Isn't this RAG?"** Memory unit is a distilled trap→fix correction, not a past query.
  Retrieval is schema-scoped + intent-triggered graph traversal. Written continuously by an
  automated repair loop. Basic RAG retrieves documents; this retrieves learned corrections.
- **"What's novel?"** Two-tier error memory — per-schema traps plus transferable global rules
  that fire on unseen databases. A failure on one schema improves another.
- **"Does the model improve or the prompt?"** The system improves via structured memory; weights
  are untouched. Explicitly system-level continual adaptation. No training, no labels, unbounded with use.
- **"How does it use the drift signal?"** Logan's channel sets the rule type; severity gates writes.
  Windowed detection means we only commit confirmed patterns, not one-off noise.
```

First move: STEP 0, freeze contracts.py. Then STEP 1 to prove transfer before building anything else.