from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
            timestamp=datetime.utcnow().isoformat(),
        )
        self._events.append(event)
        return event

    def get_run_events(self, run_id: str) -> List[TraceEvent]:
        return [event for event in self._events if event.run_id == run_id]
