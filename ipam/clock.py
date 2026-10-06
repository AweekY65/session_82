"""可注入时钟：生产环境用 SystemClock，测试用 FakeClock。"""

import time


class SystemClock:
    """基于 time.time() 的真实时钟。"""

    def now(self) -> float:
        return time.time()


class FakeClock:
    """测试用手动推进的时钟。"""

    def __init__(self, start: float = 1_000_000.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move clock backwards")
        self._now += float(seconds)
