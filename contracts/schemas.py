"""
FROZEN CONTRACT — do not edit without team agreement (see .claude/rules/01-contracts.md).

These Pydantic models are the single shared dependency of all four stages.
Import them; never redefine a record shape locally.

    from contracts.schemas import TelemetryRecord, DriftEvent, CorrectionAction, AgentConfig
"""
from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class Difficulty(str, Enum):
    """Spider difficulty buckets. On every TelemetryRecord so accuracy can be
    stratified by difficulty — the contamination-free improvement signal."""
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXTRA = "extra"


class FailureMode(str, Enum):
    """How a failing run failed — set by the detector from the validity channel,
    consumed by correction to decide WHAT to learn."""
    VALID_BUT_WRONG = "valid_but_wrong"  # SQL runs, wrong result -> needs logic/join examples
    INVALID_SQL = "invalid_sql"          # SQL won't parse/run -> needs structural examples
    NONE = "none"


class FewShotExample(BaseModel):
    """A (question, correct_sql) pair the agent learns from. Produced by the teacher
    model in the correction stage, injected into AgentConfig.few_shot_examples."""
    question: str
    correct_sql: str
    db_id: str = ""          # which Spider schema this targets
    source: str = "teacher"  # provenance: "teacher" | "gold" | etc.


class AgentConfig(BaseModel):
    """The agent's current state. THE FEEDBACK SPINE: few_shot_examples starts empty
    and grows as correction appends learned examples. That growth IS the learning."""
    config_id: str
    model: str                                   # the model the agent runs (base = weaker tier)
    prompt_version: str = "v1"
    few_shot_examples: list[FewShotExample] = Field(default_factory=list)


class TelemetryRecord(BaseModel):
    """ONE agent run. Rohan (harness) emits -> Logan (detector) consumes.
    Carries all channels plus question/SQL for the viewer's example panel."""
    run_id: str
    timestamp: float
    difficulty: Difficulty

    # --- Tier 1: detection + improvement ---
    execution_accuracy: float = Field(..., ge=0.0, le=1.0)  # 1.0 if result set matches gold

    # --- Tier 1: diagnostic ---
    query_valid: bool                                        # did generated SQL parse + execute

    # --- Tier 2: diagnostic, label-free ---
    generated_complexity: int = 0                            # joins+nesting in generated SQL
    required_complexity: int = 0                             # joins+nesting in gold SQL

    # --- Tier 3: operational ---
    latency_ms: float = 0.0
    tokens: int = 0

    # --- for the viewer's example panel ---
    question: str = ""
    generated_sql: str = ""
    db_id: str = ""
    config_id: str = ""                                      # which AgentConfig produced this run
    reasoning: str = ""                                      # raw <think> block (MiniMax M-series), empty for non-reasoning models

    @property
    def complexity_gap(self) -> int:
        """Positive = agent under-reached (generated too-simple SQL for a hard question)."""
        return self.required_complexity - self.generated_complexity


class DriftEvent(BaseModel):
    """Logan (detector) emits -> Mihir (correction) consumes.
    Fires when a WINDOWED channel aggregate crosses threshold (never on one query)."""
    detected_at: float
    channel: str                                  # e.g. "execution_accuracy"
    severity: float                               # how far past threshold (>=0)
    window_mean: float                            # the windowed aggregate that crossed
    baseline_mean: float
    failure_mode: FailureMode = FailureMode.NONE  # dominant failure kind in the window
    # the specific runs to learn from (run_ids from the degraded window)
    failing_run_ids: list[str] = Field(default_factory=list)


class CorrectionAction(BaseModel):
    """Mihir (correction) emits -> back to Rohan's harness (appended to few_shot_examples).
    The agent uses these on subsequent runs and recovers by LEARNING, not reverting."""
    triggered_by: str                                          # DriftEvent channel/id
    new_few_shot_examples: list[FewShotExample] = Field(default_factory=list)
    rationale: str = ""
