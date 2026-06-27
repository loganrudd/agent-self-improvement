"""Wires the full live loop. BUILT TOGETHER AT THE INTEGRATION CHECKPOINT (hr 5-6),
not by one person in isolation (see .claude/rules/04-workflow.md).

Until integration, every stage runs standalone against its mock fixture.

Intended flow once stages exist:
    config = AgentConfig(config_id="c0", model=<weak tier>)        # few_shot_examples empty
    detector = Detector(baseline_from=phase1)
    for rec in harness.run_stream(config):       # harness emits TelemetryRecord
        append_event(rec)
        ev = detector.update(rec)                 # -> DriftEvent or None
        if ev:
            append_event(ev)
            action = correction.handle(ev)        # -> CorrectionAction (learned examples)
            append_event(action)
            config.few_shot_examples += action.new_few_shot_examples   # THE FEEDBACK SPINE
    # viewer tails events.jsonl throughout
"""

if __name__ == "__main__":
    raise SystemExit("TODO(team): build at the integration checkpoint")
