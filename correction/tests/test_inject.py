"""Tests for correction/inject.py — proves the pitch (Steps 1 demo beats)."""
from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import correction.graph as g
from correction.contracts import CorrectionRule


def _reset(tmp_path: Path) -> None:
    g._STORE_PATH = tmp_path / "graph_store.json"
    g._G = nx.DiGraph()
    g._rules = {}
    g._loaded = True


def test_empty_graph_returns_no_rules(tmp_path):
    _reset(tmp_path)
    from correction.inject import build_context

    ctx = build_context("concert_singer", "how many singers are there?")
    assert ctx.injected_rules == []
    assert ctx.rule_ids == []


def test_matching_rule_injected(tmp_path):
    _reset(tmp_path)
    rule = CorrectionRule(
        id="rule:concert_singer:inject001",
        scope="db",
        db_id="concert_singer",
        trap="Using dept_name instead of country",
        fix="Use singer.country for country-based grouping",
        trigger="country",
        applies_to=["schema:concert_singer:singer"],
        source="seed",
        seen_dbs=["concert_singer"],
    )
    g.add_rule(rule)

    from correction.inject import build_context
    ctx = build_context("concert_singer", "how many singers per country?")
    assert len(ctx.injected_rules) == 1
    assert "country" in ctx.injected_rules[0].lower()
    assert "rule:concert_singer:inject001" in ctx.rule_ids


def test_hit_counter_bumped(tmp_path):
    _reset(tmp_path)
    rule = CorrectionRule(
        id="rule:concert_singer:inject002",
        scope="db",
        db_id="concert_singer",
        trap="Bad alias",
        fix="Use correct alias t1",
        trigger="alias",
        applies_to=[],
        source="seed",
        seen_dbs=["concert_singer"],
    )
    g.add_rule(rule)

    from correction.inject import build_context
    build_context("concert_singer", "select alias from singer")
    assert g._rules["rule:concert_singer:inject002"].hits == 1


def test_format_prompt_block_non_empty(tmp_path):
    _reset(tmp_path)
    rule = CorrectionRule(
        id="rule:concert_singer:inject003",
        scope="db",
        db_id="concert_singer",
        trap="Missing WHERE clause",
        fix="Add WHERE to filter by year",
        trigger="year",
        applies_to=[],
        source="seed",
        seen_dbs=["concert_singer"],
    )
    g.add_rule(rule)

    from correction.inject import build_context, format_prompt_block
    ctx = build_context("concert_singer", "concerts in a specific year")
    block = format_prompt_block(ctx)
    assert block.startswith("Known corrections for this schema:")
    assert "WHERE" in block


def test_format_prompt_block_empty(tmp_path):
    _reset(tmp_path)
    from correction.inject import build_context, format_prompt_block

    ctx = build_context("concert_singer", "totally unrelated question")
    assert format_prompt_block(ctx) == ""


def test_beat3_global_rule_fires_on_unseen_db(tmp_path):
    """Demo beat 3: a promoted global rule prevents a failure on a brand-new db_id."""
    _reset(tmp_path)
    global_rule = CorrectionRule(
        id="rule:global:beat3",
        scope="global",
        db_id=None,
        trap="Pluralizing Spider table names",
        fix="Spider schema uses singular table names (singer not singers)",
        trigger="singers",
        applies_to=[],
        source="seed",
        seen_dbs=["concert_singer", "world_1"],
    )
    g.add_rule(global_rule)

    from correction.inject import build_context
    # student_network is a db_id the graph has never explicitly seen
    ctx = build_context("student_network", "how many singers performed this semester?")
    assert len(ctx.injected_rules) == 1
    assert "singular" in ctx.injected_rules[0].lower()
