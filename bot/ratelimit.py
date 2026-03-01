"""Token-bucket rate limiter for DMs and vision API calls."""

import time


class RateLimiter:
    def __init__(self, max_tokens: int, period_seconds: float):
        self.max_tokens = max_tokens
        self.period = period_seconds
        self._timestamps: list[float] = []

    def _cleanup(self):
        now = time.monotonic()
        cutoff = now - self.period
        self._timestamps = [t for t in self._timestamps if t > cutoff]

    def can_proceed(self) -> bool:
        self._cleanup()
        return len(self._timestamps) < self.max_tokens

    def consume(self) -> bool:
        """Try to consume a token. Returns True if allowed."""
        self._cleanup()
        if len(self._timestamps) >= self.max_tokens:
            return False
        self._timestamps.append(time.monotonic())
        return True

    @property
    def remaining(self) -> int:
        self._cleanup()
        return max(0, self.max_tokens - len(self._timestamps))

    @property
    def retry_after(self) -> float:
        """Seconds until next token is available."""
        self._cleanup()
        if len(self._timestamps) < self.max_tokens:
            return 0.0
        return self._timestamps[0] + self.period - time.monotonic()
