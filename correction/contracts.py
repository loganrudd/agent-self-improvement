"""Local contracts for the correction module.

FailedRun, CorrectionRule, CorrectionContext are internal to this stage.
The global DriftEvent and CorrectionAction live in contracts/schemas.py (frozen).
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FailedRun(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    run_id: str
    db_id: str
    question: str
    broken_sql: str
    execution_error: str | None = None
    expected_result: list | None = None
    observed_result: list | None = None
    schema: dict = Field(default_factory=dict)  # {table: [col, ...]}


class CorrectionRule(BaseModel):
    id: str
    scope: Literal["db", "global"]
    db_id: str | None = None          # None when scope == "global"
    trap: str                          # what the agent did wrong
    fix: str                           # the correction
    trigger: str                       # keyword/table cue for retrieval
    applies_to: list[str] = Field(default_factory=list)  # schema node ids
    source: Literal["react_repair", "seed"]
    hits: int = 0
    seen_dbs: list[str] = Field(default_factory=list)


class CorrectionContext(BaseModel):
    db_id: str
    question: str
    injected_rules: list[str]  # formatted lines for the prompt
    rule_ids: list[str]        # for hit-tracking
