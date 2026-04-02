from __future__ import annotations


class ReplayClock:
    def __init__(self, timeline):
        if not timeline:
            raise ValueError("timeline cannot be empty")
        self._timeline = list(timeline)
        self._index = 0

    @property
    def current_time(self):
        return self._timeline[self._index]

    def step(self):
        if self._index + 1 >= len(self._timeline):
            return False
        self._index += 1
        return True
