"""The daemon and its durable request spool (chapters 4 and 12).

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
  interrupted   the daemon died mid-run. Only explicitly replay-safe reads
                are eligible for one automatic recovery attempt.

All state is in files, so a restarted daemon resumes where it left off.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import shlex
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from . import adapters, config, control, state, stop_switch, tasks
from .contract import ACCESS_LEVELS

OPEN = ("pending", "running", "needs_review", "needs_human")
STATUSES = OPEN + ("done", "failed", "cancelled", "interrupted", "resolved")


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
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(req, out, indent=2, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return req


def _read_request(path: Path) -> Optional[dict]:
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(req, dict) or req.get("id") != path.stem or req.get("status") not in STATUSES:
        return None
    if req.get("access") not in ACCESS_LEVELS or not isinstance(req.get("brief"), str):
        return None
    if not isinstance(req.get("cwd"), str) or not os.path.isabs(req["cwd"]):
        return None
    for name in ("agent", "skill", "task_id"):
        if req.get(name) is not None and not isinstance(req[name], str):
            return None
    if req.get("result") is not None and not isinstance(req["result"], dict):
        return None
    if req.get("meta") is not None and not isinstance(req["meta"], dict):
        return None
    files = req.get("files")
    if files is not None and (not isinstance(files, list) or not all(isinstance(name, str) for name in files)):
        return None
    # A damaged keyed record must still occupy its key. Reject missing or
    # inconsistent key fields so a producer cannot mistake it for a new job.
    if re.search(r"-k[0-9a-f]{16}$", path.stem) or "idempotency_key" in req:
        key, fingerprint = req.get("idempotency_key"), req.get("payload_sha256")
        if not isinstance(key, str) or not isinstance(fingerprint, str):
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return None
        try:
            suffix = _key_suffix(key)
        except UnicodeEncodeError:
            return None
        if not path.stem.endswith(suffix):
            return None
    return req


def status_counts() -> dict:
    counts = {}
    for path in (config.home() / "requests").glob("*.json"):
        req = _read_request(path)
        status = req["status"] if req is not None else "unreadable"
        counts[status] = counts.get(status, 0) + 1
    return counts


def all_requests() -> List[dict]:
    out = []
    for path in sorted(_dir().glob("*.json")):
        req = _read_request(path)
        if req is not None:
            out.append(req)
    return out


def load(request_id: str) -> dict:
    """Find a request by id or by any unique part of it."""
    if request_id and Path(request_id).name == request_id:
        exact = _read_request(_dir() / (request_id + ".json"))
        if exact is not None:
            return exact
    matches = [r for r in all_requests() if request_id in r["id"]]
    if not matches:
        raise SpoolError(f"no request matching '{request_id}'")
    if len(matches) > 1:
        raise SpoolError(f"'{request_id}' matches {len(matches)} requests; be more specific")
    return matches[0]


def prepare_submission(brief: str, agent: Optional[str] = None, cwd: Optional[str] = None,
                       access: Optional[str] = None, timeout: Optional[int] = None,
                       files: Optional[List[str]] = None, meta: Optional[dict] = None,
                       skill: Optional[str] = None, use_skills: bool = True,
                       replay_safe: bool = False, *, check_cwd: bool = True) -> dict:
    """Validate and pin the request fields shared by manual and scheduled producers."""
    cfg = config.load()
    access = access or cfg["defaults"]["access"]
    if agent:
        adapters.get(agent)  # reject unknown agents at the door
    leaks = tasks.find_secrets(brief)
    if leaks:  # a queued request is a file on disk; do not write credentials into it
        raise SpoolError("not queued: the task contains " + ", ".join(leaks))
    if access not in ACCESS_LEVELS:
        raise SpoolError(f"access must be one of {ACCESS_LEVELS}")
    if not isinstance(replay_safe, bool) or (replay_safe and access != "read"):
        raise SpoolError("automatic crash replay is available only for explicitly replay-safe reads")
    # The daemon runs elsewhere, so "the current directory" must be pinned now.
    cwd = str(Path(cwd or ".").expanduser().resolve())
    if check_cwd and not os.path.isdir(cwd):
        raise SpoolError(f"working directory not found: {cwd}")
    return {
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
        "replay_safe": replay_safe,
    }


def _key_suffix(key: str) -> str:
    return "-k" + hashlib.sha256(key.encode()).hexdigest()[:16]


def submit(brief: str, agent: Optional[str] = None, cwd: Optional[str] = None,
           access: Optional[str] = None, timeout: Optional[int] = None,
           files: Optional[List[str]] = None, meta: Optional[dict] = None,
           skill: Optional[str] = None, use_skills: bool = True,
           idempotency_key: Optional[str] = None, schedule: Optional[str] = None,
           replay_safe: bool = False) -> dict:
    payload = prepare_submission(brief, agent, cwd, access, timeout, files, meta,
                                 skill, use_skills, replay_safe, check_cwd=False)
    payload["source"] = "schedule" if schedule is not None else "cli"
    if schedule is not None:
        payload["schedule"] = schedule
    keyed = {}
    suffix = ""
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise SpoolError("the idempotency key must be a nonempty string")
        if tasks.find_secrets(idempotency_key):
            raise SpoolError("the idempotency key contains a credential")
        # A keyed record is its own index. The digest suffix lets us find it
        # without opening every past result, and the timestamp retains FIFO.
        suffix = _key_suffix(idempotency_key)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        keyed = {"idempotency_key": idempotency_key,
                 "payload_sha256": hashlib.sha256(encoded).hexdigest()}
    with stop_switch.dispatch_change():
        if keyed:
            for path in _dir().glob(f"*{suffix}.json"):
                old = _read_request(path)
                if old is None:
                    raise SpoolError("cannot verify idempotency against an unreadable request")
                if old.get("idempotency_key") == idempotency_key:
                    if old.get("payload_sha256") != keyed["payload_sha256"]:
                        raise SpoolError("the idempotency key already belongs to a different request")
                    state.audit("request.deduplicated", request_id=old["id"], source=payload["source"])
                    return old
        if schedule is not None and stop_switch.is_stopped():
            raise stop_switch.Stopped()
        if not os.path.isdir(payload["cwd"]):
            raise SpoolError(f"working directory not found: {payload['cwd']}")
        request_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + secrets.token_hex(4) + suffix
        req = dict(payload, **keyed, id=request_id, created=state.now(), status="pending",
                   task_id=None, result=None)
        _save(req)
        state.audit("request.submitted", request_id=request_id, source=payload["source"],
                    schedule=schedule, agent=agent or "router's choice",
                    access=payload["access"], cwd=payload["cwd"])
        return req


def cancel(request_id: str) -> dict:
    """Withdraw a pending request, including while dispatch is stopped."""
    with stop_switch.dispatch_change():
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
    with stop_switch.dispatch_change():
        try:
            req = load(request_id)
        except SpoolError:
            return
        if req["status"] == "needs_review":
            req["status"] = status
            _save(req)
            state.audit("request.review_closed", request_id=req["id"], task_id=req.get("task_id"))


def resolve(request_id: str, reason: str) -> dict:
    """Record an operator's decision on an escalation and close the request."""
    reason = reason.strip()
    if not reason or tasks.find_secrets(reason):
        raise SpoolError("supply a decision without credentials")
    with stop_switch.dispatch_change():
        req = load(request_id)
        if req["status"] == "resolved" and req.get("resolution") == reason:
            return req
        if req["status"] != "needs_human":
            raise SpoolError(f"request {req['id']} is {req['status']}; only escalations can be resolved")
        req.update(status="resolved", resolution=reason)
        _save(req)
        state.audit("request.resolved", request_id=req["id"], task_id=req.get("task_id"))
        return req


