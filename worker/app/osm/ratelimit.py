import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Guarantees at least `min_interval` seconds between calls to wait().

    Nominatim's usage policy allows at most 1 request per second, for the whole
    application, so one limiter is shared by every job in this worker process.
    """

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                delay = self._last + self.min_interval - self._clock()
                if delay > 0:
                    self._sleep(delay)
            self._last = self._clock()
