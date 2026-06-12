"""Per-(uid, action) sliding-window rate limiter for the broker.

Lives in its own module so it can be unit-tested without dragging in
dbus. The broker creates one RateLimiter instance and calls `check()`
from RequestPermission; anything over the hard threshold is rejected
with an audit row and a DBusException in the caller.

Design notes:
- Sliding window per (uid, action) key. Buckets are plain deques of
  float timestamps; cheap enough at the volumes we care about.
- Hard threshold only in v1. A "soft" threshold was on the table
  (slow the requester rather than deny) but Phase 1 has no
  backpressure channel to the SDK — raising is the only signal
  that actually reaches the caller.
- No periodic GC. Deques self-trim on each `check()` call; idle
  keys keep their empty deque until process exit. At single-tenant
  scale that's negligible; a future multi-user broker should add
  a timed sweep (see todo once raised).
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Callable


DEFAULT_LIMIT = 50
DEFAULT_WINDOW_S = 1.0
# Per-process cap on distinct (uid, action) keys tracked. A hostile
# caller that cycles action strings would otherwise grow _buckets
# without bound and exhaust broker memory. We evict the bucket with
# the oldest most-recent-entry when we overflow (the caller whose
# traffic has been quiet longest).
DEFAULT_MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self, *, limit: int = DEFAULT_LIMIT,
                 window_s: float = DEFAULT_WINDOW_S,
                 max_keys: int = DEFAULT_MAX_KEYS,
                 now: "Callable[[], float] | None" = None):
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        if max_keys < 1:
            raise ValueError(f"max_keys must be >= 1, got {max_keys}")
        self._limit = int(limit)
        self._window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._now = now or time.monotonic
        self._buckets: dict[tuple[int, str], deque[float]] = {}
        self._lock = Lock()

    def check(self, uid: int, action: str) -> bool:
        """True iff this call is within the rate; False means reject.

        Records the call when allowed. Rejections are **not** recorded
        in the window — otherwise a flood would keep the bucket full
        forever and the caller would never recover.
        """
        key = (int(uid), str(action))
        now = self._now()
        cutoff = now - self._window_s
        with self._lock:
            existing = key in self._buckets
            if not existing and len(self._buckets) >= self._max_keys:
                # Evict the bucket with the oldest most-recent-entry.
                # Empty buckets go first; otherwise we pick the one
                # whose last-used time is earliest. Cost is O(n) at
                # overflow, but overflow is a DoS signal and shouldn't
                # happen on a healthy system.
                def _last_seen(item):
                    b = item[1]
                    return b[-1] if b else -1.0
                victim = min(self._buckets.items(), key=_last_seen)[0]
                del self._buckets[victim]
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            return True

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_s(self) -> float:
        return self._window_s
