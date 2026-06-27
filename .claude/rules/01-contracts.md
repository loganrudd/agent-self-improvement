# LOCKED RULE: Data contracts (schemas are FROZEN)

**`contracts/schemas.py` is frozen as of hour 1.** It is the single shared file every stage depends on.

- **Do NOT edit it without announcing to the whole team first.** A silent change here breaks all four stages and the integration.
- If a field genuinely must change: stop, post in the team channel, get agreement, change it once, everyone re-pulls.
- Treat the Pydantic models as the source of truth. Import them; never redefine a record shape locally.

## The records (see schemas.py for the authoritative definition)
- `AgentConfig` — the agent's current state. `few_shot_examples` starts empty and grows via correction.
- `TelemetryRecord` — one agent run. Carries the channels (accuracy, validity, complexity, latency, tokens), the Spider `difficulty` label, and the question/SQL for the viewer.
- `DriftEvent` — emitted by the detector. Carries which channel drifted, severity, the windowed vs baseline means, and a `failure_mode` tag.
- `CorrectionAction` — emitted by correction. Carries the `new_few_shot_examples` the agent should learn from.

## Why `difficulty` is on every record
Stratifying accuracy by difficulty is what makes the improvement claim defensible ("hard-bucket accuracy went 40%→80%, same difficulty"). Never drop it.

## Event log helper
Use `contracts/eventlog.py` (`append_event`, `read_events`, `tail_events`) to read/write `events.jsonl`. Don't hand-roll JSON line formats.
