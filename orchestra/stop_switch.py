"""A persistent, cross-process operator stop for one Orchestra state directory."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from . import config, state


class Stopped(OSError):
    def __init__(self):
        super().__init__("Orchestra is stopped; run orchestra resume to allow new work")


def is_stopped() -> bool:
    try:
        (config.home() / "STOPPED").lstat()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def _gate(mode: int):
    home = config.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep this inode stable across stop/resume, separate from the outcome
    # log lock. Hold shared admission only while claiming or launching work.
    fd = os.open(home / "dispatch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, mode)
        yield
    finally:
        os.close(fd)


@contextmanager
def admission():
    with _gate(fcntl.LOCK_SH):
        if is_stopped():
            raise Stopped()
        yield


def dispatch_change():
    """Serialize a short queue state change, even while dispatch is stopped."""
    return _gate(fcntl.LOCK_EX)


def stop() -> int:
    with _gate(fcntl.LOCK_EX):
        marker = config.home() / "STOPPED"
        if not is_stopped():
            with marker.open("x", encoding="utf-8") as out:
                out.write(state.now() + "\n")
                out.flush()
                os.fsync(out.fileno())
            state.audit("dispatch.stopped")
        # Repeating stop retries cleanup. A failed sweep leaves the latch set.
        return _cleanup()


def _cleanup() -> int:
    from . import backends

    try:
        removed = backends.stop_skills()
        backends.reap_abandoned_stages()
    except OSError as exc:
        state.audit("dispatch.stop_cleanup_failed", error=type(exc).__name__)
        raise
    state.audit("dispatch.stop_cleanup", containers=removed)
    return removed


def resume() -> bool:
    with _gate(fcntl.LOCK_EX):
        if not is_stopped():
            return False
        # A previous stop may have lost its connection to Docker or exited
        # mid-sweep. Confirm removal before admitting any new work.
        _cleanup()
        (config.home() / "STOPPED").unlink()
        state.audit("dispatch.resumed")
        return True
