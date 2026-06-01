import tushare as ts
import time
import logging
from backend.config import TUSHARE_TOKEN

logger = logging.getLogger(__name__)


class TushareClient:
    """tushare API wrapper with rate limiting and exponential backoff retry.

    Rate limit: 400 calls/min (conservative for 6200-point Pro tier; tested 500/min w/o throttle).
    """

    MAX_RETRIES = 3
    BASE_DELAY = 2  # seconds
    RATE_LIMIT = 400  # calls per minute

    def __init__(self):
        ts.set_token(TUSHARE_TOKEN)
        self.pro = ts.pro_api()
        self._calls = 0
        self._window_start = time.time()

    def _rate_limit(self):
        """Enforce calls-per-minute rate limit."""
        self._calls += 1
        elapsed = time.time() - self._window_start
        if elapsed < 60 and self._calls >= self.RATE_LIMIT:
            wait = 60 - elapsed + 1
            logger.info(f"Rate limit: {self._calls} calls in {elapsed:.0f}s, sleeping {wait:.0f}s")
            time.sleep(wait)
            self._calls = 0
            self._window_start = time.time()
        elif elapsed >= 60:
            self._calls = 0
            self._window_start = time.time()

    def call(self, func_name: str, **kwargs) -> list[dict]:
        """Call a tushare API function. Returns list of record dicts. Empty list if no data."""
        func = getattr(self.pro, func_name, None)
        if func is None:
            raise ValueError(f"Unknown tushare API: {func_name}")

        for attempt in range(self.MAX_RETRIES + 1):
            self._rate_limit()
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
