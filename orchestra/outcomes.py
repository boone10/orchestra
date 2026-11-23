"""The outcomes log: the seed of memory (book 2.8, listings 2.2 and 2.3).

One JSON object per line, appended after every delegation. Chapter 7 replaces
this with a real memory store; the point for now is that every run leaves a
durable trace the next run can read.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import config
from .contract import OK, Result


def home() -> Path:
    return config.home()


def log_path() -> Path:
    return home() / "outcomes.jsonl"


def backup_path() -> Path:
    return home() / "outcomes.jsonl.bak"


@contextmanager
def _locked():
    # The directory has a stable inode even when one of the logs is restored.
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def guard() -> Optional[dict]:
    if not home().exists():
        return None
    with _locked():
        return _guard()


def _guard() -> Optional[dict]:
    """Keep the outcome log from being lost quietly.

    Every line is written to the log and to a second copy beside it. The log
    is append-only, so it should never be smaller than its copy. If it is,
    something outside the normal write path emptied or shortened it: restore
    it from the copy, say so on stderr, and leave a line in the audit log.
    Returns what was restored, or None.

    To clear the log on purpose, delete both files.
    """
    main, bak = log_path(), backup_path()
    try:
        main_size = main.stat().st_size if main.exists() else 0
        bak_size = bak.stat().st_size if bak.exists() else 0
        if bak_size > main_size:
            _replace_from(bak, main)
            info = {"restored_bytes": bak_size, "was_bytes": main_size}
            from . import state  # late: keep this module importable on its own
            state.audit("outcomes.restored", **info)
            print(f"orchestra: the outcome log had shrunk from {bak_size} to {main_size} bytes "
                  f"since it was last written. Restored from {bak.name}.", file=sys.stderr)
            return info
        if main_size > bak_size:
            bak.parent.mkdir(parents=True, exist_ok=True)
            _replace_from(main, bak)  # first run with a copy, or the copy fell behind
    except OSError:
        pass  # a guard must never be the reason a run fails
    return None


def _replace_from(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        shutil.copyfile(source, path)
        os.replace(path, target)
    finally:
        path.unlink(missing_ok=True)


def append_raw(entry: dict) -> None:
    """Append one line to the log and to its copy."""
    with _locked():
        _guard()
        _append(entry)


def _append(entry: dict) -> None:
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    for path in (log_path(), backup_path()):
        with path.open("a+b") as f:
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")  # keep a torn tail separate from this valid record
            f.write(line)


def record(result: Result, brief: str, cwd: Optional[str] = None, **fields) -> dict:
    """Append one outcome. The record is regular on purpose (book 5.5): the
    router, the budget and a human reviewer should all be able to read it
    without guessing. `brief` here is the task as the operator wrote it; the
    full assembled brief is kept in the task folder and identified by digest.
    """
    entry = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "task_id": result.task_id,
        "task_class": None,
        "route": result.route,     # the router's decision, axes and reasons
        "privacy": None,
        "stakes": None,
        "location": None,          # where the work ran: "cloud" or "local"
        "skill": None,             # the skill (name@version) that shaped or performed it
        "memories_used": None,     # ids of memory records included in the brief
        "project": None,
        "agent": result.agent,
        "adapter_version": None,
        "attempt": 1,
        "access": None,
        "status": result.status,
        "exit_code": result.exit_code,
        "error": result.error,
        "started": None,
        "finished": None,
        "duration_ms": result.duration_ms,
        "cost_usd": result.cost_usd,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "brief_digest": None,
        "brief_chars": None,
        "files_in_scope": None,
        "effects": result.effects,
        "validation": {"ok": not result.problems, "problems": result.problems or []},
        "gate": result.gate,       # the human-gate state when the run finished
        "artifacts": result.artifacts,
        "tests_run": None,         # chapter 13
        "group": None,             # ties together runs of one brief through several agents
        "source": None,            # which surface the request came through
        "request_id": None,
        "cwd": cwd,
        "brief": brief,
    }
    unknown = set(fields) - set(entry)
    if unknown:
        raise TypeError(f"unknown outcome fields: {sorted(unknown)}")
    entry.update(fields)
    append_raw(entry)
    return entry


def read_all() -> List[dict]:
    if not home().exists():
        return []
    with _locked():
        return _read_all()


def _read_all() -> List[dict]:
    _guard()
    path = log_path()
    if not path.exists():
        return []
    entries = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a torn or hand-edited line must not break the report
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def summarize(entries: List[dict]) -> dict:
    """Cross-run evidence a router will later decide on (not a router yet)."""
    by_agent = {}
    for e in entries:
        a = by_agent.setdefault(e.get("agent", "?"),
                                {"runs": 0, "failures": 0, "cost_usd": 0.0, "cost_unknown": 0})
        a["runs"] += 1
        if e.get("status") != OK:
            a["failures"] += 1
        if e.get("cost_usd") is None:
            a["cost_unknown"] += 1
        else:
            a["cost_usd"] += e["cost_usd"]
    return {
        "runs": sum(a["runs"] for a in by_agent.values()),
        "failures": sum(a["failures"] for a in by_agent.values()),
        "cost_usd": sum(a["cost_usd"] for a in by_agent.values()),
        "cost_unknown": sum(a["cost_unknown"] for a in by_agent.values()),
        "by_agent": by_agent,
    }


def recover_from_audit() -> List[dict]:
    """Rebuild missing outcome lines from the audit log.

    The audit log records every finished run: when, which agent, the status,
    the cost and the task id. It does not keep the task wording (on purpose:
    the audit line is written before the task has been checked for
    credentials), so recovered lines say so in place of the task. Lines
    already in the outcome log are left alone. Returns what was added.
    """
    path = home() / "logs" / "audit.jsonl"
    if not path.is_file():
        guard()
        return []
    with _locked():
        return _recover(path)


def _recover(path: Path) -> List[dict]:
    existing = _read_all()
    have_ids = {(e["task_id"], e.get("attempt", 1)) for e in existing if e.get("task_id")}
    have_times = Counter((e.get("ts"), e.get("agent")) for e in existing if not e.get("task_id"))
    received, routed, attempts, added = {}, {}, {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind, tid = ev.get("event"), ev.get("task_id")
        request = tid or ev.get("request_id")
        if kind == "request.received":
            received[request] = ev
        elif kind == "route.decided":
            routed[tid] = ev
        elif kind == "run.started" and tid:
            attempts[tid] = ev.get("attempt", 1)
        elif kind == "run.finished":
            attempt = ev.get("attempt", attempts.get(tid, 1))
            key = (ev.get("ts"), ev.get("agent"))
            if tid and (tid, attempt) in have_ids:
                continue
            if not tid and have_times[key] > 0:
                have_times[key] -= 1
                continue
            same = received.get(request, {})
            if not request and (same.get("agent") != ev.get("agent") or same.get("cwd") != ev.get("cwd")):
                same = {}
            cwd = ev.get("cwd") or same.get("cwd")
            route = routed.get(tid) or {}
            entry = {
                "ts": ev.get("ts"), "task_id": tid, "attempt": attempt, "task_class": None, "project": cwd,
                "agent": ev.get("agent"), "access": ev.get("access"), "status": ev.get("status"),
                "error": ev.get("error"), "cost_usd": ev.get("cost_usd"), "gate": ev.get("gate"),
                "source": ev.get("source"), "request_id": ev.get("request_id"), "cwd": cwd,
                "route": {"outcome": route.get("outcome"), "executor": route.get("executor"),
                          "reasons": route.get("reasons")} if route else None,
                "brief": same.get("brief")
                or "[recovered from the audit log; the task wording was not kept]",
                "recovered": True,
            }
            _append(entry)
            if tid:
                have_ids.add((tid, attempt))
            added.append(entry)
    return added
