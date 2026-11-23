"""The parts of delegation that are not "run the agent" (book chapter 5).

  assemble()      the brief: operator context, project guidance, what we
                  already know, the files in scope, then the task (5.2)
  find_secrets()  the check that keeps credentials out of a brief (5.6, 5.7)
  prepare_copy()  a private copy of the project for an edit-level run
  make_patch()    the agent's changes as a diff
  apply()/discard()  the human gate: nothing lands until a person says so (5.3)

Every task gets a folder, ~/.orchestra/tasks/<task-id>/, holding exactly what
was sent (brief.md), what came back (result.md) and, for edit runs, the diff
(changes.diff), the review state (task.json) and the working copy (repo/).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import config, memory, outcomes, state, workspace

PENDING, APPLIED, DISCARDED, BLOCKED = "pending", "applied", "discarded", "blocked"


class TaskError(Exception):
    pass


def new_id() -> str:
    return "t-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def task_dir(task_id: str) -> Path:
    return config.home() / "tasks" / task_id


def save_text(task_id: str, name: str, text: str) -> Path:
    path = task_dir(task_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- secrets ------------------------------------------------------------------

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_SECRET_SHAPES = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "an Anthropic API key"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}"), "an API key (sk-...)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "a Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
]


def find_secrets(text: str) -> List[str]:
    """Describe anything in `text` that looks like a credential. Returns
    descriptions only, never the values."""
    found = []
    for name, value in config.load_secrets().items():
        if len(value) >= 8 and value in text:
            found.append(f"the value of {name} from .env")
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and len(value) >= 12 and value in text:
            found.append(f"the value of the environment variable {name}")
    for pattern, label in _SECRET_SHAPES:
        if pattern.search(text):
            found.append(label)
    return list(dict.fromkeys(found))


# --- the brief ----------------------------------------------------------------

def add_note(task_ref: str, note: str) -> str:
    """A lesson you attach to a past task. It becomes a curated memory record,
    scoped to that task's project, confirmed by you."""
    by_id = {e["task_id"]: e for e in outcomes.read_all() if e.get("task_id")}
    matches = [i for i in by_id if task_ref in i]
    if task_ref in by_id:
        matches = [task_ref]
    if len(matches) != 1:
        raise TaskError(f"'{task_ref}' matches {len(matches)} tasks; give one task id")
    try:
        memory.add(note, scope=memory.scope_for(project=by_id[matches[0]].get("project")),
                   type="preference", source="human", confidence="user-confirmed",
                   state_="curated", evidence=matches[0])
    except memory.MemoryError_ as e:
        raise TaskError(str(e)) from None
    return matches[0]


def relevant(task: str, project: str, limit: int, task_class: Optional[str] = None,
             threshold: int = 2) -> Tuple[List[str], List[str]]:
    """What the coordinator already knows that bears on this task (7.7):
    reviewed lessons first, then earlier runs in the same project. Returns
    (lines for the brief, ids of the memory records used).

    Lessons are things a person wrote or accepted. Earlier runs are described
    by what was asked and how it ended, never by what the agent said: feeding
    agent output back into briefs unreviewed is how a memory gets poisoned.
    """
    if limit <= 0:
        return [], []
    lines, used = [], []
    for rec, _ in memory.search(task, project, task_class, limit, threshold):
        lines.append(f"- [{memory.label(rec)}] {rec['summary']}")
        used.append(rec["id"])

    want, best = memory.tokens(task), {}
    for e in outcomes.read_all():
        if e.get("project") != project or e.get("source") == "bench" \
                or e.get("status") in ("denied", "escalated"):
            continue
        past = str(e.get("brief") or "")
        score = memory.overlap(want, memory.tokens(past)
                               | memory.tokens(" ".join(e.get("files_in_scope") or [])))
        if score >= threshold:
            best[past] = (score, e.get("ts", ""), e)  # the latest run of a task wins
    for score, ts, e in sorted(best.values(), key=lambda x: (x[0], x[1]), reverse=True)[:limit]:
        short = " ".join(str(e.get("brief", "")).split())
        if len(short) > 160:
            short = short[:157] + "..."
        how = e.get("status", "?")
        if e.get("error") and how != "ok":
            how += " (" + " ".join(str(e["error"]).split())[:120] + ")"
        lines.append(f"- [earlier run, {ts[:10]}, {e.get('agent')}] \"{short}\" ended {how}")
    return lines, used


