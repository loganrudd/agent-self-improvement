from collections import deque


class RollingStats:
    """Streaming mean and sample std (n-1) over a bounded or unbounded window.

    Push floats one at a time. With maxlen set, old values are evicted as new
    ones arrive (sliding window). Without maxlen, grows indefinitely (used for
    the frozen baseline fit).

    std is computed directly from the stored deque to avoid floating-point
    cancellation in the naive sum-of-squares formula.
    """

    def __init__(self, maxlen: int | None = None) -> None:
        self._buf: deque[float] = deque(maxlen=maxlen)
        self._sum: float = 0.0

    def push(self, x: float) -> None:
        if self._buf.maxlen and len(self._buf) == self._buf.maxlen:
            self._sum -= self._buf[0]
        self._buf.append(x)
        self._sum += x

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
        m = self.mean
        variance = sum((x - m) ** 2 for x in self._buf) / (self.n - 1)
        return variance ** 0.5 if variance > 0 else 0.0
