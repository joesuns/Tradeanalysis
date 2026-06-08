import logging
import time

from backend.etl.progress import StageProgress


def _lines(caplog, stage: str):
    prefix = f"progress {stage}:"
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def test_stage_progress_count_throttle(caplog):
    p = StageProgress("test.stage", total=100, count_step=5, heartbeat_sec=999)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start(unit="items")
        for _ in range(100):
            p.tick()
        p.log_done(rows=100)

    lines = _lines(caplog, "test.stage")
    assert any("5/100" in ln for ln in lines)
    assert any("100/100 (100%)" in ln for ln in lines)


def test_stage_progress_heartbeat(caplog):
    p = StageProgress("hb.stage", total=1000, count_step=500, heartbeat_sec=0.05)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start()
        p.tick()
        time.sleep(0.08)
        p.tick(force_heartbeat=True)
        p.log_done()

    lines = _lines(caplog, "hb.stage")
    assert len(lines) >= 2, f"expected heartbeat lines, got {lines}"


def test_stage_progress_thread_safe(caplog):
    import threading

    p = StageProgress("thread.stage", total=200, count_step=10, heartbeat_sec=999)
    with caplog.at_level(logging.INFO, logger="backend.etl.progress"):
        p.log_start()

        def worker():
            for _ in range(50):
                p.tick()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        p.log_done()

    lines = _lines(caplog, "thread.stage")
    assert any("200/200 (100%)" in ln for ln in lines)