def guidance(project: str, adapter, cap: int) -> Tuple[Optional[str], Optional[str], str]:
    """(file name, text, remark). One guidance file should brief every agent,
    so if the agent's own file is missing we use whichever trio file exists."""
    root = Path(project)
    own = adapter.context_file
    if own and adapter.auto_reads_context and (root / own).is_file():
        return own, None, "read by the agent itself, so not repeated in the brief"
    for name in [own] + [n for n in workspace.CONTEXT_FILES if n != own]:
        if name and (root / name).is_file():
            text = (root / name).read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            remark = ""
            if len(text) > cap:
                text = text[:cap].rstrip() + f"\n[... {name} truncated to {cap} characters by orchestra ...]"
                remark = "truncated"
            return name, text, remark
    return None, None, "no guidance file in this project"


@dataclass
class Brief:
    text: str
    sections: List[Tuple[str, int, str]] = field(default_factory=list)  # (name, chars, remark)
    over_budget: bool = False
    memories_used: List[str] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


MAX_INLINE = 40000  # characters of file content handed to a tool-less local model


def assemble(task: str, adapter, project: str, scope: List[str], cfg: dict,
             task_class: Optional[str] = None, procedure: Optional[Tuple[str, str]] = None) -> Brief:
    """Relevance under a budget (book 5.2). Context first, the task last. With
    no context at all, the brief is just the task.

    `procedure` is (skill name and version, instructions) when a skill shapes
    this delegation (chapter 8).
    """
    parts, sections = [], []

    def add(name: str, text: Optional[str], remark: str = ""):
        if text:
            parts.append(text)
        sections.append((name, len(text or ""), remark))

    core_ids = []
    core = state.bounded_memory(core_ids)
    add("operator context (SOUL/MEMORY/USER)",
        "\n\n".join(["<operator-context>"] + [f"## {n}\n{t}" for n, t in core]
                    + ["</operator-context>"]) if core else None)

    name, text, remark = guidance(project, adapter, int(cfg["brief"]["guidance_max_chars"]))
    add(f"project guidance ({name})" if name else "project guidance",
        f"# Project guidance (from {name})\n{text}" if text else None, remark)

    if procedure:
        add(f"procedure ({procedure[0]})", f"# Procedure (skill: {procedure[0]})\n{procedure[1]}")

    known, used = relevant(task, project, int(cfg["brief"]["memory_items"]), task_class)
    used = list(dict.fromkeys(core_ids + used))
    add("what we already know",
        "# What we already know\nFrom this control plane's memory. Entries the operator "
        "confirmed are standing guidance; tentative entries and earlier runs are background.\n"
        + "\n".join(known) if known else None)

    add("files in scope",
        "# Files in scope\n" + "\n".join(f"- {f}" for f in scope)
        + "\nWork only in these files. If the task cannot be done without touching others, "
          "stop and say which and why." if scope else None,
        "" if scope else "none named; the agent may look anywhere in the project")

    if adapter.needs_inline_files:
        # A local model has no tools, so it is handed the files themselves.
        # That is acceptable only because nothing here leaves the machine.
        chunks, room = [], MAX_INLINE
        for f in scope:
            path = Path(project) / f
            if not path.is_file() or room <= 0:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")[:room]
            room -= len(body)
            chunks.append(f"## {f}\n```\n{body}\n```")
        add("file contents (local model only)",
            "# File contents\n" + "\n\n".join(chunks) if chunks else None,
            "" if chunks else "name files with --file; a local model cannot open them itself")

    if not parts:
        return Brief(task, sections + [("task", len(task), "")], memories_used=used)
    text = "\n\n".join(parts + [f"Task:\n{task}"])
    sections.append(("task", len(task), ""))
    return Brief(text, sections, over_budget=len(text) > int(cfg["brief"]["max_chars"]),
                 memories_used=used)


