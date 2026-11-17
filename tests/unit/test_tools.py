import sys
import time

import pytest

from reovault import tools
from reovault.tools import ToolError, run


def test_a_timeout_interrupts_first_so_the_tool_can_clean_up(tmp_path, monkeypatch):
    """restic removes its repository lock on SIGINT; SIGKILL leaves it behind."""
    monkeypatch.setattr(tools, "INTERRUPT_GRACE_SECS", 10)
    marker = tmp_path / "cleaned-up"
    script = (
        "import signal, sys, time, pathlib\n"
        f"signal.signal(signal.SIGINT, lambda *a: (pathlib.Path({str(marker)!r}).touch(), "
        "sys.exit(130)))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(ToolError) as exc:
        run([sys.executable, "-c", script], timeout=1)
    assert exc.value.timed_out
    assert marker.exists()
    assert time.monotonic() - started < 10


def test_a_tool_that_ignores_the_interrupt_is_killed(monkeypatch):
    monkeypatch.setattr(tools, "INTERRUPT_GRACE_SECS", 1)
    script = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)\n"
    started = time.monotonic()
    with pytest.raises(ToolError):
        run([sys.executable, "-c", script], timeout=1)
    assert time.monotonic() - started < 10


def test_shutdown_interrupts_tools_still_running(tmp_path):
    """A stop must let restic remove its lock, not leave it to be killed."""
    import threading

    marker = tmp_path / "cleaned-up"
    script = (
        "import signal, sys, time, pathlib\n"
        f"signal.signal(signal.SIGINT, lambda *a: (pathlib.Path({str(marker)!r}).touch(), "
        "sys.exit(130)))\n"
        "time.sleep(60)\n"
    )
    done = threading.Event()
    t = threading.Thread(
        target=lambda: (run([sys.executable, "-c", script], timeout=60), done.set())
    )
    t.start()
    deadline = time.monotonic() + 10
    while not tools._running and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)  # let the child install its handler
    tools.interrupt_all()
    t.join(timeout=10)
    assert done.is_set() and marker.exists()
