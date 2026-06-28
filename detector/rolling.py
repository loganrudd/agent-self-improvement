from collections import deque


class RollingStats:
    """Streaming mean and sample std (n-1) over a bounded or unbounded window.

    Push floats one at a time. With maxlen set, old values are evicted as new
    ones arrive (sliding window). Without maxlen, grows indefinitely (used for
    the frozen baseline fit).
    """

    def __init__(self, maxlen: int | None = None) -> None:
        self._buf: deque[float] = deque(maxlen=maxlen)
        self._sum: float = 0.0
        self._sum_sq: float = 0.0

    def push(self, x: float) -> None:
        if self._buf.maxlen and len(self._buf) == self._buf.maxlen:
            evicted = self._buf[0]
            self._sum -= evicted
            self._sum_sq -= evicted * evicted
        self._buf.append(x)
        self._sum += x
        self._sum_sq += x * x

    def extend(self, xs: list[float]) -> None:
        for x in xs:
            self.push(x)

    @property
    def n(self) -> int:
        return len(self._buf)

    @property
    def mean(self) -> float:
        if self.n == 0:
            return 0.0
        return self._sum / self.n

    @property
    def std(self) -> float:
        """Sample std (n-1). Returns 0.0 when n < 2."""
        if self.n < 2:
            return 0.0
        variance = (self._sum_sq - self._sum * self._sum / self.n) / (self.n - 1)
        # Floating-point cancellation can yield tiny negatives; clamp to 0.
        return variance ** 0.5 if variance > 0 else 0.0
