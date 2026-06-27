"""Text-to-SQL agent (Gemini, weaker tier as base).

KEY: the prompt MUST include config.few_shot_examples — that growing list is how the
agent recovers after correction feeds it learned examples.

- generate_sql(question, schema_text, config) -> (sql, tokens, latency_ms)
"""
from __future__ import annotations

import os
import re
import time

from google import genai
from google.genai import types as genai_types

from contracts.schemas import AgentConfig, FewShotExample

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    return _client


_SYSTEM = (
    "You are a SQL expert. Given a database schema and a question, "
    "write a single valid SQL SELECT statement. "
    "Return ONLY the SQL, no markdown fences, no explanation."
)


def _build_prompt(question: str, schema: str, examples: list[FewShotExample]) -> str:
    parts = [f"Schema:\n{schema}"]
    if examples:
        shots = "\n\n".join(f"Q: {e.question}\nSQL: {e.correct_sql}" for e in examples[:8])
        parts.append(f"Few-shot examples:\n{shots}")
    parts.append(f"Question: {question}\nSQL:")
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def generate_sql(
    question: str,
    schema: str,
    config: AgentConfig,
) -> tuple[str, int, float]:
    """Returns (sql, tokens, latency_ms). sql may be an error comment on failure."""
    t0 = time.time()
    try:
        client = _get_client()
        prompt = _build_prompt(question, schema, config.few_shot_examples)
        response = client.models.generate_content(
            model=config.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                temperature=0.0,
            ),
        )
        sql = _strip_fences(response.text)
        tokens = response.usage_metadata.total_token_count or 0 if response.usage_metadata else 0
    except Exception as e:
        sql = f"-- error: {e}"
        tokens = 0
    latency_ms = (time.time() - t0) * 1000
    return sql, tokens, latency_ms
