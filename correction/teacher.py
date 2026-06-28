"""Teacher model generates corrected SQL for failing questions (stronger MiniMax tier).

The teacher is used to PRODUCE training examples, not as a permanent model swap.
Verification and gold fallback happen in the learner — teacher quality matters for
yield (how often it beats gold), not for correctness (the learner always verifies).

Models:
  default teacher  -> MiniMax-M3  (or TEACHER_MODEL env var)
  base agent       -> MiniMax-M2.7-highspeed  (harness/agent.py)
"""
from __future__ import annotations

import os
import re

from openai import OpenAI

_MINIMAX_BASE_URL = "https://api.minimax.io/v1"
_DEFAULT_TEACHER_MODEL = "MiniMax-M3"

_client: OpenAI | None = None

_SYSTEM = (
    "You are a SQL expert. Given a database schema and a natural language question, "
    "write a single valid SQL SELECT statement that correctly answers the question. "
    "Return ONLY the SQL — no markdown fences, no explanation, no commentary."
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("MINIMAX_API_KEY")
        if not key:
            raise RuntimeError(
                "MINIMAX_API_KEY is not set. Export it before running correction."
            )
        _client = OpenAI(api_key=key, base_url=_MINIMAX_BASE_URL)
    return _client


def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (MiniMax M-series)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
    return text


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def generate_sql(question: str, schema_text: str, model: str | None = None) -> str:
    """Call the teacher model to generate corrected SQL for a failing question.

    Args:
        question:    Natural language question.
        schema_text: DB schema string (from harness.spider.schema_text).
        model:       Override the teacher model; falls back to TEACHER_MODEL env var,
                     then to MiniMax-M3.

    Returns:
        Raw SQL string with fences and think-blocks stripped.
    """
    resolved_model = model or os.environ.get("TEACHER_MODEL", _DEFAULT_TEACHER_MODEL)
    prompt = f"Schema:\n{schema_text}\n\nQuestion: {question}\nSQL:"
    client = _get_client()
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    raw = response.choices[0].message.content or ""
    return _strip_fences(_strip_think(raw))
