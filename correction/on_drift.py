"""Seam: DriftEvent + FailedRun -> write correction rule to the knowledge graph.

Called by the orchestrator (or directly in tests) when Logan fires a DriftEvent.
Severity gate keeps single-query noise from polluting the graph.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from contracts.schemas import DriftEvent
from .contracts import FailedRun, CorrectionRule
from .repair import repair
from .distill import distill
from .graph import add_rule, maybe_promote

SEVERITY_THRESHOLD = 0.2


def on_drift_event(
    event: DriftEvent,
    failed: FailedRun,
    db_path: Optional[Path] = None,
) -> Optional[CorrectionRule]:
    """Process one DriftEvent + one FailedRun.

    Returns the written rule, or None if severity is below threshold.
    """
    if event.severity < SEVERITY_THRESHOLD:
        return None

    fixed_sql = repair(failed, db_path=db_path)
    rule = distill(failed, fixed_sql)
    rule.seen_dbs = [failed.db_id]

    add_rule(rule)
    maybe_promote(rule)

    return rule
