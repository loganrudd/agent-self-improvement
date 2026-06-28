"""Read hook: runs before every SQL generation call.

Queries the knowledge graph for rules relevant to this (db_id, question) pair,
bumps hit counters, and returns a CorrectionContext with formatted prompt lines.
"""
from __future__ import annotations

from .contracts import CorrectionContext
from .graph import get_rules, bump_hit


def build_context(db_id: str, question: str) -> CorrectionContext:
    rules = get_rules(db_id, question)
    lines = [f"- {r.fix} (avoid: {r.trap})" for r in rules]
    for r in rules:
        bump_hit(r.id)
    return CorrectionContext(
        db_id=db_id,
        question=question,
        injected_rules=lines,
        rule_ids=[r.id for r in rules],
    )


def format_prompt_block(ctx: CorrectionContext) -> str:
    """Format the context block to splice into the agent's prompt."""
    if not ctx.injected_rules:
        return ""
    lines = "\n".join(ctx.injected_rules)
    return f"Known corrections for this schema:\n{lines}"
