"""Memory that compounds, and can be repaired (book chapter 7).

Three places, by how much they are trusted and how often they are read:

  the bounded core   SOUL.md, MEMORY.md, USER.md. Sent with every brief. Only
                     a human puts anything there.
  memory records     memory.jsonl. Lessons with a source, a scope and a review
                     state. Fetched by relevance, never dumped.
  the archive        outcomes.jsonl. What happened, every time. Evidence, not
                     policy.

Each record carries what is needed to decide whether to believe it later
(7.4): type, scope, source, timestamp, confidence, review state, a summary
written for retrieval, a pointer to its evidence, and optionally a revisit date.

Write policy (7.5): anyone may add to the archive; a lesson starts as
`proposed`; only a person makes it `curated`, and only a person promotes
anything into the core. An agent can propose (through the MCP memory server)
but cannot edit what governs future briefs. Unreviewed proposals from agents
are not put into briefs at all until you accept them.

Read path (7.7): rank by fit, then filter by trust. Every brief records which
memories it used, so a bad brief can be traced to the record behind it and
that record quarantined (7.11).

memory.jsonl is the source data, in a boring format on purpose; any index is
derived and can be rebuilt.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime
from typing import List, Optional, Tuple

from . import config, outcomes, state

TYPES = ("fact", "preference", "procedure", "warning", "decision", "question", "outcome")
CONFIDENCE = ("observed", "inferred", "user-confirmed", "contradicted", "unknown")
STATES = ("proposed", "curated", "rejected", "stale", "quarantined")
USABLE = ("curated", "proposed")
MAX_SUMMARY = 600


class MemoryError_(Exception):
    pass


# Words of three letters or more; dots, dashes and slashes are kept inside a
# word so that file names such as app.py or src/billing.py match as a whole.
_WORD = re.compile(r"[a-z_][a-z0-9_]*(?:[./\-][a-z0-9_]+)*")
_STOP = set("""the and for add fix use new all any are not but can you its out our has was get set
this that with from into file files function what does code then them have every where
everything nothing change changes explaining explain please should would could there their
about which when will make sure only also each project keep""".split())


def tokens(text: str) -> set:
    out = set()
    for w in _WORD.findall(text.lower()):
        if len(w) < 3 or w in _STOP:
            continue
        # Crude plural folding, so "docstrings" meets "docstring".
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") and "." not in w else w)
    return out


def overlap(a: set, b: set) -> int:
    """A shared file name is strong evidence on its own; one shared ordinary
    word is not."""
    return sum(2 if ("." in w or "/" in w) else 1 for w in a & b)


def _path():
    return config.home() / "memory.jsonl"


def _legacy_notes() -> List[dict]:
    """Notes written before memory records existed (`orchestra note`, ch. 5)."""
    path = config.home() / "notes.jsonl"
    if not path.is_file():
        return []
    projects = {e.get("task_id"): e.get("project") for e in outcomes.read_all()}
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            n = json.loads(line)
            out.append({"id": f"note-{i + 1}", "type": "preference",
                        "scope": (f"project:{projects[n['task_id']]}" if projects.get(n["task_id"])
                                  else "unknown"), "source": "human",
                        "ts": n.get("ts", ""), "confidence": "user-confirmed", "state": "curated",
                        "summary": n["note"], "evidence": n["task_id"], "revisit": None,
                        "schema": 1, "legacy": True})
        except (ValueError, KeyError, TypeError):
            continue
    return out


def load(include_legacy: bool = True) -> List[dict]:
    out = _legacy_notes() if include_legacy else []
    path = _path()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
    return out


def _save(records: List[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in records if not r.get("legacy")), encoding="utf-8")
    os.replace(tmp, path)


def scope_for(project: Optional[str] = None, task_class: Optional[str] = None,
              everywhere: bool = False) -> str:
    if everywhere:
        return "global"
    if task_class:
        return f"class:{task_class}"
    if project:
        return f"project:{os.path.realpath(os.path.expanduser(project))}"
    raise MemoryError_("a memory needs a scope: a project, a task class, or global")


def add(summary: str, scope: str, type: str = "fact", source: str = "human",
        confidence: str = "user-confirmed", state_: str = "proposed",
        evidence: Optional[str] = None, revisit: Optional[str] = None) -> dict:
    from . import tasks  # late: tasks imports this module
    summary = " ".join(str(summary).split())
    if not summary:
        raise MemoryError_("a memory needs a summary")
    if len(summary) > MAX_SUMMARY:
        raise MemoryError_(f"keep a memory under {MAX_SUMMARY} characters; this is the retrieval "
                           "summary, not the full story")
    if type not in TYPES:
        raise MemoryError_(f"type must be one of {TYPES}")
    if confidence not in CONFIDENCE:
        raise MemoryError_(f"confidence must be one of {CONFIDENCE}")
    if state_ not in STATES:
        raise MemoryError_(f"state must be one of {STATES}")
    if revisit:
        try:
            date.fromisoformat(revisit)
        except ValueError:
            raise MemoryError_("revisit must be a date like 2027-02-01") from None
    leaks = tasks.find_secrets(summary)
    if leaks:
        raise MemoryError_("not saved: it contains " + ", ".join(leaks))
    rec = {"id": "m-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
           "type": type, "scope": scope, "source": source, "ts": state.now(),
           "confidence": confidence, "state": state_, "summary": summary,
           "evidence": evidence, "revisit": revisit, "schema": 1}
    _save(load(include_legacy=False) + [rec])
    state.audit("memory.added", memory_id=rec["id"], source=source, state=state_, scope=scope)
    return rec


def find(ref: str) -> dict:
    records = load()
    exact = [r for r in records if r["id"] == ref]
    matches = exact or [r for r in records if ref in r["id"]]
    if not matches:
        raise MemoryError_(f"no memory matching '{ref}'")
    if len(matches) > 1:
        raise MemoryError_(f"'{ref}' matches {len(matches)} memories; be more specific")
    return matches[0]


def set_state(ref: str, new_state: str, by: str = "operator") -> dict:
    """Review a memory. This is a human act: it is what makes a lesson trusted,
    or takes a bad one out of circulation."""
    if new_state not in STATES:
        raise MemoryError_(f"state must be one of {STATES}")
    target = find(ref)
    if target.get("legacy"):
        raise MemoryError_("this is an old-style note; it cannot be re-reviewed. "
                           "Add a new memory instead.")
    records = load(include_legacy=False)
    for r in records:
        if r["id"] == target["id"]:
            r["state"] = new_state
            r["reviewed"] = state.now()
            r["reviewed_by"] = by
            if new_state == "curated" and r["confidence"] in ("inferred", "unknown"):
                r["confidence"] = "user-confirmed"
            target = r
    _save(records)
    state.audit("memory.reviewed", memory_id=target["id"], state=new_state, by=by)
    return target


def promote_to_core(ref: str) -> dict:
    """Give a curated memory a permanent seat in MEMORY.md (7.3). Everything
    there is paid for on every single run, so this is deliberate and rare."""
    rec = find(ref)
    if rec["state"] != "curated":
        raise MemoryError_("only a curated memory can be promoted; accept it first")
    path = config.home() / "MEMORY.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    marker = f"<!-- orchestra-memory:{rec['id']} -->"
    if marker in existing:
        return rec
    if rec["summary"] in existing:
        used = []
        render_core(existing, used)
        if rec["id"] in used:
            return rec
        raise MemoryError_("MEMORY.md already contains this")
    with path.open("a", encoding="utf-8") as f:
        f.write(("" if existing.endswith("\n") or not existing else "\n") +
                f"- {rec['summary']} {marker}\n")
    state.audit("memory.promoted_to_core", memory_id=rec["id"])
    return rec


def render_core(text: str, used: List[str]) -> str:
    """Apply current review states without rewriting operator-authored Markdown."""
    records = {r["id"]: r for r in load(include_legacy=False)}
    lines = text.splitlines()
    # 0.4.1 wrote an unmarked bullet and a promotion audit event. Only
    # attribute a plain bullet if both pieces of evidence still exist.
    legacy = {}
    candidates = {f"- {r['summary']}": r["id"] for r in records.values() if "summary" in r}
    audit = config.home() / "logs" / "audit.jsonl"
    if candidates.keys() & set(lines) and audit.is_file():
        with audit.open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event") == "memory.promoted_to_core":
                    rec = records.get(event.get("memory_id"))
                    if rec and "summary" in rec and f"<!-- orchestra-memory:{rec['id']} -->" not in text:
                        legacy[f"- {rec['summary']}"] = rec["id"]
    rendered = []
    for line in lines:
        marker = re.search(r" <!-- orchestra-memory:([\w-]+) -->$", line)
        ident = marker.group(1) if marker else legacy.pop(line, None)
        if ident:
            if records.get(ident, {}).get("state") != "curated":
                continue
            used.append(ident)
        rendered.append(line[:marker.start()] if marker else line)
    return "\n".join(rendered)


def is_due(rec: dict) -> bool:
    try:
        return bool(rec.get("revisit")) and date.fromisoformat(rec["revisit"]) <= date.today()
    except ValueError:
        return False


def label(rec: dict) -> str:
    """How much to believe this, said plainly, so the agent can reason with
    the memory instead of being silently steered by it (7.7)."""
    if rec["state"] == "proposed":
        trust = "tentative, not yet reviewed"
    elif rec["confidence"] == "user-confirmed":
        trust = "confirmed by the operator"
    else:
        trust = "reviewed"
    bits = [trust, rec.get("ts", "")[:10]]
    if is_due(rec):
        bits.append("due for re-checking")
    return ", ".join(b for b in bits if b)


def search(task: str, project: Optional[str], task_class: Optional[str], limit: int,
           threshold: int = 2) -> List[Tuple[dict, int]]:
    """Rank by fit, then filter by trust (7.7). Returns (record, score)."""
    if limit <= 0:
        return []
    want = tokens(task)
    past = {e.get("task_id"): str(e.get("brief") or "") for e in outcomes.read_all()}
    scopes = {"global"}
    if project:
        scopes.add(f"project:{os.path.realpath(project)}")
    if task_class:
        scopes.add(f"class:{task_class}")
    found = []
    for rec in load():
        if rec["state"] not in USABLE or rec["scope"] not in scopes:
            continue
        # An agent's unreviewed proposal never steers a brief. A person's does,
        # labelled as tentative.
        if rec["state"] == "proposed" and not str(rec.get("source", "")).startswith("human"):
            continue
        about = tokens(rec["summary"]) | tokens(past.get(rec.get("evidence"), ""))
        score = overlap(want, about)
        if score >= threshold:
            trust = (rec["state"] == "curated") + (rec["confidence"] == "user-confirmed")
            found.append((score, trust, rec.get("ts", ""), rec))
    found.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return [(rec, score) for score, _, _, rec in found[:limit]]


def used_by(memory_id: str) -> List[dict]:
    """Which runs were briefed with this memory: the trail for 7.11."""
    return [e for e in outcomes.read_all() if memory_id in (e.get("memories_used") or [])]
