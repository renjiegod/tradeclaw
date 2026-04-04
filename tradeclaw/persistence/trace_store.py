from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    run_id: str
    phase: str
    payload: dict
    timestamp: str


class InMemoryTraceStore:
    """Append-only trace store for run replay and audit."""

    def __init__(self):
        self._events: List[TraceEvent] = []
        self._sequence_by_run: Dict[str, int] = {}

    def append(self, run_id: str, phase: str, payload: dict):
        sequence = self._sequence_by_run.get(run_id, 0) + 1
        self._sequence_by_run[run_id] = sequence
        event = TraceEvent(
            sequence=sequence,
            run_id=run_id,
            phase=phase,
            payload=dict(payload),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._events.append(event)
        return event

    def get_run_events(self, run_id: str) -> List[TraceEvent]:
        return [event for event in self._events if event.run_id == run_id]


def _trace_event(record) -> TraceEvent:
    timestamp = record.timestamp
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    return TraceEvent(
        sequence=record.sequence,
        run_id=record.run_id,
        phase=record.phase,
        payload=dict(record.payload),
        timestamp=str(timestamp),
    )


class AsyncTraceStore:
    def __init__(self, repository):
        self.repository = repository

    async def append(self, run_id: str, phase: str, payload: dict):
        record = await self.repository.append_event(run_id=run_id, phase=phase, payload=payload)
        return _trace_event(record)

    async def get_run_events(self, run_id: str):
        records = await self.repository.list_run_events(run_id)
        return [_trace_event(record) for record in records]
