"""Tests for correction/graph.py — no model calls, no network."""
from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import correction.graph as g
from correction.contracts import CorrectionRule


def _reset(tmp_path: Path) -> None:
    """Reset module-level state between tests."""
    g._STORE_PATH = tmp_path / "graph_store.json"
    g._G = nx.DiGraph()
    g._rules = {}
    g._loaded = True


def _rule(id, scope, db_id, trap, fix, trigger, seen_dbs=None, applies_to=None):
    return CorrectionRule(
        id=id,
        scope=scope,
        db_id=db_id,
        trap=trap,
        fix=fix,
        trigger=trigger,
        applies_to=applies_to or ([f"schema:{db_id}:singer"] if db_id else []),
        source="seed",
        seen_dbs=seen_dbs or ([db_id] if db_id else []),
    )


def test_add_and_get_db_scoped_rule(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:concert_singer:001", "db", "concert_singer",
                 "Using wrong column dept_name", "Use singer.country", "country")
    g.add_rule(rule)

    results = g.get_rules("concert_singer", "how many singers from each country?")
    assert len(results) == 1
    assert results[0].id == rule.id


def test_db_rule_does_not_fire_on_different_db(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:concert_singer:002", "db", "concert_singer",
                 "Missing join", "Add join to singer", "singer")
    g.add_rule(rule)

    results = g.get_rules("world_1", "list all singer names")
    assert results == []


def test_global_rule_fires_on_any_db(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:global:001", "global", None,
                 "Pluralizing table names", "Use singular names", "singers",
                 seen_dbs=[], applies_to=[])
    rule.db_id = None
    g.add_rule(rule)

    results = g.get_rules("student_network", "how many singers performed?")
    assert any(r.id == "rule:global:001" for r in results)


def test_bump_hit_increments(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:concert_singer:003", "db", "concert_singer",
                 "Wrong alias", "Use correct alias", "alias")
    g.add_rule(rule)

    g.bump_hit("rule:concert_singer:003")
    g.bump_hit("rule:concert_singer:003")
    assert g._rules["rule:concert_singer:003"].hits == 2


def test_maybe_promote_creates_global_rule(tmp_path):
    _reset(tmp_path)
    rule1 = _rule("rule:concert_singer:004", "db", "concert_singer",
                  "Missing GROUP BY", "Add GROUP BY clause", "group",
                  seen_dbs=["concert_singer"])
    rule2 = _rule("rule:world_1:004", "db", "world_1",
                  "Missing GROUP BY", "Add GROUP BY clause", "group",
                  seen_dbs=["world_1"])
    g.add_rule(rule1)
    g.add_rule(rule2)
    g.maybe_promote(rule2)

    global_rules = [r for r in g._rules.values() if r.scope == "global"]
    assert len(global_rules) >= 1
    assert any("GROUP BY" in r.trap for r in global_rules)


def test_promote_not_duplicated(tmp_path):
    _reset(tmp_path)
    rule1 = _rule("rule:a:005", "db", "db_a",
                  "Same trap", "Same fix", "same", seen_dbs=["db_a"])
    rule2 = _rule("rule:b:005", "db", "db_b",
                  "Same trap", "Same fix", "same", seen_dbs=["db_b"])
    g.add_rule(rule1)
    g.add_rule(rule2)
    g.maybe_promote(rule2)
    g.maybe_promote(rule2)  # second call should not create a second global

    global_rules = [r for r in g._rules.values() if r.scope == "global"
                    and r.trap.strip().lower() == "same trap"]
    assert len(global_rules) == 1


def test_graph_nodes_created(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:concert_singer:006", "db", "concert_singer",
                 "Wrong table", "Use correct table", "table",
                 applies_to=["schema:concert_singer:singer"])
    g.add_rule(rule)

    graph = g.get_graph()
    assert "rule:concert_singer:006" in graph.nodes
    assert "schema:concert_singer:singer" in graph.nodes
    assert graph.has_edge("rule:concert_singer:006", "schema:concert_singer:singer")


def test_persistence_roundtrip(tmp_path):
    _reset(tmp_path)
    rule = _rule("rule:concert_singer:007", "db", "concert_singer",
                 "Bad join", "Use LEFT JOIN", "join")
    g.add_rule(rule)

    # Reset and reload from disk
    g._loaded = False
    results = g.get_rules("concert_singer", "join singers and concerts")
    assert any(r.id == "rule:concert_singer:007" for r in results)
