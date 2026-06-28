"""Tests for correction/teacher.py.

All tests are hermetic — no real API calls. The OpenAI client is monkeypatched.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from correction.teacher import _DEFAULT_TEACHER_MODEL, generate_sql


def _fake_response(content: str) -> SimpleNamespace:
    usage = SimpleNamespace(total_tokens=10)
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice], usage=usage)


class TestGenerateSql:
    def test_returns_plain_sql(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response(
            "SELECT COUNT(*) FROM singer"
        )
        with patch("correction.teacher._get_client", return_value=mock_client):
            sql = generate_sql("How many singers?", "Table singer(id INT, name TEXT)")
        assert sql == "SELECT COUNT(*) FROM singer"

    def test_strips_markdown_fences(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response(
            "```sql\nSELECT id FROM singer\n```"
        )
        with patch("correction.teacher._get_client", return_value=mock_client):
            sql = generate_sql("List singer ids", "Table singer(id INT)")
        assert sql == "SELECT id FROM singer"

    def test_strips_think_blocks(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response(
            "<think>reasoning here</think>\nSELECT name FROM singer"
        )
        with patch("correction.teacher._get_client", return_value=mock_client):
            sql = generate_sql("List names", "Table singer(name TEXT)")
        assert sql == "SELECT name FROM singer"

    def test_strips_unclosed_think_block(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response(
            "<think>reasoning cut off mid"
        )
        with patch("correction.teacher._get_client", return_value=mock_client):
            sql = generate_sql("Any question", "Table t(x INT)")
        assert sql == ""

    def test_uses_teacher_model_env_var(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response("SELECT 1")
        with patch("correction.teacher._get_client", return_value=mock_client):
            with patch.dict(os.environ, {"TEACHER_MODEL": "MiniMax-M3-custom"}):
                generate_sql("q", "schema")
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "MiniMax-M3-custom"

    def test_explicit_model_overrides_env(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response("SELECT 1")
        with patch("correction.teacher._get_client", return_value=mock_client):
            with patch.dict(os.environ, {"TEACHER_MODEL": "should-be-ignored"}):
                generate_sql("q", "schema", model="explicit-override")
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "explicit-override"

    def test_default_model_when_no_env(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response("SELECT 1")
        env = {k: v for k, v in os.environ.items() if k != "TEACHER_MODEL"}
        with patch("correction.teacher._get_client", return_value=mock_client):
            with patch.dict(os.environ, env, clear=True):
                generate_sql("q", "schema")
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == _DEFAULT_TEACHER_MODEL

    def test_schema_and_question_appear_in_prompt(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_response("SELECT 1")
        with patch("correction.teacher._get_client", return_value=mock_client):
            generate_sql("How many rows?", "Table foo(id INT)")
        call_kwargs = mock_client.chat.completions.create.call_args
        messages = call_kwargs.kwargs["messages"]
        user_content = next(m["content"] for m in messages if m["role"] == "user")
        assert "How many rows?" in user_content
        assert "Table foo(id INT)" in user_content
