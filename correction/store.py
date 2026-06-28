"""JSON persistence for the correction knowledge graph."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("correction/graph_store.json")


def load(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"rules": []}
    return json.loads(p.read_text())


def save(data: dict[str, Any], path: Path | str = DEFAULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
