"""Tiny in-memory per-IP daily rate limiter.

Kept deliberately simple: a dict from IP -> (UTC-date-string, count). Resets
naturally at midnight UTC because the date key changes. Good enough for a
single-process personal app. If you ever run multiple workers or want
horizontal scaling, swap this for Redis.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone


class RateLimiter:
    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._counts: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def check(self, ip: str) -> tuple[bool, int]:
        """Return (allowed, remaining_after_this_call). Increments on allow."""
        today = self._today()
        with self._lock:
            day, count = self._counts.get(ip, (today, 0))
            if day != today:
                count = 0
            if count >= self.daily_limit:
                return False, 0
            count += 1
            self._counts[ip] = (today, count)
            return True, max(0, self.daily_limit - count)

    def remaining(self, ip: str) -> int:
        today = self._today()
        with self._lock:
            day, count = self._counts.get(ip, (today, 0))
            if day != today:
                return self.daily_limit
            return max(0, self.daily_limit - count)
