"""Knowledge graph: rules connected to schema nodes.

Node types:
  schema:{db_id}:{table}         table node
  schema:{db_id}:{table}.{col}   column node
  rule:{db_id}:{n}               per-database correction rule
  rule:global:{n}                transferable global rule

The graph persists as JSON via store.py. Module-level state is lazy-loaded
on first use and reset via reload().
"""
from __future__ import annotations

import uuid
from pathlib import Path

import networkx as nx

from .contracts import CorrectionRule
from . import store as _store

_STORE_PATH = Path("correction/graph_store.json")
_G: nx.DiGraph = nx.DiGraph()
_rules: dict[str, CorrectionRule] = {}
_loaded: bool = False


def _load() -> None:
    global _G, _rules, _loaded
    data = _store.load(_STORE_PATH)
    _G = nx.DiGraph()
    _rules = {}
    for r in data.get("rules", []):
        rule = CorrectionRule.model_validate(r)
        _rules[rule.id] = rule
        _G.add_node(rule.id, kind="rule", scope=rule.scope)
        for node_id in rule.applies_to:
            _G.add_node(node_id, kind="schema")
            _G.add_edge(rule.id, node_id)
    _loaded = True


def _ensure_loaded() -> None:
    if not _loaded:
        _load()


def _persist() -> None:
    _store.save({"rules": [r.model_dump() for r in _rules.values()]}, _STORE_PATH)


# ── public API ────────────────────────────────────────────────────────────────

def add_rule(rule: CorrectionRule) -> None:
    _ensure_loaded()
    if rule.id in _rules:
        return
    _rules[rule.id] = rule
    _G.add_node(rule.id, kind="rule", scope=rule.scope)
    for node_id in rule.applies_to:
        _G.add_node(node_id, kind="schema")
        _G.add_edge(rule.id, node_id)
    _persist()


def get_rules(db_id: str, question: str) -> list[CorrectionRule]:
    """Return rules whose scope matches and whose trigger fires on the question."""
    _ensure_loaded()
    q_lower = question.lower()
    matches: list[CorrectionRule] = []
    for rule in _rules.values():
        if rule.scope != "global" and rule.db_id != db_id:
            continue
        trigger_hit = rule.trigger.lower() in q_lower
        schema_hit = any(
            # strip node-id prefix to get the bare table/column name
            _bare(a) in q_lower
            for a in rule.applies_to
        )
        if trigger_hit or schema_hit:
            matches.append(rule)
    return matches


def bump_hit(rule_id: str) -> None:
    _ensure_loaded()
    if rule_id in _rules:
        _rules[rule_id].hits += 1
        _persist()


def maybe_promote(rule: CorrectionRule) -> None:
    """If the same trap+fix appears on >=2 distinct db_ids, create a global clone."""
    _ensure_loaded()
    if rule.scope == "global":
        return

    norm = (rule.trap.strip().lower(), rule.fix.strip().lower())

    # Already have a global rule with the same trap+fix?
    if any(r.scope == "global" and (r.trap.strip().lower(), r.fix.strip().lower()) == norm
           for r in _rules.values()):
        return

    # Collect all seen_dbs across db-scoped rules with the same trap+fix
    all_dbs: set[str] = set(rule.seen_dbs)
    for existing in _rules.values():
        if existing.id == rule.id or existing.scope == "global":
            continue
        if (existing.trap.strip().lower(), existing.fix.strip().lower()) == norm:
            all_dbs.update(existing.seen_dbs)
            existing.seen_dbs = list(all_dbs)
            _rules[existing.id] = existing

    if len(all_dbs) >= 2:
        global_id = f"rule:global:{uuid.uuid4().hex[:8]}"
        global_rule = CorrectionRule(
            id=global_id,
            scope="global",
            db_id=None,
            trap=rule.trap,
            fix=rule.fix,
            trigger=rule.trigger,
            applies_to=rule.applies_to,
            source=rule.source,
            seen_dbs=list(all_dbs),
        )
        _rules[global_id] = global_rule
        _G.add_node(global_id, kind="rule", scope="global")
        _persist()


def get_graph() -> nx.DiGraph:
    _ensure_loaded()
    return _G


def reload() -> None:
    global _loaded
    _loaded = False
    _load()


# ── helpers ───────────────────────────────────────────────────────────────────

def _bare(node_id: str) -> str:
    """schema:concert_singer:singer -> singer; schema:x:t.col -> col"""
    parts = node_id.split(":")
    last = parts[-1]
    return last.split(".")[-1].lower()
