"""The daemon and its durable request spool (book 4.1, 4.12, 4.14).

A request is one JSON file in ~/.orchestra/requests/. `orchestra submit`
writes one; the daemon picks it up, runs it through the same lifecycle as the
command line, and writes the result back into the same file. There is no
network port: the spool directory is the gateway, so the only senders are
people who can already write to your home directory.

Statuses (the state model of book 5.10):
  pending       queued, waiting for the daemon
  running       an agent is working on it
  needs_review  an edit run finished and its diff is waiting for you:
                `orchestra review <task>`, then `orchestra apply` or `discard`
  needs_human   the router found no agent that may do this (privacy, budget
                or nothing installed). It is yours to do, or to re-declare
  done / failed finished; the result is in the file
  cancelled     withdrawn with `orchestra cancel <id>` before it started
  interrupted   the daemon died mid-run. NOT re-run automatically, because
                the agent may already have done things (book 4.14).
                Resubmit it yourself if it is safe to repeat.

The daemon handles one request at a time, so a long run delays the ones behind
it; the per-run timeout bounds how long. Parallel work is chapter 12.

All state is in files, so a restarted daemon resumes where it left off.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import adapters, config, control, state, tasks
from .contract import ACCESS_LEVELS

OPEN = ("pending", "running", "needs_review")


class SpoolError(Exception):
    pass


def _dir() -> Path:
    path = config.home() / "requests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save(req: dict) -> dict:
    req["updated"] = state.now()
    path = _dir() / f"{req['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written request
    return req


def all_requests() -> List[dict]:
    out = []
    for path in sorted(_dir().glob("*.json")):
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(req, dict) and "id" in req:
            out.append(req)
    return out


def load(request_id: str) -> dict:
    """Find a request by id or by any unique part of it."""
    matches = [r for r in all_requests() if request_id in r["id"]]
    exact = [r for r in matches if r["id"] == request_id]
    if exact:
        return exact[0]
    if not matches:
        raise SpoolError(f"no request matching '{request_id}'")
    if len(matches) > 1:
        raise SpoolError(f"'{request_id}' matches {len(matches)} requests; be more specific")
    return matches[0]


def submit(brief: str, agent: Optional[str] = None, cwd: Optional[str] = None,
           access: Optional[str] = None, timeout: Optional[int] = None,
           files: Optional[List[str]] = None, meta: Optional[dict] = None,
           skill: Optional[str] = None, use_skills: bool = True) -> dict:
    cfg = config.load()
    access = access or cfg["defaults"]["access"]
    if agent:
        adapters.get(agent)  # reject unknown agents at the door
    leaks = tasks.find_secrets(brief)
    if leaks:  # a queued request is a file on disk; do not write credentials into it
        raise SpoolError("not queued: the task contains " + ", ".join(leaks))
    if access not in ACCESS_LEVELS:
        raise SpoolError(f"access must be one of {ACCESS_LEVELS}")
    # The daemon runs elsewhere, so "the current directory" must be pinned now.
    cwd = str(Path(cwd or ".").expanduser().resolve())
    if not os.path.isdir(cwd):
        raise SpoolError(f"working directory not found: {cwd}")
    req = {
        # Sortable by submission time (to the microsecond), so the daemon
        # works first-come, first-served.
        "id": datetime.now().strftime("%Y%m%d-%H%M%S-%f") + secrets.token_hex(1),
        "created": state.now(),
        "source": "cli",
        "status": "pending",
        "task_id": None,
        "agent": agent,
        "access": access,
        "cwd": cwd,
        "timeout": timeout,
        # Pinned now, relative to the directory the request was made from.
        "files": [str((Path(cwd) / os.path.expanduser(f)).resolve()) for f in (files or [])],
        # Declared routing properties. Anything absent is classified by the
        # router, conservatively, when the daemon picks the request up.
        "meta": {k: v for k, v in (meta or {}).items() if v is not None},
        "skill": skill,
        "use_skills": use_skills,
        "brief": brief,
        "result": None,
    }
    state.audit("request.submitted", request_id=req["id"], agent=agent or "router's choice",
                access=access, cwd=cwd)
    return _save(req)


def cancel(request_id: str) -> dict:
    """Withdraw a request that has not started. A running agent cannot be
    stopped from here yet; stop the daemon (Ctrl-C) if you must."""
    req = load(request_id)
    if req["status"] != "pending":
        raise SpoolError(f"request {req['id']} is {req['status']}; only pending requests can be cancelled")
    req["status"] = "cancelled"
    state.audit("request.cancelled", request_id=req["id"])
    return _save(req)


def close_review(request_id: Optional[str], status: str) -> None:
    """A reviewed diff was applied or discarded: settle the request it came from."""
    if not request_id:
        return
    try:
        req = load(request_id)
    except SpoolError:
        return
    if req["status"] == "needs_review":
        req["status"] = status
        _save(req)


def recover(log: Callable[[str], None] = print) -> List[str]:
    """After a restart: anything still marked running was cut off mid-run."""
    interrupted = []
    for req in all_requests():
        if req["status"] == "running":
            req["status"] = "interrupted"
            req["result"] = {"status": "interrupted",
                             "error": "the daemon stopped while this was running; not re-run"}
            _save(req)
            state.audit("request.interrupted", request_id=req["id"])
            log(f"{req['id']} was interrupted by a restart; left for you to review")
            interrupted.append(req["id"])
    return interrupted


def work_once(log: Callable[[str], None] = print) -> int:
    """One pass over the spool. Returns how many requests were run."""
    ran = 0
    for req in all_requests():
        if req["status"] != "pending":
            continue
        try:
            cfg = config.load()
        except config.ConfigError as e:
            log(f"configuration problem, not running anything: {e}")
            return ran
        req["status"] = "running"
        _save(req)
        log(f"{req['id']} routing ({req['access']}"
            + (f", agent named: {req['agent']}" if req.get("agent") else "") + ")")
        try:
            result = control.delegate(req["brief"], agent=req["agent"], cwd=req["cwd"],
                                      timeout=req.get("timeout"), access=req["access"],
                                      files=req.get("files") or None,
                                      meta=req.get("meta") or None, skill=req.get("skill"),
                                      use_skills=req.get("use_skills", True),
                                      source="daemon", request_id=req["id"])
            req["result"] = result.to_dict()
            req["task_id"] = result.task_id
            req["agent"] = result.agent
            if result.gate in ("pending", "blocked"):
                req["status"] = "needs_review"
            elif result.status == "escalated":
                req["status"] = "needs_human"  # no agent may do it; it is the operator's
            else:
                req["status"] = "done" if result.ok else "failed"
        except Exception as e:  # the daemon must outlive any single request
            req["result"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            req["status"] = "failed"
        _save(req)
        log(f"{req['id']} {req['status']}"
            + (f": orchestra review {result.task_id}" if req["status"] == "needs_review" else ""))
        ran += 1
    return ran


def run(once: bool = False, log: Callable[[str], None] = print) -> None:
    pid_file = config.home() / "daemon.pid"
    running = state.daemon_pid()
    if running and running != os.getpid():
        raise SpoolError(f"a daemon is already running (pid {running})")
    config.home().mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    stop = {"now": False}

    def ask_to_stop(signum, frame):
        stop["now"] = True
        log("stopping after the current request")

    if not once:
        signal.signal(signal.SIGTERM, ask_to_stop)
        signal.signal(signal.SIGINT, ask_to_stop)
    state.audit("daemon.started", pid=os.getpid())
    log(f"orchestra daemon started (pid {os.getpid()}), watching {_dir()}")
    try:
        recover(log)
        while True:
            work_once(log)
            if once or stop["now"]:
                break
            try:
                poll = float(config.load()["daemon"]["poll_seconds"])
            except (config.ConfigError, KeyError, TypeError, ValueError):
                poll = 2.0
            time.sleep(max(0.2, poll))
    finally:
        state.audit("daemon.stopped", pid=os.getpid())
        try:
            pid_file.unlink()
        except OSError:
            pass
        log("orchestra daemon stopped")
