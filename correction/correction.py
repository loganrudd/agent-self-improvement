"""DriftEvent + failing cases -> CorrectionAction (learned few-shot examples).

TODO(Mihir):
- on DriftEvent: look up failing_run_ids -> the failing (question, gold/schema) cases.
- teacher.correct(...) -> correct SQL; learner.make_examples(...) -> FewShotExamples.
- emit CorrectionAction(new_few_shot_examples=..., rationale=...).
- the harness appends these to AgentConfig.few_shot_examples -> agent recovers by LEARNING.
"""
from contracts.schemas import DriftEvent, CorrectionAction, FewShotExample  # noqa: F401
from contracts.eventlog import append_event, read_events                    # noqa: F401

if __name__ == "__main__":
    raise SystemExit("TODO(Mihir): implement the learn-from-failures correction")
