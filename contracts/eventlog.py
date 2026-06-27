"""
Shared event-log helper — the ONE way to read/write events.jsonl.
All four stages use this; do not hand-roll a JSON line format (see rules/00-architecture.md).

Each line is a typed envelope:
    {"type": "telemetry"|"drift"|"correction", "ts": <float>, "data": {...}}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel

from .schemas import TelemetryRecord, DriftEvent, CorrectionAction

DEFAULT_LOG = Path("events.jsonl")

EventType = Literal["telemetry", "drift", "correction"]
_TYPE_BY_CLASS = {
    TelemetryRecord: "telemetry",
    DriftEvent: "drift",
    CorrectionAction: "correction",
}
_CLASS_BY_TYPE = {v: k for k, v in _TYPE_BY_CLASS.items()}


class Event(BaseModel):
    type: EventType
    ts: float
    data: dict


def append_event(record: BaseModel, path: Path | str = DEFAULT_LOG) -> None:
    """Append any contract record (Telemetry/Drift/Correction) to the log."""
    etype = _TYPE_BY_CLASS.get(type(record))
    if etype is None:
        raise TypeError(f"Unknown record type for event log: {type(record)!r}")
    env = Event(type=etype, ts=time.time(), data=record.model_dump(mode="json"))
    with open(path, "a") as f:
        f.write(env.model_dump_json() + "\n")


def read_events(
    path: Path | str = DEFAULT_LOG,
    only: EventType | None = None,
) -> list[BaseModel]:
    """Read all events, optionally filtered by type. Returns parsed contract records."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[BaseModel] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        env = Event.model_validate_json(line)
        if only and env.type != only:
            continue
        out.append(_CLASS_BY_TYPE[env.type].model_validate(env.data))
    return out


def tail_events(
    path: Path | str = DEFAULT_LOG,
    poll_seconds: float = 0.5,
) -> Iterator[BaseModel]:
    """Block and yield new events as they're appended (for the viewer)."""
    p = Path(path)
    pos = 0
    while True:
        if p.exists():
            with open(p) as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if line:
                        env = Event.model_validate_json(line)
                        yield _CLASS_BY_TYPE[env.type].model_validate(env.data)
                pos = f.tell()
        time.sleep(poll_seconds)
