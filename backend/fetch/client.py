import tushare as ts
import time
import logging
import threading
from collections import defaultdict, deque

from backend.config import TUSHARE_TOKEN

logger = logging.getLogger(__name__)


class _InterfaceRateLimiter:
    """Process-wide, per-interface sliding-window rate limiter (thread-safe).

    Tushare enforces its call quota *per interface*, not globally. When the
    parallel fetch spawns one TushareClient per worker thread, a per-instance
    counter lets each thread independently allow the full budget — combined
    they overshoot the per-interface quota and get throttled server-side.

    A single shared limiter keyed by interface name fixes this: concurrent
    threads hitting the *same* interface draw from one budget, while different
    interfaces keep independent budgets (matching Tushare's real policy).

    Uses a 60s sliding window of call timestamps per interface. The lock is
    released before sleeping so other interfaces never block on each other.
    """

    def __init__(self, limit_per_min: int, window: float = 60.0):
        self.limit = limit_per_min
        self.window = window
        self._lock = threading.Lock()
        self._calls = defaultdict(deque)  # interface -> deque[timestamps]

    def acquire(self, interface: str, now_fn=time.time, sleep_fn=time.sleep):
        """Block until a call slot is available for ``interface``."""
        while True:
            with self._lock:
                now = now_fn()
                dq = self._calls[interface]
                while dq and now - dq[0] >= self.window:
                    dq.popleft()
                if len(dq) < self.limit:
                    dq.append(now)
                    return
                wait = self.window - (now - dq[0]) + 0.01
            logger.info("Rate limit [%s]: %d calls in window, sleeping %.1fs",
                        interface, self.limit, wait)
            sleep_fn(wait)


class TushareClient:
    """tushare API wrapper with rate limiting and exponential backoff retry.

    Rate limiting is process-wide and per-interface (see _InterfaceRateLimiter)
    so multiple client instances across worker threads share one budget per
    interface — matching tushare's per-interface quota policy.
    """

    MAX_RETRIES = 3
    BASE_DELAY = 2  # seconds
    RATE_LIMIT = 480  # calls/min per interface (conservative under tested ~500/min on 6200pt tier)

    # Shared across ALL instances/threads in this process.
    _limiter = _InterfaceRateLimiter(RATE_LIMIT)

    def __init__(self):
        ts.set_token(TUSHARE_TOKEN)
        self.pro = ts.pro_api()

    def _rate_limit(self, func_name: str):
        """Enforce per-interface calls-per-minute rate limit (process-wide)."""
        self._limiter.acquire(func_name)

    def call(self, func_name: str, **kwargs) -> list[dict]:
        """Call a tushare API function. Returns list of record dicts. Empty list if no data."""
        func = getattr(self.pro, func_name, None)
        if func is None:
            raise ValueError(f"Unknown tushare API: {func_name}")

        for attempt in range(self.MAX_RETRIES + 1):
            self._rate_limit(func_name)
            try:
                result = func(**kwargs)
                if result is None or result.empty:
                    return []
                return result.to_dict("records")
            except Exception as e:
                if attempt < self.MAX_RETRIES:
                    delay = self.BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"tushare {func_name} attempt {attempt+1} failed: {e}. Retrying in {delay}s"
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"tushare {func_name} failed after {self.MAX_RETRIES} retries: {e}"
                    )
                    raise
