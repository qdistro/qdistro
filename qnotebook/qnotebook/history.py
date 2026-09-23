"""Navigation history — back/forward stack of visited page paths (strings)."""

from __future__ import annotations

from collections import deque


class NavigationHistory:
    """Minimal browser-style back/forward history over page-path strings."""

    def __init__(self, limit: int = 100) -> None:
        self._back: deque[str] = deque(maxlen=limit)
        self._forward: list[str] = []
        self._current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    def push(self, path: str) -> None:
        if self._current is not None and self._current == path:
            return
        if self._current is not None:
            self._back.append(self._current)
        self._current = path
        self._forward.clear()

    def can_go_back(self) -> bool:
        return bool(self._back)

    def can_go_forward(self) -> bool:
        return bool(self._forward)

    def go_back(self) -> str | None:
        if not self._back:
            return None
        if self._current is not None:
            self._forward.append(self._current)
        self._current = self._back.pop()
        return self._current

    def go_forward(self) -> str | None:
        if not self._forward:
            return None
        if self._current is not None:
            self._back.append(self._current)
        self._current = self._forward.pop()
        return self._current
