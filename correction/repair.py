"""ReAct repair loop: big teacher model fixes a broken SQL query.

Uses stronger Gemini tier (TEACHER_MODEL env var, default gemini-1.5-pro).
Only called on confirmed failures — off the hot path.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

from .contracts import FailedRun

try:
    import google.generativeai as genai
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

_TEACHER_MODEL = os.environ.get("TEACHER_MODEL", "gemini-1.5-pro")
_MAX_ITERS = 3


def repair(failed: FailedRun, db_path: Optional[Path] = None) -> str:
    """Run a ReAct loop (max 3 iters) and return the best corrected SQL."""
    _configure_genai()
    best_sql = failed.broken_sql
    history: list[str] = []

    for iteration in range(1, _MAX_ITERS + 1):
        prompt = _build_prompt(failed, iteration, history)
        try:
            sql_candidate = _extract_sql(_call_model(prompt))
        except Exception:
            break

        if not sql_candidate:
            continue

        best_sql = sql_candidate
        if db_path and db_path.exists():
            rows, error = _execute(sql_candidate, db_path)
            if error is None and _match(rows, failed.expected_result):
                return best_sql
            history.append(f"iter{iteration}: {sql_candidate[:80]} -> err={error}, rows={rows[:2]}")
        else:
            # No live DB — accept the first syntactically plausible candidate
            if sql_candidate.strip().upper().startswith("SELECT"):
                return best_sql

    return best_sql


# ── internals ─────────────────────────────────────────────────────────────────

def _configure_genai() -> None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key and _GENAI_AVAILABLE:
        genai.configure(api_key=key)


def _call_model(prompt: str) -> str:
    if not _GENAI_AVAILABLE:
        raise RuntimeError("google-generativeai not installed; set GEMINI_API_KEY")
    model = genai.GenerativeModel(_TEACHER_MODEL)
    return model.generate_content(prompt).text.strip()


def _build_prompt(failed: FailedRun, iteration: int, history: list[str]) -> str:
    prev = ("Previous attempts:\n" + "\n".join(history[-2:]) + "\n\n") if history else ""
    exp = str(failed.expected_result[:3]) if failed.expected_result else "unknown"
    obs = str(failed.observed_result[:3]) if failed.observed_result else "none"
    return f"""You are a SQL expert repairing a broken query (ReAct style).

Schema:
{_schema_text(failed.schema)}

Question: {failed.question}
Broken SQL: {failed.broken_sql}
Execution error: {failed.execution_error or "none"}
Expected result (sample): {exp}
Observed result (sample): {obs}

{prev}Iteration {iteration}/{_MAX_ITERS}. Reply ONLY in this format:
Thought: <one sentence on what went wrong>
SQL: <corrected SQL, single line, no markdown fences>"""


def _extract_sql(response: str) -> str:
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SQL:"):
            return stripped.split(":", 1)[1].strip()
    for line in response.splitlines():
        if line.strip().upper().startswith("SELECT"):
            return line.strip()
    return ""


def _execute(sql: str, db_path: Path) -> tuple[list, str | None]:
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.cursor().execute(sql).fetchall()
        conn.close()
        return rows, None
    except Exception as e:
        return [], str(e)


def _match(observed: list, expected: list | None) -> bool:
    if expected is None:
        return False
    return set(map(tuple, observed)) == set(map(tuple, expected))


def _schema_text(schema: dict) -> str:
    return "\n".join(
        f"  {table}({', '.join(cols) if isinstance(cols, list) else cols})"
        for table, cols in schema.items()
    )