def resolve_scope(files: Optional[List[str]], cwd: str, project: str) -> List[str]:
    """Turn --file arguments into project-relative paths; refuse anything
    outside the project. A file that does not exist yet is allowed (the task
    may be to create it)."""
    scope = []
    root = Path(project).resolve()
    for f in files or []:
        path = (Path(cwd) / os.path.expanduser(f)).resolve()
        try:
            rel = path.relative_to(root)
        except ValueError:
            raise TaskError(f"--file {f} is outside the project ({root})") from None
        if path.is_dir():
            raise TaskError(f"--file {f} is a directory; name the files themselves")
        scope.append(rel.as_posix())
    return list(dict.fromkeys(scope))


# --- review, then apply ---------------------------------------------------------

def _git(cwd, *args, check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-c", "user.name=orchestra", "-c", "user.email=orchestra@localhost",
                        "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise TaskError(f"git {args[0]} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def prepare_copy(project: str, task_id: str) -> Tuple[Path, str]:
    """Copy the project, uncommitted work included, and mark the starting
    point so the agent's changes can be told apart from yours."""
    copy = task_dir(task_id) / "repo"
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project, copy, symlinks=True)
    _git(copy, "add", "-A")
    _git(copy, "commit", "-q", "--allow-empty", "--no-verify", "-m", "orchestra: starting point")
    return copy, _git(copy, "rev-parse", "HEAD").stdout.strip()


def make_patch(copy: Path, baseline: str) -> Tuple[str, List[str]]:
    """Everything the agent changed since the starting point, whether or not
    it committed: (diff text, files touched)."""
    _git(copy, "add", "-A")
    patch = _git(copy, "diff", "--cached", "--binary", baseline).stdout
    names = _git(copy, "diff", "--cached", "--name-only", baseline).stdout.split("\n")
    return patch, [n for n in names if n]


def save_task(task_id: str, record: dict) -> dict:
    record["updated"] = state.now()
    path = task_dir(task_id) / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return record


def all_tasks() -> List[dict]:
    out = []
    root = config.home() / "tasks"
    if root.is_dir():
        for path in sorted(root.glob("*/task.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def load_task(ref: str) -> dict:
    tasks = all_tasks()
    exact = [t for t in tasks if t["task_id"] == ref]
    matches = exact or [t for t in tasks if ref in t["task_id"]]
    if not matches:
        raise TaskError(f"no reviewable task matching '{ref}'")
    if len(matches) > 1:
        raise TaskError(f"'{ref}' matches {len(matches)} tasks; be more specific")
    return matches[0]


def patch_text(task: dict) -> str:
    path = Path(task["patch"])
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def apply(ref: str, anyway: bool = False, decided_by: str = "operator") -> dict:
    """Land a reviewed diff in the real project, as uncommitted changes. You
    still gate the commit. Nothing is applied unless the whole diff applies."""
    task = load_task(ref)
    if task["status"] == BLOCKED and not anyway:
        raise TaskError(f"{task['task_id']} failed validation ({'; '.join(task.get('problems') or [])}). "
                        f"Read the diff first; `orchestra apply {task['task_id']} --anyway` overrides.")
    if task["status"] not in (PENDING, BLOCKED):
        raise TaskError(f"{task['task_id']} is already {task['status']}")
    project = task["project"]
    if not Path(project).is_dir():
        raise TaskError(f"the project is no longer at {project}")
    check = _git(project, "apply", "--check", task["patch"], check=False)
    if check.returncode != 0:
        raise TaskError("the diff no longer applies cleanly; the project has changed since the agent "
                        f"ran. Nothing was applied.\n{check.stderr.strip()}\n"
                        f"The diff is at {task['patch']}")
    _git(project, "apply", task["patch"])
    task["status"] = APPLIED
    state.audit("review.applied", task_id=task["task_id"], project=project, decided_by=decided_by)
    return save_task(task["task_id"], task)


def discard(ref: str) -> dict:
    task = load_task(ref)
    if task["status"] not in (PENDING, BLOCKED):
        raise TaskError(f"{task['task_id']} is already {task['status']}")
    task["status"] = DISCARDED
    state.audit("review.discarded", task_id=task["task_id"], project=task["project"])
    return save_task(task["task_id"], task)