def trace(request_id: str):
    """Yield recorded events in order, including operator decisions on the task."""
    req = load(request_id)
    task_ids = {req["task_id"]} if req.get("task_id") else set()
    try:
        audit = (config.home() / "logs/audit.jsonl").open(encoding="utf-8")
    except FileNotFoundError:
        return
    with audit:
        for line in audit:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("request_id") == req["id"]:
                if event.get("task_id"):
                    task_ids.add(event["task_id"])
                yield event
            elif event.get("task_id") in task_ids:
                yield event


def recover(log: Callable[[str], None] = print) -> List[str]:
    """After exclusive daemon startup, settle the previous owner's abandoned claims."""
    interrupted = []
    with stop_switch.dispatch_change():
        generation = stop_switch.generation()
        # A killed Python coordinator can leave a Docker skill alive. Its
        # home-scoped registration survives until the operator cleans it up.
        native_active = any((config.home() / "active-skills").glob("*.socket"))
        for req in all_requests():
            if req["status"] != "running":
                continue
            owner = req.get("owner")
            req.update(owner=None, lease_until=None)
            retry = (req.get("replay_safe") is True and req["access"] == "read"
                     and not req.get("crash_retries") and not native_active
                     and not stop_switch.is_stopped() and req.get("stop_generation") == generation)
            if retry:
                req.update(status="pending", crash_retries=1, result=None)
                _save(req)
                state.audit("request.requeued", request_id=req["id"], owner=owner,
                            reason="replay-safe read after coordinator exit", crash_retries=1)
                log(f"{req['id']} returned to pending for its one crash retry")
                continue
            req.update(status="interrupted", result={"status": "interrupted",
                       "error": "the daemon stopped while this was running; not re-run"})
            _save(req)
            state.audit("request.interrupted", request_id=req["id"], owner=owner)
            log(f"{req['id']} was interrupted by a restart; left for you to review")
            interrupted.append(req["id"])
    return interrupted


