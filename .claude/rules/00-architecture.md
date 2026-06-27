# LOCKED RULE: Architecture & data flow

**Do not change without team agreement.** This defines how the four stages connect.

## The loop
```
 HARNESS --TelemetryRecord--> DETECTOR --DriftEvent--> CORRECTION
    ^                                                       |
    |   AgentConfig.few_shot_examples (feedback)            |
    +-------------------------------------------------------+
         every stage appends a typed Event to events.jsonl --> VIEWER
```

## The three seams (the ONLY coordination points)
1. **Harness → Detector:** a stream of `TelemetryRecord` (one per agent run).
2. **Detector → Correction:** a `DriftEvent` when the windowed signal crosses threshold, plus the failing cases to learn from.
3. **Correction → Harness:** a `CorrectionAction` carrying `new_few_shot_examples`, which get appended to `AgentConfig.few_shot_examples`; the harness uses them on subsequent runs.

## The feedback spine (non-negotiable concept)
`AgentConfig.few_shot_examples` **starts empty**. On drift, correction appends learned examples. The harness reads them for the next runs. Recovery happens because the agent **learned**, not because anything was reverted. This growing list *is* "self-improvement" — keep it central.

## Event log
All stages write typed events to a single `events.jsonl` via `contracts/eventlog.py`. The viewer tails it. One append-only log, typed envelopes (`telemetry` | `drift` | `correction`). Do not invent a second log format.

## Orchestrator
`orchestrator.py` wires the full live loop and is built **together at the integration checkpoint** — not by one person in isolation. Until then, every stage runs standalone against mocks.
