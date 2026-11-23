"""Recurring jobs feed the same durable, unattended queue as manual submissions."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone as tz
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, spool, state, stop_switch


class SchedulerError(ValueError):
    pass


def _path(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise SchedulerError("schedule name must be 1–64 lowercase letters, digits, dashes or underscores")
    return config.home() / "cron" / (name + ".json")


@contextmanager
def _lock():
    home = config.home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (home / "scheduler.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _utc(value):
    if value is None:
        return datetime.now(tz.utc)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise SchedulerError("schedule time must have a UTC offset")
    return value.astimezone(tz.utc)


def _cadence(every, daily, timezone):
    if (every is None) == (daily is None):
        raise SchedulerError("choose one of every or daily")
    if every is not None and (type(every) is not int or every <= 0):
        raise SchedulerError("every must be a positive integer number of seconds")
    if daily is not None and (not isinstance(daily, str)
                              or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", daily)):
        raise SchedulerError("daily must be a 24-hour time in HH:MM format")
    try:
        return ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise SchedulerError("unknown IANA time zone") from None


def _next(job, now, due=None):
    zone = _cadence(job["every"], job["daily"], job["timezone"])
    if job["every"] is not None:
        step = timedelta(seconds=job["every"])
        if due is None:
            return now + step
        return due + ((now - due) // step + 1) * step
    day = now.astimezone(zone).date()
    clock = time.fromisoformat(job["daily"])
    while True:
        # Choose the first occurrence on a fall-back day, and skip a
        # nonexistent spring-forward time (a UTC round trip changes it).
        local = datetime.combine(day, clock, tzinfo=zone)
        instant = local.astimezone(tz.utc)
        if instant > now and instant.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            return instant
        day += timedelta(days=1)


def _save(job):
    path = _path(job["name"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(job, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def _read(path):
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["version"] != 1 or _path(job["name"]) != path:
            raise SchedulerError("unexpected version or name")
        if not isinstance(job["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
            raise SchedulerError("invalid schedule ID")
        _cadence(job["every"], job["daily"], job["timezone"])
        _utc(datetime.fromisoformat(job["next_due"]))
        if type(job["paused"]) is not bool or not isinstance(job["request"], dict):
            raise SchedulerError("invalid pause state or request")
        last = job["last_request_id"]
        if last is not None and (not isinstance(last, str) or not last):
            raise SchedulerError("invalid previous request ID")
        inherited = job.get("predecessors", [])
        if not isinstance(inherited, list) or not all(isinstance(i, str) and i for i in inherited):
            raise SchedulerError("invalid predecessor request IDs")
        request = job["request"]
        if (not {"brief", "cwd", "access"} <= request.keys()
                or not isinstance(request["brief"], str)
                or not isinstance(request["cwd"], str) or not Path(request["cwd"]).is_absolute()
                or not isinstance(request.get("meta", {}), dict)
                or not isinstance(request.get("files", []), list)
                or request.keys() - {"brief", "cwd", "access", "agent", "skill", "files", "timeout",
                                     "meta", "use_skills", "replay_safe"}):
            raise SchedulerError("invalid scheduled request")
        return job
    except FileNotFoundError:
        raise SchedulerError(f"no schedule named {path.stem}") from None
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise SchedulerError(f"invalid schedule {path.name}: {exc}") from None


def add(name, brief, *, every=None, daily=None, timezone="UTC", cwd=None, skill=None,
        agent=None, access=None, files=None, timeout=None, meta=None, use_skills=True,
        replay_safe=False, now=None):
    path = _path(name)
    job = {"version": 1, "id": uuid.uuid4().hex, "name": name,
           "every": every, "daily": daily, "timezone": timezone,
           "paused": False, "last_request_id": None}
    try:
        job["next_due"] = _next(job, _utc(now)).isoformat()
    except OverflowError:
        raise SchedulerError("schedule interval is outside the supported date range") from None
    job["request"] = spool.prepare_submission(brief, agent=agent, cwd=cwd, access=access,
                                             timeout=timeout, files=files, meta=meta, skill=skill,
                                             use_skills=use_skills, replay_safe=replay_safe)
    with _lock():
        if path.exists():
            raise SchedulerError(f"schedule {name} already exists")
        # Removing a definition retires its clock. Requests it already
        # produced still belong to the queue when the operator reuses a name.
        predecessors = [req["id"] for req in spool.all_requests() if req.get("schedule") == name
                        and (req.get("status") in spool.OPEN or req.get("status") == "interrupted")]
        if predecessors:
            job["predecessors"] = predecessors
        _save(job)
    state.audit("schedule.added", schedule=name, next_due=job["next_due"])
    return job


def _jobs(log):
    for path in sorted((config.home() / "cron").glob("*.json")):
        try:
            yield _read(path)
        except SchedulerError as exc:
            log(str(exc))


def list_jobs():
    return list(_jobs(lambda message: print(message, file=sys.stderr)))


def _set_paused(name, paused):
    with _lock():
        job = _read(_path(name))
        job["paused"] = paused
        _save(job)
    state.audit("schedule.paused" if paused else "schedule.resumed", schedule=name)
    return job


def pause(name):
    return _set_paused(name, True)


def resume(name):
    return _set_paused(name, False)


def remove(name):
    with _lock():
        path = _path(name)
        if not path.is_file():
            raise SchedulerError(f"no schedule named {name}")
        try:
            job = _read(path)
        except SchedulerError:
            # Operators can retire a damaged definition from the same CLI.
            job = {"name": name}
        path.unlink()
    state.audit("schedule.removed", schedule=name)
    return job


def _overlaps(previous):
    if previous["status"] in spool.OPEN:
        return True
    # A dead coordinator leaves its Docker client registration behind. These
    # registrations identify the home, not the request. A successful stop
    # and resume cleans the whole prior generation, even if new skills start.
    return (previous["status"] == "interrupted" and previous["access"] == "read"
            and not previous.get("agent") and (previous.get("skill") or previous.get("use_skills", True))
            and previous.get("stop_generation", "") == stop_switch.generation()
            and any((config.home() / "active-skills").glob("*.socket")))


def enqueue_due(log=print, now=None):
    """Coalesce missed slots and publish at most one occurrence per job and tick."""
    if config.load()["scheduler"]["enabled"] is not True or stop_switch.is_stopped():
        return 0
    now = _utc(now)
    count = 0
    with _lock():
        for job in _jobs(log):
            due = _utc(datetime.fromisoformat(job["next_due"]))
            if job["paused"] or due > now:
                continue
            try:
                next_due = _next(job, now, due).isoformat()
                previous_ids = list(job.get("predecessors", []))
                if job["last_request_id"]:
                    previous_ids.append(job["last_request_id"])
                with stop_switch.admission():
                    busy = next((ident for ident in previous_ids if _overlaps(spool.load(ident))), None)
                    if busy:
                        job["next_due"] = next_due
                        _save(job)
                        state.audit("schedule.overlap_skipped", schedule=job["name"], request_id=busy)
                        continue
                # Use the persisted slot, even if a later poll coalesces more
                # elapsed intervals. Repeating submit after a crash finds the
                # original request before we advance this job's clock.
                key = "schedule." + job["id"] + "." + due.strftime("%Y%m%dT%H%M%S%fZ")
                req = spool.submit(**job["request"], idempotency_key=key, schedule=job["name"])
                with stop_switch.admission():
                    job.pop("predecessors", None)
                    job["last_request_id"] = req["id"]
                    job["next_due"] = next_due
                    _save(job)
                count += 1
                state.audit("schedule.enqueued", schedule=job["name"], request_id=req["id"], due=due.isoformat())
                log(f'{job["name"]} queued {req["id"]}')
            except stop_switch.Stopped:
                break
            except (spool.SpoolError, TypeError, ValueError, KeyError, OverflowError, OSError) as exc:
                log(f'{job["name"]} could not schedule work: {exc}')
    return count
