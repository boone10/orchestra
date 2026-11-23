"""The routing engine (book chapter 6) and the local-vs-cloud gate (chapter 10).

A task has four properties that decide how it is handled:

  stakes    low | med | high      how bad, and how irreversible, if it goes wrong
  context   small | large         how much must be held in mind at once
  privacy   P0 .. P4              may the data leave this machine?
  autonomy  supervised | unattended

and the router turns them into one of three outcomes:

  act-direct      handled here, by a local model or a trusted native skill
  delegate        handed to a specific cloud agent, chosen by fit and evidence
  escalate-human  a person authorizes the consequence. Agents may still
                  prepare (inspect, draft a diff); nothing is applied for them.

The order is the policy (listing 6.1, section 10.6):

  1. Stakes. High stakes always escalates. No other axis can override it.
  2. Privacy. P4 never leaves the machine. P3 stays local unless a named,
     logged exception exists. P2 goes only to approved providers.
  3. Then size and autonomy pick among what the first two allow, the outcome
     record adjusts the choice, and the budget has the last word on spend.

Classification is declaration first (book 6.5). What you state is trusted;
what is inferred always rounds toward caution and says so. The whole thing is
rules you can read, on purpose: a router you cannot audit is not one you can
give autonomy. `orchestra route "<task>"` prints a decision without running it.

Decision and enforcement are separate (10.6). The router decides; allowed()
is the wall, and the delegation primitive calls it again just before anything
is sent, so a routing bug cannot become a privacy incident.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import adapters, config, outcomes
from .contract import AUTONOMY, CONTEXT, EDIT, OK, PRIVACY, READ, STAKES

ACT, DELEGATE, ESCALATE = "act-direct", "delegate", "escalate-human"

# Wording and paths that suggest consequence. Used only to round stakes UP when
# nothing was declared; never to lower a declared value.
_HIGH_WORDS = re.compile(
    r"\b(payments?|billing|refunds?|payroll|production|prod|deploy(ment|s|ed|ing)?|release|"
    r"publish|schema|drop|delete|credentials?|secrets?|passwords?|api keys?|auth(entication|orization)?|"
    r"security|permissions?|iam|ci/?cd|pipeline|terraform|kubernetes|k8s|infra(structure)?|dns|"
    r"force[- ]push|rm -rf)\b", re.IGNORECASE)
_HIGH_PATHS = re.compile(
    r"(^|/)(\.github/workflows|deploy|infra|terraform|migrations?|payments?|billing|auth|k8s|helm)(/|$)"
    r"|(^|/)\.env", re.IGNORECASE)
_LARGE_WORDS = re.compile(r"\b(refactor|restructure|redesign|across|entire|whole|every|all files|"
                          r"codebase|architecture|multi-file)\b", re.IGNORECASE)

# First match wins, so the more specific kinds of work come first.
_CLASSES = [
    ("summarize", r"\b(summari[sz]e|summary|explain|describe|what does)\b"),
    ("review", r"\b(review|audit|inspect|check)\b"),
    ("research", r"\b(research|investigate|compare|find out|look up)\b"),
    ("test", r"\b(tests?|pytest|failing|coverage)\b"),
    ("docs", r"\b(docs?|readme|documentation|docstrings?|comments?|changelog)\b"),
    ("refactor", r"\b(refactor|rename|restructure|extract|migrate|clean ?up)\b"),
    ("bugfix", r"\b(fix|bug|error|crash|broken)\b"),
    ("feature", r"\b(add|implement|create|build|support)\b"),
]


# --- project tags: privacy is declared where the work lives --------------------

def _projects_path() -> Path:
    return config.home() / "projects.yaml"


def projects() -> dict:
    path = _projects_path()
    if not path.is_file():
        return {}
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise config.ConfigError(f"{path} is not valid YAML: {e}") from None
    return data if isinstance(data, dict) else {}


def set_project(path: str, privacy: Optional[str] = None, stakes: Optional[str] = None,
                autonomy: Optional[str] = None) -> Tuple[str, dict]:
    import yaml
    root = str(Path(os.path.expanduser(path)).resolve())
    for value, valid, name in ((privacy, PRIVACY, "privacy"), (stakes, STAKES, "stakes"),
                               (autonomy, AUTONOMY, "autonomy")):
        if value is not None and value not in valid:
            raise ValueError(f"{name} must be one of {valid}")
    data = projects()
    tags = dict(data.get(root) or {})
    tags.update({k: v for k, v in (("privacy", privacy), ("stakes", stakes),
                                   ("autonomy", autonomy)) if v is not None})
    tags["tagged"] = datetime.now().strftime("%Y-%m-%d")
    data[root] = tags
    _projects_path().parent.mkdir(parents=True, exist_ok=True)
    _projects_path().write_text(
        "# Declared by you with `orchestra project set`. One entry per project folder;\n"
        "# a tag covers everything beneath that folder.\n" + yaml.safe_dump(data, sort_keys=True),
        encoding="utf-8")
    return root, tags


def project_tags(project: Optional[str]) -> dict:
    """Tags of the closest tagged folder at or above `project`."""
    if not project:
        return {}
    here = Path(project).resolve()
    best, best_len = {}, -1
    for root, tags in projects().items():
        try:
            here.relative_to(Path(root))
        except ValueError:
            continue
        if len(str(root)) > best_len and isinstance(tags, dict):
            best, best_len = tags, len(str(root))
    return best


# --- classification ---------------------------------------------------------------

def _axis(value, declared_by: str, confidence: str, note: str = "") -> dict:
    out = {"value": value, "declared_by": declared_by, "confidence": confidence}
    if note:
        out["note"] = note
    return out


def infer_class(task: str) -> str:
    for name, pattern in _CLASSES:
        if re.search(pattern, task, re.IGNORECASE):
            return name
    return "general"


def classify(task: str, meta: Optional[dict], project: Optional[str], scope: List[str],
             source: str, cfg: dict) -> dict:
    """Give a task its values on the four axes, and say where each came from."""
    meta = {k: v for k, v in (meta or {}).items() if v is not None}
    for name, valid in (("stakes", STAKES), ("privacy", PRIVACY), ("context", CONTEXT),
                        ("autonomy", AUTONOMY)):
        if name in meta and meta[name] not in valid:
            raise ValueError(f"{name} must be one of {valid}, not {meta[name]!r}")
    tags = project_tags(project)
    by = meta.get("declared_by", "operator")

    # Privacy: declared, or the project's tag, or assumed sensitive.
    if "privacy" in meta:
        privacy = _axis(meta["privacy"], by, "high")
    elif tags.get("privacy") in PRIVACY:
        privacy = _axis(tags["privacy"], "project", "high")
    else:
        privacy = _axis(cfg["routing"]["untagged_privacy"], "default", "low",
                        "no privacy class declared for this project; treated as sensitive")

    # Stakes: declared, else rounded up on any sign of consequence.
    words = sorted({m.group(0).lower() for m in _HIGH_WORDS.finditer(task)})
    paths = [f for f in scope if _HIGH_PATHS.search(f)]
    signal = ", ".join(words + paths)
    if "stakes" in meta:
        note = (f"the wording suggests high stakes ({signal}); the declared value stands"
                if signal and meta["stakes"] != "high" else "")
        stakes = _axis(meta["stakes"], by, "high", note)
    elif signal:
        stakes = _axis("high", "inferred", "low", f"rounded up because the task mentions: {signal}")
    elif tags.get("stakes") in STAKES:
        stakes = _axis(tags["stakes"], "project", "high")
    else:
        stakes = _axis("med", "default", "low", "not declared; assumed to need a human look")

    # Context size: a preference, safe to estimate.
    if "context" in meta:
        context = _axis(meta["context"], by, "high")
    elif len(scope) > 3 or _LARGE_WORDS.search(task):
        context = _axis("large", "inferred", "low")
    else:
        context = _axis("small", "inferred", "low")

    # Autonomy: declared, else from how the request arrived.
    if "autonomy" in meta:
        autonomy = _axis(meta["autonomy"], by, "high")
    elif tags.get("autonomy") in AUTONOMY:
        autonomy = _axis(tags["autonomy"], "project", "high")
    else:
        autonomy = _axis("unattended" if source == "daemon" else "supervised", "source", "high",
                         f"arrived via {source}")

    if meta.get("task_class"):
        task_class = _axis(str(meta["task_class"]), by, "high")
    else:
        task_class = _axis(infer_class(task), "inferred", "low")
    return {"stakes": stakes, "privacy": privacy, "context": context, "autonomy": autonomy,
            "task_class": task_class}


# --- evidence and budgets, both read from the outcome log ---------------------------

def evidence(task_class: str) -> dict:
    """How each agent has actually done on this class of task (book 6.8)."""
    seen = {}
    for e in outcomes.read_all():
        if e.get("task_class") != task_class or e.get("source") == "bench":
            continue
        if e.get("status") in ("denied", "escalated"):
            continue  # never ran; says nothing about the agent
        a = seen.setdefault(e.get("agent"), {"runs": 0, "ok": 0, "cost": 0.0, "costed": 0})
        a["runs"] += 1
        a["ok"] += e.get("status") == OK
        if isinstance(e.get("cost_usd"), (int, float)):
            a["cost"] += e["cost_usd"]
            a["costed"] += 1
    for a in seen.values():
        a["rate"] = a["ok"] / a["runs"] if a["runs"] else None
        a["avg_cost"] = a["cost"] / a["costed"] if a["costed"] else None
    return seen


def spend_this_month(task_class: Optional[str] = None) -> float:
    month = datetime.now().strftime("%Y-%m")
    total = 0.0
    for e in outcomes.read_all():
        if not str(e.get("ts", "")).startswith(month):
            continue
        if task_class is not None and e.get("task_class") != task_class:
            continue
        if isinstance(e.get("cost_usd"), (int, float)):
            total += e["cost_usd"]
    return total


def budget_state(task_class: str, cfg: dict) -> Optional[dict]:
    """None if there is room; otherwise which cap is spent and what to do."""
    b = cfg.get("budgets") or {}
    cap = b.get("global_monthly_usd")
    if isinstance(cap, (int, float)) and spend_this_month() >= cap:
        return {"cap": f"global ${cap:g}/month", "on_exhaustion": "refuse"}
    for entry in b.get("classes") or []:
        if entry.get("task_class") == task_class and isinstance(entry.get("monthly_usd"), (int, float)):
            if spend_this_month(task_class) >= entry["monthly_usd"]:
                return {"cap": f"{task_class} ${entry['monthly_usd']:g}/month",
                        "on_exhaustion": entry.get("on_exhaustion", "refuse")}
    return None


# --- the wall ---------------------------------------------------------------------------

def allowed(adapter, privacy: str, task_class: str, cfg: dict) -> Tuple[bool, str]:
    """May a brief of this privacy class be given to this agent? This is the
    enforcement half: dumb, absolute, and checked again at the moment of sending."""
    if adapter.location == "local":
        return True, "stays on this machine"
    if privacy == "P4":
        return False, "P4 never leaves the machine"
    if privacy == "P3":
        for ex in cfg["routing"].get("p3_exceptions") or []:
            if isinstance(ex, dict) and ex.get("agent") == adapter.name \
                    and ex.get("task_class") == task_class and ex.get("approved_by"):
                return True, (f"P3 exception for class '{task_class}' approved by {ex['approved_by']}"
                              + (f": {ex['reason']}" if ex.get("reason") else ""))
        return False, "P3 stays local unless a named exception exists"
    if privacy == "P2":
        if adapter.name in (cfg["routing"].get("approved_providers") or []):
            return True, "approved provider for P2"
        return False, "P2 goes only to approved providers (routing.approved_providers)"
    return True, f"{privacy} may go to a cloud agent"


# --- the decision ---------------------------------------------------------------------

def decide(axes: dict, access: str, cfg: dict, requested: Optional[str] = None,
           native_skill: Optional[str] = None) -> dict:
    stakes, privacy = axes["stakes"]["value"], axes["privacy"]["value"]
    context, autonomy = axes["context"]["value"], axes["autonomy"]["value"]
    task_class = axes["task_class"]["value"]
    reasons, blocked = [], []

    # 1. Stakes: the override that beats everything else.
    prepare_only = stakes == "high"
    if prepare_only:
        reasons.append("hard rule: high stakes. A human authorizes the consequence; "
                       "an agent may only prepare")

    # 2. Privacy decides who may even be considered.
    eligible = []
    for name, adapter in adapters.REGISTRY.items():
        ok, why = allowed(adapter, privacy, task_class, cfg)
        if not ok:
            blocked.append(f"{name}: {why}")
        elif access == EDIT and not adapter.can_edit:
            blocked.append(f"{name}: cannot edit files")
        elif not adapter.available():
            blocked.append(f"{name}: not installed or not running")
        else:
            eligible.append(name)
            if adapter.location == "cloud" and privacy == "P3":
                reasons.append(f"{name}: {why}")
    cloud = [n for n in eligible if adapters.REGISTRY[n].location == "cloud"]
    local = [n for n in eligible if adapters.REGISTRY[n].location == "local"]
    if privacy in ("P3", "P4"):
        reasons.append(f"privacy {privacy}: local only" if not cloud
                       else f"privacy {privacy}: local by default")

    def fit(key: str) -> Optional[str]:
        return next((n for n in cloud if key in adapters.REGISTRY[n].fit), None)

    # 3. Size and autonomy choose among what is left.
    executor = None
    if requested:
        if requested in eligible:
            executor = requested
            reasons.append(f"the operator named {requested}")
        else:
            reasons.append(f"the operator named {requested}, which may not take this task")
    elif native_skill:
        executor = f"skill:{native_skill}"
        reasons.append(f"a trusted native skill matches: {native_skill}")
    elif context == "large" and cloud:
        executor = fit("large_context") or cloud[0]
        reasons.append("large context needs strong reasoning")
    elif autonomy == "unattended" and cloud:
        executor = next((n for n in cloud if adapters.REGISTRY[n].sandboxed), None) or cloud[0]
        reasons.append("unattended run suits an agent that sandboxes itself")
    elif local:
        executor = local[0]
        reasons.append("small and bounded: handled on the local model")
    elif cloud:
        executor = fit("supervised") or cloud[0]
        reasons.append("the local model cannot edit files, so it is delegated"
                       if any(b.startswith("local: cannot edit") for b in blocked)
                       else "no local model is running, so it is delegated")
    if executor is None and not requested and local:
        executor = local[0]
        reasons.append("only the local model may see this data")

    # Evidence sharpens the choice between cloud agents (6.8). It never
    # crosses the privacy wall: only already-eligible agents are compared.
    if executor in cloud and not requested:
        seen, need = evidence(task_class), int(cfg["routing"].get("min_evidence", 5))
        mine = seen.get(executor)
        if mine and mine["runs"] >= need and mine["rate"] < 0.6:
            better = [n for n in cloud if n != executor and seen.get(n)
                      and seen[n]["runs"] >= need and seen[n]["rate"] >= 0.8]
            if better:
                reasons.append(f"evidence: {executor} succeeded on {mine['ok']}/{mine['runs']} "
                               f"'{task_class}' tasks; {better[0]} on "
                               f"{seen[better[0]]['ok']}/{seen[better[0]]['runs']}")
                executor = better[0]

    # The budget has the last word on spend (10.5).
    if executor in cloud:
        spent = budget_state(task_class, cfg)
        if spent:
            if spent["on_exhaustion"] == "downgrade_to_local" and local and access == READ:
                reasons.append(f"budget spent ({spent['cap']}): downgraded to the local model")
                executor = local[0]
            else:
                reasons.append(f"budget spent ({spent['cap']}): refused")
                executor = None

    if executor is None:
        outcome = ESCALATE
        reasons.append("no agent may do this; it is yours to do or to re-declare")
    elif prepare_only:
        outcome = ESCALATE
    elif executor.startswith("skill:") or executor in local:
        outcome = ACT
    else:
        outcome = DELEGATE
    return {"outcome": outcome, "executor": executor, "prepare_only": prepare_only,
            # Only low-stakes work may ever land without a person reading it.
            "auto_apply_allowed": stakes == "low",
            "reasons": reasons, "not_eligible": blocked, "axes": axes,
            "task_class": task_class}


def explain(decision: dict) -> List[str]:
    """The decision as lines a person can read."""
    lines = []
    for name in ("stakes", "privacy", "context", "autonomy", "task_class"):
        a = decision["axes"][name]
        lines.append(f"  {name.replace('_', ' '):<10} {a['value']:<11} "
                     f"({a['declared_by']}, {a['confidence']} confidence)"
                     + (f"  {a['note']}" if a.get("note") else ""))
    lines.append(f"outcome:  {decision['outcome']}"
                 + (f" -> {decision['executor']}" if decision["executor"] else " -> nobody"))
    for r in decision["reasons"]:
        lines.append(f"  because {r}")
    for b in decision["not_eligible"]:
        lines.append(f"  not eligible: {b}")
    if decision["prepare_only"] and decision["executor"]:
        lines.append("  the agent may inspect or draft a diff; applying it is your decision")
    return lines
