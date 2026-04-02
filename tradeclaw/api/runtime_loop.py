from __future__ import annotations

import threading
import time


class RuntimeTickLoop:
    """Background loop that advances running instances at a fixed cadence."""

    def __init__(self, service, approval_gate, interval_seconds: float = 5.0):
        self.service = service
        self.approval_gate = approval_gate
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tradeclaw-runtime-loop", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds * 2)

    def _run(self):
        while not self._stop.is_set():
            self.service.tick_once()
            if hasattr(self.approval_gate, "expire_pending"):
                self.approval_gate.expire_pending()
            self._stop.wait(self.interval_seconds)
