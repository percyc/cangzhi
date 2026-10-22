"""Real local process/connection faults; no production database or files."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

from apps.worker.core.config import settings
from apps.worker.services import task_execution


def stopped(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
    except FileNotFoundError:
        return True


def test_child_exits_when_parent_disappears():
    # A bad frame gives an acknowledgement only once the real child loop and
    # parent watchdog are ready, avoiding a sleep-based startup race.
    script = """
import threading, time
from apps.worker.services.task_execution import _SupervisedWorker, _read_frame
w = _SupervisedWorker()
w._ensure_proc()
w._proc.stdin.write(b'0\\n'); w._proc.stdin.flush()
_read_frame(w._resp_stream, 20, threading.Event())
print(w._proc.pid, flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    child_pid = None
    try:
        assert select.select([parent.stdout], [], [], 25)[0], "child never acknowledged"
        child_pid = int(parent.stdout.readline().strip())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not stopped(child_pid):
            time.sleep(0.05)
        assert stopped(child_pid), "orphaned executor kept running"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if child_pid and not stopped(child_pid):
            os.killpg(child_pid, signal.SIGKILL)


def test_heartbeat_database_loss_cancels_execution(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://unused@127.0.0.1:1/unused")
    monkeypatch.setattr(settings, "task_heartbeat_seconds", 1)
    stop, cancel = threading.Event(), threading.Event()
    thread = threading.Thread(target=task_execution._heartbeat_loop,
        args=(stop, cancel), kwargs={"job_id": 1, "attempt_id": "synthetic"})
    thread.start()
    try:
        assert cancel.wait(8), "coordinator loss did not cancel the task"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
