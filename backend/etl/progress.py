"""Unified stage progress logging — count throttle + time heartbeat."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from backend.config import (
    LOG_PROGRESS_DAY_STEP,
    LOG_PROGRESS_HEARTBEAT_SEC,
    LOG_PROGRESS_STOCK_STEP,
)

logger = logging.getLogger(__name__)


class StageProgress:
    """Thread-safe progress reporter for long-running ETL stages.

    Log format (stable prefix for grep):
        progress {stage}: {done}/{total} ({pct}%) | {elapsed}s | {rate} {unit}/s | ETA ~{eta}s
        progress {stage}: still running | {done}/{total} | {elapsed}s
        progress {stage}: done | {elapsed}s | {summary}
    """

    def __init__(
        self,
        stage: str,
        total: int,
        *,
        count_step: Optional[int] = None,
        heartbeat_sec: Optional[float] = None,
        unit: str = "items",
    ):
        self.stage = stage
        self.total = max(0, int(total))
        self.count_step = max(1, count_step or max(1, self.total // 20))
        self.heartbeat_sec = (
            float(LOG_PROGRESS_HEARTBEAT_SEC)
            if heartbeat_sec is None
            else float(heartbeat_sec)
        )
        self.unit = unit
        self._done = 0
        self._t0 = 0.0
        self._last_log_mono = 0.0
        self._lock = threading.Lock()

    def log_start(self, **extra: object) -> None:
        suffix = " ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"progress {self.stage}: started | total={self.total} {self.unit}"
        if suffix:
            msg = f"{msg} | {suffix}"
        with self._lock:
            self._t0 = time.monotonic()
            self._last_log_mono = self._t0
        logger.info(msg)

    def tick(self, n: int = 1, *, force: bool = False, force_heartbeat: bool = False) -> None:
        with self._lock:
            self._done += n
            self._maybe_log(force=force, force_heartbeat=force_heartbeat)

    def log_done(self, **extra: object) -> None:
        elapsed = time.monotonic() - self._t0 if self._t0 else 0.0
        suffix = " ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"progress {self.stage}: done | {elapsed:.0f}s"
        if suffix:
            msg = f"{msg} | {suffix}"
        logger.info(msg)

    def _maybe_log(self, *, force: bool, force_heartbeat: bool) -> None:
        now = time.monotonic()
        elapsed = now - self._t0 if self._t0 else 0.0
        done = self._done
        total = self.total
        if total <= 0:
            return

        at_step = (done % self.count_step == 0) or done == total
        heartbeat_due = (
            force_heartbeat
            or (
                self.heartbeat_sec > 0
                and (now - self._last_log_mono) >= self.heartbeat_sec
                and done > 0
                and done < total
            )
        )

        if not (force or at_step or heartbeat_due):
            return

        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (total - done) / rate if rate > 0 else 0.0
        pct = done * 100 // total

        if heartbeat_due and not at_step and not force:
            logger.info(
                "progress %s: still running | %d/%d (%d%%) | %.0fs",
                self.stage, done, total, pct, elapsed,
            )
        else:
            logger.info(
                "progress %s: %d/%d (%d%%) | %.0fs | %.1f %s/s | ETA ~%.0fs",
                self.stage, done, total, pct, elapsed, rate, self.unit, eta,
            )
        self._last_log_mono = now


def day_progress(stage: str, total_days: int) -> StageProgress:
    return StageProgress(
        stage, total_days,
        count_step=max(1, LOG_PROGRESS_DAY_STEP),
        unit="days",
    )


def stock_progress(stage: str, total_stocks: int) -> StageProgress:
    return StageProgress(
        stage, total_stocks,
        count_step=max(1, LOG_PROGRESS_STOCK_STEP),
        unit="stocks",
    )
