"""Seam: DriftEvent + FailedRun -> write correction rule to the knowledge graph.

Called by the orchestrator (or directly in tests) when Logan fires a DriftEvent.
Severity gate keeps single-query noise from polluting the graph.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from contracts.schemas import DriftEvent
from .contracts import FailedRun, CorrectionRule
from .repair import repair
from .distill import distill
from .graph import add_rule, maybe_promote

log = logging.getLogger(__name__)
SEVERITY_THRESHOLD = 0.2


def on_drift_event(
    event: DriftEvent,
    failed: FailedRun,
    db_path: Optional[Path] = None,
) -> Optional[CorrectionRule]:
    """Process one DriftEvent + one FailedRun.

    Returns the written rule, or None if severity is below threshold or repair failed.
    """
    if event.severity < SEVERITY_THRESHOLD:
        log.info("on_drift: skipping run %s — severity %.2f below threshold %.2f",
                 failed.run_id, event.severity, SEVERITY_THRESHOLD)
        return None

    fixed_sql = repair(failed, db_path=db_path)

    if fixed_sql == failed.broken_sql:
        log.warning("on_drift: repair produced no improvement for run %s — skipping rule write",
                    failed.run_id)
        return None

    rule = distill(failed, fixed_sql)
    rule.seen_dbs = [failed.db_id]

    add_rule(rule)
    maybe_promote(rule)

    log.info("on_drift: wrote rule %s (scope=%s) for run %s", rule.id, rule.scope, failed.run_id)
    return rule
