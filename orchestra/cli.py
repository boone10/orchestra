"""Command line for the orchestra control plane.

Direct:   delegate | route | compare | context | bench
Review:   review | apply | discard | note
Daemon:   daemon | submit | requests | cancel | stop | resume
Declare:  project (privacy and stakes of a folder)
Memory:   memory | recall
Skills:   skill
Tools:    mcp
State:    init | state | report | agents
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import (adapters, bench, config, control, mcp_server, memory, outcomes, router,
               skills, spool, state, stop_switch, tasks, workspace)
from .contract import ACCESS_LEVELS, AUTONOMY, CONTEXT, PRIVACY, STAKES


def _spend(cost, tin, tout) -> str:
    bits = ["cost_usd=" + ("unknown" if cost is None else f"{cost:.4f}")]
    if tin is not None or tout is not None:
        bits.append(f"tokens={tin if tin is not None else '?'}in/"
                    f"{tout if tout is not None else '?'}out")
    return " ".join(bits)


def _meta(args) -> dict:
    """The routing properties the operator declared on the command line."""
    return {"stakes": getattr(args, "stakes", None), "privacy": getattr(args, "privacy", None),
            "context": getattr(args, "context", None), "autonomy": getattr(args, "autonomy", None),
            "task_class": getattr(args, "task_class", None)}


def _skill_args(args) -> dict:
    return {"skill": getattr(args, "skill", None), "use_skills": not getattr(args, "no_skill", False)}


def _print_route(decision: dict, matched: Optional[dict] = None, out=sys.stdout) -> None:
    for line in router.explain(decision):
        print(line, file=out)
    for c in (matched or {}).get("candidates", []):
        verdict = "chosen" if c["name"] == matched.get("chosen") else f"not used: {c['rejected'] or 'a better match was chosen'}"
        print(f"  skill {c['name']} ({c['category']}, {c['status']}, match {c['score']}): {verdict}",
              file=out)


def _cmd_route(args) -> int:
    decision, matched = control.route(args.brief, agent=args.agent, cwd=args.cwd,
                                      access=args.access, files=args.file, meta=_meta(args),
                                      source="daemon" if args.as_daemon else "cli",
                                      **_skill_args(args))
    if args.json:
        print(json.dumps({"decision": decision, "skills": matched}, indent=2, ensure_ascii=False))
    else:
        _print_route(decision, matched)
    return 0


def _dry_run(args) -> int:
    """Show exactly what the agent would receive, and what it costs in
    characters, without running anything."""
    import os
    cfg = config.load()
    err = lambda line: print(line, file=sys.stderr)
    decision, matched = control.route(args.brief, agent=args.agent, cwd=args.cwd,
                                      access=args.access, files=args.file, meta=_meta(args),
                                      **_skill_args(args))
    if decision["executor"] is None:
        _print_route(decision, matched, out=sys.stderr)
        err("--- nothing would be sent")
        return 1
    cwd = os.path.abspath(os.path.expanduser(args.cwd or "."))
    project = workspace.project_root(cwd) or cwd
    scope = tasks.resolve_scope(args.file, cwd, project)
    spec = skills.summary(matched["chosen"]) if matched["chosen"] else None
    if decision["executor"].startswith("skill:"):
        print(args.brief)
        _print_route(decision, matched, out=sys.stderr)
        err(f"--- the skill's script would run with this task; no model is involved")
        return 0
    adapter = adapters.get(decision["executor"])
    procedure = (f"{spec['name']}@{spec['version']}", skills.body(spec["name"])) \
        if spec and spec["category"] == "delegation" else None
    brief = tasks.assemble(args.brief, adapter, project, scope, cfg,
                           task_class=decision["task_class"], procedure=procedure)
    print(brief.text)
    _print_route(decision, matched, out=sys.stderr)
    err(f"--- brief for {adapter.name}: {len(brief.text)} chars, digest {brief.digest}")
    for name, chars, remark in brief.sections:
        err(f"    {name:<38} {chars:>6} chars" + (f"   ({remark})" if remark else ""))
    if brief.over_budget:
        err(f"    WARNING: over brief.max_chars ({cfg['brief']['max_chars']}); every run pays for this")
    leaks = tasks.find_secrets(brief.text)
    if leaks:
        err("    BLOCKED: this brief would not be sent; it contains " + ", ".join(leaks))
        return 1
    return 0


def _print_gate(result, out=sys.stderr) -> None:
    files = sorted(set((result.effects or {}).get("uncommitted_files", []))
                   | set((result.effects or {}).get("committed_files", [])))
    if result.gate == "pending":
        if (result.route or {}).get("prepare_only"):
            print("HIGH STAKES: prepared only. Read the diff yourself before applying.", file=out)
        print(f"changes: {len(files)} file(s) in a private copy: {', '.join(files[:8])}", file=out)
        print("         your project is untouched until you apply them", file=out)
        print(f"review:  orchestra review {result.task_id}", file=out)
        print(f"apply:   orchestra apply {result.task_id}      discard: orchestra discard {result.task_id}",
              file=out)
    elif result.gate == "blocked":
        print(f"changes: held back; the run did not pass validation. Read the diff before deciding:",
              file=out)
        print(f"review:  orchestra review {result.task_id}", file=out)
    elif result.gate == "auto_applied":
        print(f"changes: applied to your project automatically (approvals.mode is off): "
              f"{', '.join(files[:8])}", file=out)
    elif result.gate == "nothing_to_review":
        print("changes: none; the agent did not modify anything", file=out)


def _cmd_delegate(args) -> int:
    if args.dry_run:
        return _dry_run(args)
    result = control.delegate(args.brief, agent=args.agent, cwd=args.cwd, timeout=args.timeout,
                              access=args.access, files=args.file, meta=_meta(args),
                              **_skill_args(args))
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        if result.result:
            print(result.result)
        if result.route:
            d = result.route
            print(f"--- route: {d['outcome']} -> {d['executor'] or 'nobody'}"
                  f"   [stakes {d['axes']['stakes']['value']}, privacy {d['axes']['privacy']['value']}, "
                  f"class {d['task_class']}]", file=sys.stderr)
            if result.status == "escalated":
                _print_route(d, out=sys.stderr)
                if d["axes"]["privacy"]["declared_by"] == "default":
                    import os
                    here = os.path.abspath(os.path.expanduser(args.cwd or "."))
                    folder = workspace.project_root(here) or here
                    print("    This project has no declared privacy class. If its contents may be "
                          "sent to a cloud agent, say so once:\n"
                          f"      orchestra project set {folder} --privacy P1", file=sys.stderr)
        print(f"--- task={result.task_id} agent={result.agent} status={result.status} "
              f"{_spend(result.cost_usd, result.input_tokens, result.output_tokens)} "
              f"duration_ms={result.duration_ms}", file=sys.stderr)
        if result.error and result.error != result.result[:500] and result.status != "escalated":
            print(f"error: {result.error}", file=sys.stderr)
        _print_gate(result)
        print(f"recorded in {outcomes.log_path()}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_compare(args) -> int:
    names = [n.strip() for n in args.agents.split(",") if n.strip()]
    runs = control.compare(args.brief, names, cwd=args.cwd, timeout=args.timeout,
                           access=args.access, meta=_meta(args))
    if args.json:
        print(json.dumps([dict(r.to_dict(), workspace=path) for r, path in runs],
                         ensure_ascii=False))
        return 0 if all(r.ok for r, _ in runs) else 1

    access = args.access or config.load()["defaults"]["access"]
    print(f"brief:  {args.brief}")
    print(f"access: {access}   (each agent runs in its own copy; your project is untouched)")
    for result, path in runs:
        print(f"\n### {result.agent} ###")
        print(f"status:    {result.status}"
              + (f"  ({result.error})" if result.error else ""))
        if not path:
            continue
        print(f"spend:     {_spend(result.cost_usd, result.input_tokens, result.output_tokens)}"
              f"  duration_ms={result.duration_ms}")
        print(f"tree:      {workspace.describe(result.effects)}")
        print(f"workspace: {path}")
        answer = result.result.strip()
        if answer:
            if len(answer) > args.chars:
                answer = answer[:args.chars] + f"... [{len(result.result)} chars; --chars to see more]"
            print("answer:")
            for line in answer.splitlines():
                print(f"  {line}")
    if any(path for _, path in runs):
        print("\nThe difference that matters for routing is the 'tree' line: what each agent")
        print("did to the code without asking, and how you would undo it.")
        print("Inspect a copy with:  git -C <workspace> status   /   git -C <workspace> log --oneline -n 3")
    return 0 if all(r.ok for r, _ in runs) else 1


def _cmd_context(args) -> int:
    rows = workspace.context_files(args.cwd)
    for row in rows:
        if row["present"]:
            print(f"{row['file']:<10} present -> read automatically by {row['reader']}")
        else:
            print(f"{row['file']:<10} absent  -> {row['reader']} gets no repo guidance")
    present = [r for r in rows if r["present"]]
    if len(present) > 1 and not present[0]["consistent"]:
        print("warning: the files that exist do not carry identical text, so different "
              "agents are being briefed differently.")
    return 0


def _cmd_report(args) -> int:
    if args.recover:
        added = outcomes.recover_from_audit()
        if not added:
            print("nothing to recover: every finished run in the audit log is already in the outcome log")
            return 0
        cost = sum(e["cost_usd"] for e in added if isinstance(e.get("cost_usd"), (int, float)))
        print(f"recovered {len(added)} run(s) from the audit log, ${cost:.4f} of known cost, "
              f"{added[0]['ts']} to {added[-1]['ts']}")
        print("recovered lines are marked; task wording is included only when the audit event kept it")
        return 0
    entries = outcomes.read_all()
    if not entries:
        print("no outcomes yet")
        return 0
    if args.by_class or args.by_privacy:
        key = "task_class" if args.by_class else "privacy"
        rows = {}
        for e in entries:
            if e.get("source") == "bench":
                continue
            where = e.get("location") or "?"
            r = rows.setdefault((str(e.get(key) or "untagged"), str(e.get("agent")), where),
                                {"runs": 0, "failed": 0, "refused": 0, "cost": 0.0, "unknown": 0})
            if e.get("status") in ("denied", "escalated"):
                r["refused"] += 1
                continue
            r["runs"] += 1
            r["failed"] += e.get("status") != "ok"
            if isinstance(e.get("cost_usd"), (int, float)):
                r["cost"] += e["cost_usd"]
            else:
                r["unknown"] += 1
        print(f"{key.replace('_', ' '):<12} {'agent':<22} {'where':<6} {'runs':>5} {'failed':>6} "
              f"{'refused':>7} {'cost_usd':>9}")
        for (k, agent, where), r in sorted(rows.items()):
            cost = f"{r['cost']:.4f}" + ("+?" if r["unknown"] else "")
            print(f"{k:<12} {agent:<22} {where:<6} {r['runs']:>5} {r['failed']:>6} "
                  f"{r['refused']:>7} {cost:>9}")
        if args.by_privacy:
            leaked = [e for e in entries if e.get("privacy") in ("P3", "P4")
                      and e.get("location") == "cloud" and e.get("status") not in ("denied", "escalated")]
            print(f"\nsensitive (P3/P4) runs that reached a cloud agent: {len(leaked)}")
            for e in leaked:
                why = next((r for r in (e.get("route") or {}).get("reasons", []) if "exception" in r), "")
                print(f"  {e.get('ts')}  {e.get('task_id')}  {e.get('privacy')}  {e.get('agent')}  {why}")
        return 0
    s = outcomes.summarize(entries)
    unknown = f" ({s['cost_unknown']} runs with unknown cost)" if s["cost_unknown"] else ""
    print(f"runs={s['runs']} failures={s['failures']} total_cost_usd={s['cost_usd']:.4f}{unknown}")
    for name, a in sorted(s["by_agent"].items()):
        print(f"  {name}: runs={a['runs']} failures={a['failures']} cost_usd={a['cost_usd']:.4f}")
    print(f"this month: ${router.spend_this_month():.4f} of known cost")
    print("recent:")
    for e in entries[-args.last:]:
        cost = "?" if e.get("cost_usd") is None else f"{e['cost_usd']:.4f}"
        brief = " ".join(str(e.get("brief", "")).split())
        if len(brief) > 70:
            brief = brief[:67] + "..."
        print(f"  {e.get('ts')}  {e.get('agent')}  {e.get('status')}  ${cost}  {brief}")
    return 0


def _cmd_agents(args) -> int:
    for name, adapter in sorted(adapters.REGISTRY.items()):
        up = adapter.available()
        extra = ""
        if name == "local":
            extra = f"  model: {adapter.model()}" if up else "  (is Ollama running, with a model pulled?)"
        print(f"{name}: {'available' if up else 'not installed'}   [{adapter.location}]{extra}")
        if args.verbose:
            for key, value in adapter.profile.items():
                print(f"    {key.replace('_', ' ')}: {value}")
        if args.verbose:
            import os
            names = adapter.forwarded(config.load())
            shown = ", ".join(f"{n} ({'set' if n in os.environ else 'not set'})" for n in names)
            print(f"    environment forwarded: basics + {shown or 'nothing'}")
        last = bench.latest(name)
        if last:
            print(f"    bench: {last['passed']}/{last['total']} on {last['ts'][:10]}  {last['verdict']}")
        elif args.verbose:
            print(f"    bench: never benched (orchestra bench {name})")
    return 0


def _cmd_bench(args) -> int:
    print(f"bench: {args.agent}   each probe runs in a fresh throwaway repository", flush=True)
    report = bench.run(args.agent, only=args.probe or None,
                       timeout=args.timeout,
                       progress=lambda m: print(f"  running {m} ...", file=sys.stderr, flush=True))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        cost, unknown = 0.0, 0
        for row in report["probes"]:
            print(f"  {row['probe']:<12} {row['kind']:<10} {row['outcome'].upper()}")
            for problem in row["problems"]:
                print(f"      - {problem}")
            if row["outcome"] != bench.PASS:
                print(f"      workspace: {row['workspace']}")
            if row["cost_usd"] is None:
                unknown += 1
            else:
                cost += row["cost_usd"]
        note = f" ({unknown} probe(s) with unknown cost)" if unknown else ""
        print(f"verdict: {report['verdict']}")
        print(f"spend:   cost_usd={cost:.4f}{note}")
        print(f"saved in {report['saved']}")
    return 0 if report["passed"] == report["total"] else 1


def _cmd_init(args) -> int:
    created = state.init()
    if created:
        print(f"created in {config.home()}:")
        for name in created:
            print(f"  {name}")
    else:
        print(f"{config.home()} is already set up; nothing changed")
    print("next: orchestra state")
    return 0


def _cmd_state(args) -> int:
    print("\n".join(state.describe()))
    return 0


def _cmd_stop(args) -> int:
    try:
        removed = stop_switch.stop()
    except (OSError, RuntimeError) as exc:
        if stop_switch.is_stopped():
            print(f"Dispatch stopped. Skill cleanup needs attention: {exc}. Retry orchestra stop.", file=sys.stderr)
        else:
            print(f"Could not stop dispatch: {exc}", file=sys.stderr)
        return 1
    print(f"Dispatch stopped. Removed {removed} skill container(s). Pending requests wait for orchestra resume.")
    return 0


def _cmd_resume(args) -> int:
    try:
        resumed = stop_switch.resume()
    except OSError as exc:
        print(f"Could not resume dispatch: {exc}", file=sys.stderr)
        return 1
    print("Dispatch resumed." if resumed else "Dispatch is already enabled.")
    return 0


def _cmd_daemon(args) -> int:
    def log(message: str) -> None:
        print(f"{state.now()}  {message}", flush=True)
    spool.run(once=args.once, log=log)
    return 0


def _cmd_submit(args) -> int:
    req = spool.submit(args.brief, agent=args.agent, cwd=args.cwd, access=args.access,
                       timeout=args.timeout, files=args.file, meta=_meta(args),
                       **_skill_args(args))
    print(req["id"])
    if not state.daemon_pid():
        print("queued, but no daemon is running. Start one with: orchestra daemon",
              file=sys.stderr)
    return 0


def _cmd_requests(args) -> int:
    if args.id:
        print(json.dumps(spool.load(args.id), indent=2, ensure_ascii=False))
        return 0
    reqs = spool.all_requests()
    if not args.all:
        reqs = [r for r in reqs if r["status"] in spool.OPEN] + \
               [r for r in reqs if r["status"] not in spool.OPEN][-args.last:]
        reqs.sort(key=lambda r: r["id"])
    if not reqs:
        print("no requests")
        return 0
    for r in reqs:
        brief = " ".join(r["brief"].split())
        if len(brief) > 60:
            brief = brief[:57] + "..."
        print(f"{r['id']}  {r['status']:<12} {str(r.get('agent') or 'router'):<7} {r['access']:<5} {brief}"
              + (f"\n    review: orchestra review {r.get('task_id')}" if r["status"] == "needs_review" else ""))
    return 0


def _cmd_cancel(args) -> int:
    print(f"{spool.cancel(args.id)['id']} cancelled")
    return 0


def _cmd_review(args) -> int:
    if not args.id:
        waiting = [t for t in tasks.all_tasks() if t["status"] in (tasks.PENDING, tasks.BLOCKED)]
        if not waiting:
            print("nothing is waiting for review")
            return 0
        for t in waiting:
            short = " ".join(t["task"].split())
            if len(short) > 60:
                short = short[:57] + "..."
            flag = "  FAILED VALIDATION" if t["status"] == tasks.BLOCKED else ""
            print(f"{t['task_id']}  {t['agent']:<7} {len(t['files'])} file(s)  {short}{flag}")
            print(f"    project: {t['project']}")
        print("show a diff: orchestra review <task>")
        return 0
    t = tasks.load_task(args.id)
    print(f"task:    {t['task_id']}  ({t['status']})  agent: {t['agent']}")
    print(f"project: {t['project']}")
    print(f"asked:   {' '.join(t['task'].split())}")
    for problem in t.get("problems") or []:
        print(f"PROBLEM: {problem}")
    print(f"files:   {', '.join(t['files'])}")
    print()
    print(tasks.patch_text(t), end="")
    if t["status"] in (tasks.PENDING, tasks.BLOCKED):
        print(f"\napply:   orchestra apply {t['task_id']}"
              + (" --anyway" if t["status"] == tasks.BLOCKED else "")
              + f"      discard: orchestra discard {t['task_id']}", file=sys.stderr)
    return 0


def _cmd_apply(args) -> int:
    t = tasks.apply(args.id, anyway=args.anyway)
    spool.close_review(t.get("request_id"), "done")
    print(f"{t['task_id']} applied to {t['project']} as uncommitted changes: {', '.join(t['files'])}")
    print("review with `git diff`, then commit when you are satisfied")
    return 0


def _cmd_discard(args) -> int:
    t = tasks.discard(args.id)
    spool.close_review(t.get("request_id"), "done")
    print(f"{t['task_id']} discarded; your project was never touched")
    return 0


def _cmd_note(args) -> int:
    task_id = tasks.add_note(args.id, args.text)
    print(f"noted on {task_id}; future briefs for related work in that project will include it")
    return 0


def _cmd_project(args) -> int:
    if args.action == "set":
        if not args.path:
            raise ValueError("give the project folder: orchestra project set <folder> --privacy P1")
        if not (args.privacy or args.stakes or args.autonomy):
            raise ValueError("declare at least one of --privacy, --stakes, --autonomy")
        root, tags = router.set_project(args.path, privacy=args.privacy, stakes=args.stakes,
                                        autonomy=args.autonomy)
        print(f"{root}: " + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())))
        return 0
    known = router.projects()
    if not known:
        print("no projects declared. Untagged work is treated as privacy "
              f"{config.load()['routing']['untagged_privacy']}.")
        print("declare one: orchestra project set <folder> --privacy P0|P1|P2|P3|P4")
        return 0
    for root, tags in sorted(known.items()):
        print(f"{root}: " + ", ".join(f"{k}={v}" for k, v in sorted((tags or {}).items())))
    return 0


def _cmd_memory(args) -> int:
    a = args.action
    if a == "list":
        records = memory.load()
        if args.state:
            records = [r for r in records if r["state"] == args.state]
        if args.due:
            records = [r for r in records if memory.is_due(r)]
        if not records:
            print("no memory records" + (" match" if args.state or args.due else ""))
            return 0
        for r in records:
            due = "  DUE FOR REVIEW" if memory.is_due(r) else ""
            print(f"{r['id']}  {r['state']:<11} {r['type']:<10} {r['source']:<10} {r['scope']}{due}")
            print(f"    {r['summary']}")
            if r["scope"] == "unknown":
                print("    NOT IN USE: the task this old note was attached to is no longer on "
                      "record, so its project is unknown.\n"
                      "    Re-create it: orchestra memory propose \"...\" --project <folder>")
        waiting = sum(r["state"] == "proposed" for r in memory.load())
        if waiting:
            print(f"{waiting} proposed: orchestra memory accept <id>  /  reject <id>")
        return 0
    if a == "propose":
        if not args.text:
            raise ValueError("give the lesson: orchestra memory propose \"...\" --project <folder>")
        scope = memory.scope_for(project=args.project, task_class=args.task_class,
                                 everywhere=args.everywhere)
        rec = memory.add(args.text, scope=scope, type=args.type, source="human",
                         confidence="user-confirmed", state_="proposed",
                         evidence=args.evidence, revisit=args.revisit)
        print(f"{rec['id']} proposed ({rec['scope']}). Usable now, labelled tentative. "
              f"Make it trusted: orchestra memory accept {rec['id']}")
        return 0
    if not args.text:
        raise ValueError(f"give the memory id: orchestra memory {a} <id>")
    if a == "show":
        rec = memory.find(args.text)
        print(json.dumps(rec, indent=2, ensure_ascii=False))
        runs = memory.used_by(rec["id"])
        print(f"used in {len(runs)} brief(s)" + (":" if runs else ""))
        for e in runs[-10:]:
            print(f"  {e.get('ts')}  {e.get('task_id')}  {e.get('agent')}  {e.get('status')}")
        return 0
    if a == "accept":
        rec = memory.set_state(args.text, "curated")
        print(f"{rec['id']} curated: it will be used as confirmed guidance where relevant")
        if args.core:
            memory.promote_to_core(rec["id"])
            print("and added to MEMORY.md, which is sent with every brief. Check `orchestra state` "
                  "for the size of the core.")
        return 0
    rec = memory.set_state(args.text, {"reject": "rejected", "quarantine": "quarantined",
                                       "stale": "stale"}[a])
    print(f"{rec['id']} {rec['state']}: it will no longer be used in briefs")
    if a == "quarantine":
        runs = memory.used_by(rec["id"])
        print(f"it was used in {len(runs)} earlier brief(s); `orchestra memory show {rec['id']}` lists them")
    return 0


def _cmd_recall(args) -> int:
    """Listing 7.1: the always-loaded core, then only what the topic makes relevant."""
    import os
    cwd = os.path.abspath(os.path.expanduser(args.cwd or "."))
    project = workspace.project_root(cwd) or cwd
    print("always in context (sent with every brief):")
    core = dict(state.bounded_memory())
    for name in state.BOUNDED:
        print(f"  {name}: " + (f"{len(core[name])} chars" if name in core else "empty"))
    lines, _ = tasks.relevant(args.topic, project, args.limit, threshold=1)
    print(f"relevant to '{args.topic}' in {project}:")
    for line in lines or ["  (no prior memory on this)"]:
        print(f"  {line}" if not line.startswith("  ") else line)
    return 0


def _cmd_skill(args) -> int:
    a = args.action
    if a == "list":
        specs, used = skills.summaries(), skills.usage()
        if not specs:
            print("no skills. Install the examples as drafts: orchestra skill examples")
            return 0
        for sp in specs:
            u = used.get(sp["name"], {"used": 0, "failed": 0})
            print(f"{sp['name']:<24} {sp['category']:<10} v{sp['version']:<5} {sp['status']:<10} "
                  f"used {u['used']}, failed {u['failed']}")
            print(f"    {' '.join(str(sp['description']).split())[:150]}")
        return 0
    if a == "examples":
        created = skills.install_examples()
        if created:
            print("installed as drafts (not usable until you review and trust them):")
            for name in created:
                print(f"  {name}    {skills.root() / name}")
            print("read one: orchestra skill show <name>     then: orchestra skill trust <name>")
        else:
            print("the examples are already installed")
        return 0
    if not args.name:
        raise ValueError(f"give the skill name: orchestra skill {a} <name>")
    if a == "new":
        path = skills.new(args.name, args.category)
        print(f"draft created at {path}\nedit skill.yaml and SKILL.md, then: orchestra skill trust {args.name}")
        return 0
    if a == "show":
        sp = skills.summary(args.name)
        base = skills.root() / args.name
        print(f"{sp['name']}  ({sp['category']}, v{sp['version']}, {sp['status']})  {base}")
        for path in sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            print(f"\n----- {path.relative_to(base)} -----")
            print(path.read_text(encoding="utf-8", errors="replace").rstrip())
        return 0
    if a == "test":
        t = skills.run_test(args.name)
        if not t["declared"]:
            print("this skill declares no test")
            return 1
        print(t["output"])
        print("PASSED" if t["passed"] else "FAILED")
        return 0 if t["passed"] else 1
    if a == "trust":
        sp = skills.trust(args.name, without_test=args.without_test)
        print(f"{sp['name']} v{sp['version']} is trusted. Editing any of its files will require "
              "trusting it again.")
        return 0
    sp = skills.deprecate(args.name)
    print(f"{sp['name']} is deprecated: it will not be matched for new work")
    return 0


def _cmd_mcp(args) -> int:
    if args.action == "serve":
        if args.name != "memory":
            raise ValueError("the only server orchestra provides is: orchestra mcp serve memory")
        mcp_server.serve()
        return 0
    for srv in mcp_server.registrations():
        print(f"{srv.get('name', '(unnamed)')}: {srv.get('purpose', '')}")
        print(f"    start: {srv.get('command', '?')}   transport: {srv.get('transport', '?')}   "
              f"credentials: {srv.get('credentials', '?')}")
        for tool in srv.get("tools") or []:
            if isinstance(tool, dict):
                gate = "gated" if tool.get("gated") else "free"
                print(f"    {tool.get('access', '?'):<5} {gate:<5} {tool.get('name')}"
                      + (f"   ({tool['note']})" if tool.get("note") else ""))
        print(f"    owner: {srv.get('owner', '?')}   reviewed: {srv.get('reviewed', '?')}")
        if srv["missing"]:
            print("    NOT FULLY REGISTERED: " + "; ".join(str(m) for m in srv["missing"]))
    return 0


def _run_options(p, output: bool = True, scope: bool = True) -> None:
    p.add_argument("--cwd", help="project directory (default: current)")
    p.add_argument("--timeout", type=int, help="seconds per agent (default from config)")
    p.add_argument("--access", choices=ACCESS_LEVELS,
                   help="read: look and answer only. edit: work in a private copy and "
                        "produce a diff for you to review and apply")
    p.add_argument("--stakes", choices=STAKES,
                   help="how bad if it goes wrong. high = a human authorizes; agents only prepare")
    p.add_argument("--privacy", choices=PRIVACY,
                   help="P0 public .. P4 must never leave this machine (default: the project's tag)")
    p.add_argument("--context", choices=CONTEXT, help="how much must be held in mind at once")
    p.add_argument("--autonomy", choices=AUTONOMY)
    p.add_argument("--class", dest="task_class", metavar="NAME",
                   help="the kind of task, for budgets and evidence (default: inferred)")
    if scope:
        p.add_argument("--skill", metavar="NAME", help="use this skill instead of matching one")
        p.add_argument("--no-skill", action="store_true", help="do not match a skill")
        p.add_argument("--file", action="append", metavar="PATH",
                       help="a file the task is about (repeatable). Listed in the brief; an edit "
                            "run that changes anything else fails validation")
    if output:
        p.add_argument("--json", action="store_true", help="print the full contract as JSON")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestra", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("delegate", help="hand one task to an agent now and record the outcome")
    d.add_argument("brief", help="the task, in plain language")
    d.add_argument("--agent", help="name an agent instead of letting the router choose")
    d.add_argument("--dry-run", action="store_true",
                   help="print the routing decision and exactly what the agent would receive; run nothing")
    _run_options(d)
    d.set_defaults(func=_cmd_delegate)

    ro = sub.add_parser("route", help="show how a task would be routed, and why, without running it")
    ro.add_argument("brief", help="the task, in plain language")
    ro.add_argument("--agent", help="name an agent instead of letting the router choose")
    ro.add_argument("--as-daemon", action="store_true",
                    help="route it as a queued (unattended) request rather than a typed one")
    _run_options(ro)
    ro.set_defaults(func=_cmd_route)

    c = sub.add_parser("compare", help="run one brief through several agents, each in its "
                                       "own copy of the project (book 3.8)")
    c.add_argument("brief", help="the task, in plain language")
    c.add_argument("--agents", default="claude,codex", help="comma-separated (default claude,codex)")
    c.add_argument("--chars", type=int, default=600, help="answer characters to show per agent")
    _run_options(c, scope=False)
    c.set_defaults(func=_cmd_compare)

    b = sub.add_parser("bench", help="put an agent through fixed capability and safety "
                                     "probes before trusting it (book 3.10)")
    b.add_argument("agent", help="a registered agent, e.g. claude or codex")
    b.add_argument("--probe", action="append", choices=bench.PROBE_NAMES,
                   help="run only this probe (repeatable); default is all of them")
    b.add_argument("--timeout", type=int, help="seconds per probe (default from config)")
    b.add_argument("--json", action="store_true", help="print the full report as JSON")
    b.set_defaults(func=_cmd_bench)

    x = sub.add_parser("context", help="which agents arrive already briefed in a project")
    x.add_argument("--cwd", help="project directory (default: current)")
    x.set_defaults(func=_cmd_context)

    r = sub.add_parser("report", help="read the outcomes log back")
    r.add_argument("-n", "--last", type=int, default=3, help="recent runs to show")
    r.add_argument("--recover", action="store_true",
                   help="rebuild missing outcome lines from the audit log")
    r.add_argument("--by-class", action="store_true",
                   help="runs, failures and cost per task class and agent (the weekly review)")
    r.add_argument("--by-privacy", action="store_true",
                   help="what went where by privacy class, and any sensitive run that reached the cloud")
    r.set_defaults(func=_cmd_report)

    a = sub.add_parser("agents", help="list registered agents")
    a.add_argument("-v", "--verbose", action="store_true", help="show each agent's profile")
    a.set_defaults(func=_cmd_agents)

    i = sub.add_parser("init", help="create the state directory (never overwrites)")
    i.set_defaults(func=_cmd_init)

    s = sub.add_parser("state", help="walk the state directory: what the control plane knows "
                                     "and may do (book 4.9)")
    s.set_defaults(func=_cmd_state)

    stop = sub.add_parser("stop", help="stop running Docker skills and hold new work for this Orchestra home")
    stop.set_defaults(func=_cmd_stop)
    resume = sub.add_parser("resume", help="allow new work and release pending requests to the daemon")
    resume.set_defaults(func=_cmd_resume)

    dm = sub.add_parser("daemon", help="run the coordinator in the foreground; Ctrl-C to stop")
    dm.add_argument("--once", action="store_true", help="work through the spool once and exit")
    dm.set_defaults(func=_cmd_daemon)

    sm = sub.add_parser("submit", help="queue a task for the daemon; prints the request id")
    sm.add_argument("brief", help="the task, in plain language")
    sm.add_argument("--agent", help="name an agent instead of letting the router choose")
    _run_options(sm, output=False)
    sm.set_defaults(func=_cmd_submit)

    rq = sub.add_parser("requests", help="list queued and recent requests, or show one")
    rq.add_argument("id", nargs="?", help="a request id (or unique part of one) to show in full")
    rq.add_argument("--all", action="store_true", help="list every request")
    rq.add_argument("-n", "--last", type=int, default=5, help="finished requests to include")
    rq.set_defaults(func=_cmd_requests)

    cn = sub.add_parser("cancel", help="withdraw a queued request before it starts")
    cn.add_argument("id", help="request id, or a unique part of it")
    cn.set_defaults(func=_cmd_cancel)

    rv = sub.add_parser("review", help="list diffs waiting for you, or show one")
    rv.add_argument("id", nargs="?", help="a task id (or unique part of one) to show its diff")
    rv.set_defaults(func=_cmd_review)

    ap = sub.add_parser("apply", help="land a reviewed diff in your project (uncommitted)")
    ap.add_argument("id", help="task id, or a unique part of it")
    ap.add_argument("--anyway", action="store_true",
                    help="apply even though the run failed validation")
    ap.set_defaults(func=_cmd_apply)

    dc = sub.add_parser("discard", help="reject a diff; your project is never touched")
    dc.add_argument("id", help="task id, or a unique part of it")
    dc.set_defaults(func=_cmd_discard)

    nt = sub.add_parser("note", help="attach a lesson to a past task; it is included in "
                                     "future briefs for related work in the same project")
    nt.add_argument("id", help="task id, or a unique part of it")
    nt.add_argument("text", help="the note, in plain language")
    nt.set_defaults(func=_cmd_note)

    pj = sub.add_parser("project", help="declare a folder's privacy class and stakes, once")
    pj.add_argument("action", choices=("set", "list"))
    pj.add_argument("path", nargs="?", help="the project folder")
    pj.add_argument("--privacy", choices=PRIVACY,
                    help="P0 public, P1 internal, P2 confidential, P3 sensitive, P4 never leaves")
    pj.add_argument("--stakes", choices=STAKES)
    pj.add_argument("--autonomy", choices=AUTONOMY)
    pj.set_defaults(func=_cmd_project)

    me = sub.add_parser("memory", help="lessons: propose, review, quarantine")
    me.add_argument("action", choices=("list", "propose", "show", "accept", "reject",
                                       "quarantine", "stale"))
    me.add_argument("text", nargs="?", help="the lesson (for propose) or a memory id")
    me.add_argument("--project", metavar="FOLDER", help="scope: this project")
    me.add_argument("--class", dest="task_class", metavar="NAME", help="scope: this task class")
    me.add_argument("--global", dest="everywhere", action="store_true", help="scope: everywhere")
    me.add_argument("--type", choices=memory.TYPES, default="fact")
    me.add_argument("--evidence", metavar="TASK", help="the task id this lesson came from")
    me.add_argument("--revisit", metavar="YYYY-MM-DD", help="when it should be re-checked")
    me.add_argument("--state", choices=memory.STATES, help="list: only this review state")
    me.add_argument("--due", action="store_true", help="list: only those due for re-checking")
    me.add_argument("--core", action="store_true",
                    help="accept: also add it to MEMORY.md, sent with every brief")
    me.set_defaults(func=_cmd_memory)

    rc = sub.add_parser("recall", help="what the control plane remembers about a topic")
    rc.add_argument("topic")
    rc.add_argument("--cwd", help="project directory (default: current)")
    rc.add_argument("-n", "--limit", type=int, default=5)
    rc.set_defaults(func=_cmd_recall)

    sk = sub.add_parser("skill", help="the capability registry: list, review, trust")
    sk.add_argument("action", choices=("list", "examples", "new", "show", "test", "trust", "deprecate"))
    sk.add_argument("name", nargs="?")
    sk.add_argument("--category", choices=skills.CATEGORIES, default="delegation")
    sk.add_argument("--without-test", action="store_true",
                    help="trust: accept a skill that declares no test")
    sk.set_defaults(func=_cmd_skill)

    mc = sub.add_parser("mcp", help="tool servers: registrations, and the memory server")
    mc.add_argument("action", choices=("list", "serve"))
    mc.add_argument("name", nargs="?", help="for serve: memory")
    mc.set_defaults(func=_cmd_mcp)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyError as e:  # unknown agent
        print(e.args[0], file=sys.stderr)
        return 2
    except (config.ConfigError, spool.SpoolError, bench.BenchError, tasks.TaskError,
            memory.MemoryError_, skills.SkillError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
