"""The delegation primitive: one lifecycle for every surface (book 4.3, 5.3).

Whether a request is typed at the command line, picked up by the daemon, or
issued by the bench, it goes through delegate() and the same ordered steps:

  intake -> classify -> match a skill -> route -> the privacy wall
         -> assemble brief -> secrets check -> choose where to run
         -> execute -> validate -> capture -> gate -> write back

The four disciplines of chapter 5 are all here: a brief assembled from what
the coordinator knows, invocation only through an adapter, a result that is
validated before it is trusted, and an outcome written back every time,
including for runs that were refused or failed.

Routing (chapter 6) decides who does the work: a cloud agent, the local
model, a trusted native skill, or nobody. High stakes never lands without a
person. Edit-level runs never touch the real project: the agent works in a
private copy and its changes wait as a diff for `orchestra apply`.
The coordinator is rule-based and makes no model calls of its own.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import adapters, config, outcomes, router, skills, state, tasks, workspace
from .adapters.base import depth
from .contract import ACCESS_LEVELS, DENIED, EDIT, ERROR, ESCALATED, READ, TIMEOUT, Result


def _validate(result: Result, access: str, scope: List[str]) -> List[str]:
    """The boundary between an agent's output and our trust (book 5.4). This
    catches what rules can catch: a missing answer, a broken promise about
    access, work outside the declared scope. It cannot catch a confident,
    well-formed, wrong answer; that is what the human gate is for."""
    problems = []
    eff = result.effects or {}
    changed = sorted(set(eff.get("uncommitted_files", [])) | set(eff.get("committed_files", [])))
    if result.ok and not result.result.strip():
        problems.append("the agent reported success but returned no answer")
    if access == READ and (changed or eff.get("commits_added")):
        problems.append("access violation: a read-only run changed the working tree ("
                        + workspace.describe(eff) + "). Review and undo with git.")
    if access == EDIT and scope:
        outside = [f for f in changed if f not in scope]
        if outside:
            problems.append("changed files outside the declared scope: " + ", ".join(outside))
    return problems


def _transient(result: Result) -> bool:
    return result.status == TIMEOUT or bool(result.error and "unparseable reply" in result.error)


def delegate(brief: str, agent: Optional[str] = None, cwd: Optional[str] = None,
             timeout: Optional[int] = None, access: Optional[str] = None,
             group: Optional[str] = None, source: str = "cli",
             request_id: Optional[str] = None, files: Optional[List[str]] = None,
             isolate: bool = True, meta: Optional[dict] = None,
             skill: Optional[str] = None, use_skills: bool = True) -> Result:
    """Delegate one task. `brief` is the task as the operator wrote it.

    agent     name one to overrule the router's choice (the privacy wall and
              the high-stakes rule still apply).
    meta      declared routing properties: stakes, privacy, context, autonomy,
              task_class. Anything not declared is classified conservatively.
    skill     name a skill to use; use_skills=False turns matching off.
    isolate   False is for callers that already run in a throwaway directory
              (compare, bench): an edit run then changes that directory directly.
    """
    task = brief
    # --- intake -----------------------------------------------------------
    cfg = config.load()
    access = access or cfg["defaults"]["access"]
    timeout = int(timeout or cfg["defaults"]["timeout"])
    if access not in ACCESS_LEVELS:
        raise ValueError(f"access must be one of {ACCESS_LEVELS}")
    if agent:
        adapters.get(agent)  # an unknown name is the caller's mistake, not an outcome
    task_id = tasks.new_id()
    started = state.now()
    cwd_abs = os.path.abspath(os.path.expanduser(cwd or "."))
    git_root = workspace.project_root(cwd_abs) if os.path.isdir(cwd_abs) else None
    project = git_root or cwd_abs
    ids = dict(task_id=task_id, agent=agent or "unrouted", access=access, source=source,
               request_id=request_id)
    record = dict(project=project, access=access, started=started, group=group, source=source,
                  request_id=request_id)
    # The task text is not written to the audit log: it has not been checked
    # for credentials yet. The outcome log records it once it has been.
    state.audit("request.received", task_chars=len(task), cwd=cwd_abs, **ids)

    def write_back(result: Result, logged_task: str = task, **extra) -> Result:
        result.task_id = task_id
        outcomes.record(result, logged_task, cwd=cwd_abs, finished=state.now(),
                        **dict(record, **extra))
        state.audit("run.finished", status=result.status, error=result.error,
                    cost_usd=result.cost_usd, gate=result.gate, **ids)
        return result

    def refuse(status: str, why: str, **extra) -> Result:
        return write_back(Result(ids["agent"], status, error=why, gate="not_required",
                                 route=record.get("route")), **extra)

    # --- recursion guard (5.11) -------------------------------------------
    if depth() >= int(cfg["delegation"]["max_depth"]):
        return refuse(DENIED, f"delegation depth cap reached (depth {depth()}, "
                              f"max_depth {cfg['delegation']['max_depth']}): an agent started by "
                              "orchestra may not start further agents")
    try:
        scope = tasks.resolve_scope(files, cwd_abs, project)
    except tasks.TaskError as e:
        return refuse(ERROR, str(e))

    # --- classify, match a skill, route (chapters 6, 8, 10) -----------------
    # Classify from where the work really lives: a throwaway copy inherits the
    # declared properties of the project it was copied from, via meta.
    axes = router.classify(task, meta, project, scope, source, cfg)
    matched, procedure = {"considered": 0, "candidates": [], "chosen": None}, None
    if use_skills or skill:
        try:
            matched = skills.match(task, axes, access, cfg, forced=skill)
        except skills.SkillError as e:
            return refuse(ERROR, str(e))
        if matched["candidates"]:
            state.audit("skill.match", considered=matched["considered"],
                        candidates=matched["candidates"], chosen=matched["chosen"], **ids)
    chosen = next((c for c in matched["candidates"] if c["name"] == matched["chosen"]), None)
    spec = skills.summary(chosen["name"]) if chosen else None
    if spec and (meta or {}).get("task_class") is None and spec.get("task_class"):
        axes["task_class"] = {"value": str(spec["task_class"]), "declared_by": "skill",
                              "confidence": "high"}
    if spec and spec["category"] == "delegation" and not agent and spec.get("agent"):
        agent = str(spec["agent"]) if str(spec["agent"]) in adapters.REGISTRY else None
        by_skill = agent
    else:
        by_skill = None
    decision = router.decide(axes, access, cfg, requested=agent,
                             native_skill=spec["name"] if spec and spec["category"] == "native" else None)
    if by_skill and decision["executor"] == by_skill:
        decision["reasons"] = [r.replace(f"the operator named {by_skill}",
                                         f"the skill {spec['name']} names {by_skill}")
                               for r in decision["reasons"]]
    if by_skill and decision["executor"] is None:
        # The skill's preferred agent may not take this; let the router choose.
        decision = router.decide(axes, access, cfg)
    skill_tag = f"{spec['name']}@{spec['version']}" if spec else None
    record.update(route=decision, task_class=decision["task_class"],
                  privacy=axes["privacy"]["value"], stakes=axes["stakes"]["value"], skill=skill_tag)
    state.audit("route.decided", outcome=decision["outcome"], executor=decision["executor"],
                reasons=decision["reasons"], **ids)
    if decision["executor"] is None:
        if agent and not by_skill and not adapters.get(agent).available() \
                and router.allowed(adapters.get(agent), axes["privacy"]["value"],
                                   decision["task_class"], cfg)[0]:
            ids["agent"] = agent
            return refuse(ERROR, f"'{adapters.get(agent).binary or agent}' is not installed "
                                 "or not running on this machine (not found)")
        return refuse(ESCALATED, "; ".join(decision["reasons"] + decision["not_eligible"]))

    if decision["executor"].startswith("skill:"):
        adapter = skills.SkillRunner(spec["name"], project, scope)
    else:
        adapter = adapters.get(decision["executor"])
        if spec and spec["category"] == "delegation":
            procedure = (skill_tag, skills.body(spec["name"]))
    ids["agent"] = adapter.name
    record.update(adapter_version=adapter.version, location=adapter.location)

    # --- the privacy wall, checked again where it is enforced (10.6) ----------
    ok, why = router.allowed(adapter, axes["privacy"]["value"], decision["task_class"], cfg)
    if not ok:
        state.audit("privacy.blocked", privacy=axes["privacy"]["value"], why=why, **ids)
        return refuse(ESCALATED, f"privacy wall: {why}")
    if adapter.location == "cloud" and axes["privacy"]["value"] == "P3":
        state.audit("privacy.exception_used", why=why, **ids)

    # --- assemble the brief (5.2) -------------------------------------------
    if isinstance(adapter, skills.SkillRunner):
        assembled = tasks.Brief(task)  # a script gets the task, not a prompt
    else:
        assembled = tasks.assemble(task, adapter, project, scope, cfg,
                                   task_class=decision["task_class"], procedure=procedure)
    leaks = tasks.find_secrets(assembled.text)
    if leaks:
        state.audit("brief.blocked", found=leaks, **ids)
        return refuse(ERROR, "brief blocked, nothing was sent: it contains " + ", ".join(leaks)
                      + ". Remove it from the task, the guidance file or the memory files.",
                      logged_task="[withheld: the brief contained a credential]")
    artifacts = str(tasks.task_dir(task_id))
    tasks.save_text(task_id, "brief.md", assembled.text)
    record.update(brief_digest=assembled.digest, brief_chars=len(assembled.text),
                  files_in_scope=scope or None, memories_used=assembled.memories_used or None)

    # --- choose where the agent runs ----------------------------------------
    run_cwd, copy, baseline = cwd_abs, None, None
    if access == EDIT and isolate:
        if not git_root:
            return refuse(ERROR, "edit runs are reviewed before they land, which needs a git "
                                 f"repository. Run `git init` in {cwd_abs} and commit once.")
        try:
            copy, baseline = tasks.prepare_copy(git_root, task_id)
        except (tasks.TaskError, OSError) as e:
            return refuse(ERROR, f"could not prepare a private copy of the project: {e}")
        run_cwd = str(copy / os.path.relpath(cwd_abs, git_root))

    # --- execute, validate, capture (5.4) -------------------------------------
    # Retries are deliberate and bounded (5.8): only read-only runs, only for
    # a timeout or an unreadable reply. Repeating an edit could act twice.
    attempts = 1 + (max(0, int(cfg["delegation"]["retries"])) if access == READ else 0)
    for attempt in range(1, attempts + 1):
        before = workspace.snapshot(run_cwd)
        state.audit("run.started", attempt=attempt, brief_chars=len(assembled.text), **ids)
        result = adapter.delegate(assembled.text, cwd=run_cwd, timeout=timeout, access=access)
        result.effects = workspace.effects(run_cwd, before)
        result.artifacts = artifacts
        result.route = decision
        result.problems = _validate(result, access, scope)
        if result.problems:
            state.audit("validation.failed", problems=result.problems, **ids)
            result.status = ERROR
            result.error = "validation failed: " + "; ".join(result.problems)
        if attempt < attempts and _transient(result) and not result.problems:
            result.gate = "not_required"
            write_back(result, attempt=attempt)
            record["started"] = state.now()
            continue
        break
    if result.result:
        tasks.save_text(task_id, "result.md", result.result)

    # --- the human gate (5.3, 6.3) ------------------------------------------------
    if access == READ:
        result.gate = "not_required"
    elif not isolate:
        result.gate = "direct"
    else:
        try:
            patch, touched = tasks.make_patch(copy, baseline)
        except tasks.TaskError as e:
            patch, touched = "", []
            result.status, result.error = ERROR, f"could not collect the agent's changes: {e}"
        if not patch:
            result.gate = "nothing_to_review"
        else:
            patch_path = tasks.save_text(task_id, "changes.diff", patch)
            valid = result.ok and not result.problems
            review = tasks.save_task(task_id, {
                "task_id": task_id, "created": started, "project": git_root, "agent": adapter.name,
                "task": task, "status": tasks.PENDING if valid else tasks.BLOCKED,
                "problems": result.problems or ([result.error] if not result.ok else []),
                "stakes": axes["stakes"]["value"],
                "patch": str(patch_path), "files": touched, "request_id": request_id})
            result.gate = review["status"]
            # Only low-stakes work may land without a person reading it, and
            # only when the operator has switched reviews off. High stakes
            # never does: that is the hard rule, and no setting reaches it.
            if valid and cfg["approvals"]["mode"] == "off" and decision["auto_apply_allowed"]:
                try:
                    tasks.apply(task_id, decided_by="approvals.mode=off")
                    result.gate = "auto_applied"
                except tasks.TaskError as e:
                    state.audit("review.apply_failed", error=str(e), **ids)
    return write_back(result, attempt=attempt)


def route(brief: str, agent: Optional[str] = None, cwd: Optional[str] = None,
          access: Optional[str] = None, source: str = "cli", files: Optional[List[str]] = None,
          meta: Optional[dict] = None, skill: Optional[str] = None,
          use_skills: bool = True) -> Tuple[dict, dict]:
    """What the router would do with this task, without doing it:
    (decision, skill match log)."""
    cfg = config.load()
    access = access or cfg["defaults"]["access"]
    if agent:
        adapters.get(agent)
    cwd_abs = os.path.abspath(os.path.expanduser(cwd or "."))
    project = (workspace.project_root(cwd_abs) if os.path.isdir(cwd_abs) else None) or cwd_abs
    scope = tasks.resolve_scope(files, cwd_abs, project)
    axes = router.classify(brief, meta, project, scope, source, cfg)
    matched = {"considered": 0, "candidates": [], "chosen": None}
    if use_skills or skill:
        matched = skills.match(brief, axes, access, cfg, forced=skill)
    spec = skills.summary(matched["chosen"]) if matched["chosen"] else None
    if spec and (meta or {}).get("task_class") is None and spec.get("task_class"):
        axes["task_class"] = {"value": str(spec["task_class"]), "declared_by": "skill",
                              "confidence": "high"}
    if spec and spec["category"] == "delegation" and not agent \
            and str(spec.get("agent")) in adapters.REGISTRY:
        decision = router.decide(axes, access, cfg, requested=str(spec["agent"]))
        if decision["executor"] is None:
            decision = router.decide(axes, access, cfg)
    else:
        decision = router.decide(axes, access, cfg, requested=agent,
                                 native_skill=spec["name"] if spec and spec["category"] == "native"
                                 else None)
    return decision, matched


def compare(brief: str, agents: List[str], cwd: Optional[str] = None,
            timeout: Optional[int] = None, access: Optional[str] = None,
            meta: Optional[dict] = None) -> List[Tuple[Result, str]]:
    """The same brief through several agents (book 3.8).

    Each agent works in its own copy of the project, so every agent starts
    from the identical tree, none sees another's changes, and the original is
    never touched. Nothing is applied; this is for looking, not landing.
    Returns (result, path to that agent's copy) per agent.
    """
    cfg = config.load()
    access = access or cfg["defaults"]["access"]
    for name in agents:
        adapters.get(name)  # fail before any agent runs, not halfway through
    # The copies live elsewhere on disk, so carry the original project's
    # declared privacy and stakes with them rather than losing them.
    origin = os.path.abspath(os.path.expanduser(cwd or "."))
    origin = (workspace.project_root(origin) if os.path.isdir(origin) else None) or origin
    tags = router.project_tags(origin)
    meta = dict(meta or {})
    for key in ("privacy", "stakes"):
        if key not in meta and tags.get(key):
            meta[key] = tags[key]
            meta["declared_by"] = "project"
    group = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)  # unique per run
    runs = []
    for name in agents:
        copy = workspace.isolated_copy(cwd, outcomes.home() / "runs" / f"{group}-{name}")
        result = delegate(brief, agent=name, cwd=str(copy), timeout=timeout, access=access,
                          group=group, isolate=False, meta=meta, use_skills=False)
        runs.append((result, str(copy)))
    return runs