# A heartbeat shows that the daemon still owns a request. The singleton daemon
# lock, rather than the wall clock alone, proves abandonment on restart.
_HEARTBEAT_SECONDS = 10.0
_LEASE_SECONDS = 45


def _lease() -> dict:
    now = datetime.now(timezone.utc)
    return {"heartbeat_at": now.isoformat(),
            "lease_until": (now + timedelta(seconds=_LEASE_SECONDS)).isoformat()}


def _claim(path: Path, stopping: Callable[[], bool]) -> Optional[dict]:
    with stop_switch.dispatch_change():
        if stopping() or stop_switch.is_stopped():
            return None
        req = _read_request(path)
        if not req or req.get("status") != "pending":
            return None
        lease = _lease()
        req.update(lease, status="running", owner=f"{os.getpid()}-{secrets.token_hex(12)}",
                   claimed_at=lease["heartbeat_at"], stop_generation=stop_switch.generation())
        _save(req)
        state.audit("request.claimed", request_id=req["id"], owner=req["owner"],
                    lease_until=req["lease_until"])
        return req


def _owned(claim: dict) -> Optional[dict]:
    """Read under the dispatch lock before publishing any worker update."""
    req = _read_request(_dir() / (claim["id"] + ".json"))
    if req and req.get("status") == "running" and req.get("owner") == claim["owner"]:
        return req
    return None


def _heartbeat(claims) -> None:
    with stop_switch.dispatch_change():
        for claim in claims:
            req = _owned(claim)
            if req is not None:
                req.update(_lease())
                _save(req)


def _execute(claim: dict, log: Callable[[str], None]) -> None:
    log(f"{claim['id']} routing ({claim['access']}"
        + (f", agent named: {claim['agent']}" if claim.get("agent") else "") + ")")
    try:
        result = control.delegate(claim["brief"], agent=claim.get("agent"), cwd=claim["cwd"],
                                  timeout=claim.get("timeout"), access=claim["access"],
                                  files=claim.get("files") or None, meta=claim.get("meta") or None,
                                  skill=claim.get("skill"), use_skills=claim.get("use_skills", True),
                                  source="daemon", request_id=claim["id"])
        if result.gate in ("pending", "blocked"):
            status = "needs_review"
        elif result.status == "escalated":
            status = "needs_human"
        else:
            status = "done" if result.ok else "failed"
        changes = dict(result=result.to_dict(), task_id=result.task_id, agent=result.agent)
    except Exception as exc:  # the daemon must outlive any single request
        status = "failed"
        changes = {"result": {"status": "error", "error": f"{type(exc).__name__}: {exc}"}}

    with stop_switch.dispatch_change():
        req = _owned(claim)
        if req is None:
            state.audit("request.stale_completion", request_id=claim["id"], owner=claim["owner"])
            return
        if status == "needs_review" and changes["task_id"]:
            # A fast operator may close the patch while this request still
            # says running. close_review uses the same lock for the other order.
            try:
                review = tasks.load_task(changes["task_id"])
            except tasks.TaskError:
                pass
            else:
                if review["status"] in (tasks.APPLIED, tasks.DISCARDED):
                    status = "done"
        req.update(changes, status=status, lease_until=None, finished_at=state.now())
        _save(req)
        state.audit("request.finished", request_id=req["id"], task_id=req.get("task_id"),
                    owner=req["owner"], status=status)
    log(f"{req['id']} {status}"
        + (f": orchestra review {req['task_id']}" if status == "needs_review" else ""))


def _pending():
    return deque((req["id"], req["access"]) for req in all_requests()
                 if req.get("status") == "pending")


def work_once(log: Callable[[str], None] = print, *,
              stopping: Callable[[], bool] = lambda: False,
              on_poll: Optional[Callable[[], None]] = None) -> int:
    """Drain a snapshot, or admit new arrivals on each persistent daemon poll."""
    pending = _pending()
    if not pending:
        return 0
    try:
        settings = config.load().get("daemon", {})
    except config.ConfigError as exc:
        log(f"configuration problem, not running anything: {exc}")
        return 0
    workers = settings.get("workers", 1)
    poll_seconds = max(0.2, float(settings.get("poll_seconds", 2)))
    next_poll = time.monotonic() + poll_seconds
    next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
    running = {}
    ran = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="orchestra") as pool:
        while running or pending:
            if stopping() or stop_switch.is_stopped():
                pending.clear()
            while pending and len(running) < workers:
                ident, access = pending[0]
                # Reads go to isolated Docker inputs or the tool-free local
                # model. Edits may auto-apply a reviewed patch to a project.
                # Treat the first queued edit as a barrier, preserving FIFO.
                if running and (access != "read" or any(c["access"] != "read" for c in running.values())):
                    break
                if ran:
                    # The operator can edit policy between jobs in a long
                    # batch. Leave waiting work pending if that edit is invalid.
                    try:
                        config.load()
                    except config.ConfigError as exc:
                        log(f"configuration problem, not running anything: {exc}")
                        pending.clear()
                        break
                pending.popleft()
                claim = _claim(_dir() / (ident + ".json"), stopping)
                if claim is not None:
                    running[pool.submit(_execute, claim, log)] = claim
                    ran += 1
            if not running:
                break

            wake = min(next_heartbeat, next_poll) if on_poll else next_heartbeat
            finished, _ = wait(running, timeout=max(0, wake - time.monotonic()),
                               return_when=FIRST_COMPLETED)
            for future in finished:
                del running[future]
                future.result()
            now = time.monotonic()
            if now >= next_heartbeat:
                _heartbeat(running.values())
                next_heartbeat = now + _HEARTBEAT_SECONDS
            if on_poll and now >= next_poll:
                if not stopping() and not stop_switch.is_stopped():
                    on_poll()
                    pending = _pending()
                next_poll = now + poll_seconds
    return ran


def _legacy_daemon(pid_file: Path) -> bool:
    """An active pre-lock CLI daemon must finish before the upgrade takes over."""
    try:
        pid = int(pid_file.read_text())
        if pid <= 0 or pid == os.getpid():
            return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    process = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if process.returncode:
        return False
    try:
        argv = shlex.split(process.stdout)
    except ValueError:
        return False
    return any(Path(arg).name == "orchestra" and argv[i + 1] == "daemon"
               for i, arg in enumerate(argv[:-1]))


@contextmanager
def _daemon_lock():
    home = config.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid_file = home / "daemon.pid"
    # Keep a stable inode for the lifetime flock. The PID is informational
    # and can safely be removed before releasing the lock.
    with (home / "daemon.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SpoolError("a daemon is already running for this Orchestra home") from None
        if lock.tell() == 0:
            if _legacy_daemon(pid_file):
                raise SpoolError("a pre-upgrade daemon is running; stop it before starting this version")
            lock.write("orchestra\n")
            lock.flush()
        pid_file.write_text(str(os.getpid()))
        try:
            yield
        finally:
            pid_file.unlink(missing_ok=True)


def run(once: bool = False, log: Callable[[str], None] = print) -> None:
    from . import scheduler

    shutdown = threading.Event()

    def ask_to_stop(signum, frame):
        shutdown.set()

    def tick():
        if not shutdown.is_set():
            try:
                scheduler.enqueue_due(log=log)
            except config.ConfigError as exc:
                log(f"configuration problem, scheduler paused: {exc}")

    with _daemon_lock():
        saved_handlers = {}
        try:
            if not once:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    saved_handlers[sig] = signal.signal(sig, ask_to_stop)
            state.audit("daemon.started", pid=os.getpid())
            log(f"orchestra daemon started (pid {os.getpid()}), watching {_dir()}")
            recover(log)
            while not shutdown.is_set():
                tick()
                work_once(log, stopping=shutdown.is_set, on_poll=None if once else tick)
                if once or shutdown.is_set():
                    break
                try:
                    poll = float(config.load()["daemon"]["poll_seconds"])
                except (config.ConfigError, KeyError, TypeError, ValueError):
                    poll = 2.0
                shutdown.wait(max(0.2, poll))
        finally:
            for sig, handler in saved_handlers.items():
                signal.signal(sig, handler)
            state.audit("daemon.stopped", pid=os.getpid())
            log("orchestra daemon stopped")
