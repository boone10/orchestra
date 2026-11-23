"""Chapters 6 to 10: the router and its hard rule, the privacy wall and the
local model, budgets, memory records, the skills registry, and the MCP memory
server. Stand-in agents and a stand-in local model server throughout."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestra import (cli, config, control, mcp_server, memory, outcomes, router, skills,
                       spool, state, tasks)
from orchestra.contract import EDIT, ERROR, ESCALATED, OK, Result

# One stand-in plays either agent: it answers in the format of the name it was
# installed under, records its prompt, and edits app.py when allowed to.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
me = "codex" if "codex" in Path(sys.argv[0]).name else "claude"
home = Path(os.environ["ORCHESTRA_HOME"])
(home / f"prompt-{me}.txt").write_text(sys.argv[-1])
with (home / "launched.txt").open("a") as f:
    f.write(me + "\n")
if "acceptEdits" in sys.argv or "workspace-write" in sys.argv:
    Path("app.py").write_text(Path("app.py").read_text() + "# doc\n")
if me == "codex":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex done"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 2}}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "claude done", "total_cost_usd": 0.5}))
'''


class LocalModel(BaseHTTPRequestHandler):
    """Speaks just enough of the OpenAI-compatible API to stand in for Ollama."""
    received = []

    def log_message(self, *args):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"data": [{"id": "stub-model"}]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LocalModel.received.append(data)
        self._send({"choices": [{"message": {"content": "local done"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


class Base(unittest.TestCase):
    local_model = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        # This chapter tests the shipped example's matching, trust and output.
        # Chapter 11 independently exercises the Docker boundary.
        def run_example(cmd, *, skill_dir, cwd, timeout, files=(), **kwargs):
            self.assertEqual(cmd[0], "python3")
            self.assertIn(cmd[1], ("scripts/check_links.py", "scripts/run.py"))
            return subprocess.run([sys.executable, str(skill_dir / cmd[1]), *cmd[2:]],
                                  cwd=cwd, env={"ORCHESTRA_FILES": "\n".join(files)},
                                  capture_output=True, text=True, timeout=timeout)
        runner = patch.object(skills.backends, "run_skill", side_effect=run_example)
        runner.start()
        self.addCleanup(runner.stop)

        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        self.home = root / "home"
        self.home.mkdir()
        os.environ["ORCHESTRA_HOME"] = str(self.home)
        os.environ.pop("ORCHESTRA_DEPTH", None)
        for name in ("claude", "codex"):
            fake = root / f"fake-{name}"
            fake.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            os.environ[f"ORCHESTRA_{name.upper()}_BIN"] = str(fake)
        self.config = {"local": {"base_url": "http://127.0.0.1:9"}}   # nothing listens there
        if self.local_model:
            LocalModel.received = []
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.config["local"]["base_url"] = f"http://127.0.0.1:{server.server_address[1]}"
        self.write_config()
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def top():\n    return 1\n")
        (self.repo / "README.md").write_text("# Demo\n\nSee [the guide](docs/guide.md) and [gone](docs/missing.md).\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.md").write_text("# Guide\n")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def write_config(self, **sections):
        import yaml
        self.config.update(sections)
        (self.home / "config.yaml").write_text(yaml.safe_dump(self.config))

    def tag(self, **tags):
        router.set_project(str(self.repo), **tags)

    def launched(self):
        path = self.home / "launched.txt"
        return path.read_text().split() if path.exists() else []

    def go(self, task="look at app.py", **kw):
        return control.delegate(task, cwd=str(self.repo), **kw)

    def decide(self, task="look at app.py", access="read", **kw):
        return control.route(task, cwd=str(self.repo), access=access, **kw)[0]


class RouterTest(Base):
    def test_untagged_work_never_reaches_a_cloud_agent(self):
        r = self.go()
        self.assertEqual(r.status, ESCALATED)
        self.assertEqual(self.launched(), [])
        self.assertEqual(r.route["axes"]["privacy"],
                         {"value": "P3", "declared_by": "default", "confidence": "low",
                          "note": "no privacy class declared for this project; treated as sensitive"})
        self.assertEqual(outcomes.read_all()[-1]["status"], ESCALATED)   # the refusal is on record
        self.assertEqual(cli.main(["project", "set", str(self.repo), "--privacy", "P1"]), 0)
        r = self.go()
        self.assertEqual((r.status, r.agent), (OK, "claude"))
        self.assertEqual(r.route["axes"]["privacy"]["declared_by"], "project")
        self.assertEqual(cli.main(["project", "list"]), 0)

    def test_a_tag_covers_everything_beneath_the_folder(self):
        router.set_project(self.tmp.name, privacy="P0")
        self.assertEqual(router.project_tags(str(self.repo / "docs"))["privacy"], "P0")
        self.tag(privacy="P4")                                      # the closer tag wins
        self.assertEqual(router.project_tags(str(self.repo / "docs"))["privacy"], "P4")

    def test_size_and_autonomy_pick_the_agent(self):
        self.tag(privacy="P1")
        self.assertEqual(self.decide()["executor"], "claude")
        big = self.decide("refactor the billing module across the whole codebase", meta={"stakes": "low"})
        self.assertEqual((big["executor"], big["axes"]["context"]["value"]), ("claude", "large"))
        queued = self.decide(source="daemon")
        self.assertEqual((queued["executor"], queued["outcome"]), (None, router.ESCALATE))
        self.assertEqual(self.decide(agent="codex")["executor"], "codex")   # the operator may overrule
        self.assertEqual(cli.main(["route", "look at app.py", "--cwd", str(self.repo)]), 0)
        self.assertEqual(self.launched(), [])                               # routing runs nothing

    def test_the_hard_rule(self):
        self.tag(privacy="P1")
        self.write_config(approvals={"mode": "off"})
        # Declared high stakes: prepared, never applied, whatever the settings say.
        r = self.go("tidy the helper", access=EDIT, meta={"stakes": "high"})
        self.assertEqual((r.route["outcome"], r.gate), ("escalate-human", "pending"))
        self.assertEqual(tasks.load_task(r.task_id)["stakes"], "high")
        # Undeclared, but the wording smells of consequence: rounded up.
        d = self.decide("change how refunds are calculated in payments")
        self.assertEqual(d["axes"]["stakes"]["value"], "high")
        self.assertEqual(d["axes"]["stakes"]["declared_by"], "inferred")
        self.assertTrue(d["prepare_only"])
        d = self.decide("update the workflow", files=[".github/workflows/ci.yml"])
        self.assertEqual(d["axes"]["stakes"]["value"], "high")
        # A person may declare it lower; the router says what it noticed and obeys.
        d = self.decide("rename the payments helper", meta={"stakes": "low"})
        self.assertEqual(d["axes"]["stakes"]["value"], "low")
        self.assertIn("declared value stands", d["axes"]["stakes"]["note"])
        # Medium (the default) is reviewed too; only declared-low may land unread.
        r = self.go("tidy the helper", access=EDIT)
        self.assertEqual(r.gate, "pending")
        with self.assertRaises(ValueError):
            self.decide(meta={"stakes": "extreme"})

    def test_evidence_changes_the_choice_but_never_crosses_the_wall(self):
        self.tag(privacy="P1")
        for agent, status in [("claude", ERROR)] * 5 + [("codex", OK)] * 5:
            outcomes.record(Result(agent, status), "x", task_class="refactor")
        d = self.decide("refactor the parser across the whole codebase")
        self.assertEqual(d["executor"], "codex")
        self.assertTrue(any("evidence" in r for r in d["reasons"]))
        self.tag(privacy="P4")
        self.assertIsNone(self.decide("refactor the parser across the whole codebase")["executor"])

    def test_budget_refuses_or_downgrades(self):
        self.tag(privacy="P1")
        self.write_config(budgets={"classes": [{"task_class": "summarize", "monthly_usd": 1,
                                                "on_exhaustion": "refuse"}]})
        self.assertEqual(self.go("summarize app.py").status, OK)            # 0.50
        self.assertEqual(self.go("summarize app.py").status, OK)            # 1.00: now spent
        r = self.go("summarize app.py")
        self.assertEqual(r.status, ESCALATED)
        self.assertIn("budget spent", r.error)
        self.assertEqual(len(self.launched()), 2)
        self.assertEqual(self.go("review app.py").status, OK)               # another class is unaffected
        self.assertEqual(cli.main(["report", "--by-class"]), 0)

    def test_compare_inherits_the_projects_tag(self):
        runs = control.compare("look", ["claude"], cwd=str(self.repo))
        self.assertEqual(runs[0][0].status, ESCALATED)
        self.tag(privacy="P0")
        runs = control.compare("look", ["claude"], cwd=str(self.repo))
        self.assertEqual(runs[0][0].status, OK)

    def test_daemon_marks_what_no_agent_may_do(self):
        req = spool.submit("look at app.py", cwd=str(self.repo))
        spool.work_once(log=lambda m: None)
        self.assertEqual(spool.load(req["id"])["status"], "needs_human")
        self.tag(privacy="P1")
        req = spool.submit("look at app.py", cwd=str(self.repo))
        spool.work_once(log=lambda m: None)
        done = spool.load(req["id"])
        self.assertEqual(done["status"], "needs_human")
        self.assertIn("supervised", done["result"]["error"])
        self.assertEqual(self.launched(), [])


class OutcomeLogTest(Base):
    def test_cli_recovers_empty_log_and_interleaved_retries_once(self):
        from concurrent.futures import ThreadPoolExecutor
        for task, folder in (("a", "/a"), ("b", "/b")):
            state.audit("request.received", task_id=task, cwd=folder)
        for task, attempt in (("a", 1), ("b", 1), ("a", 2)):
            state.audit("run.started", task_id=task, attempt=attempt)
            state.audit("run.finished", task_id=task, agent="claude", status="ok")
        outcomes.log_path().touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: cli.main(["report", "--recover"]), range(2))), [0, 0])
        self.assertEqual([(r["task_id"], r["attempt"], r["cwd"]) for r in outcomes.read_all()],
                         [("a", 1, "/a"), ("b", 1, "/b"), ("a", 2, "/a")])
        self.assertEqual(outcomes.log_path().read_bytes(), outcomes.backup_path().read_bytes())
        self.assertEqual(outcomes.recover_from_audit(), [])

    def test_a_log_that_shrinks_is_restored_and_says_so(self):
        self.tag(privacy="P1")
        self.go("look at app.py")
        self.go("summarize app.py")
        log = self.home / "outcomes.jsonl"
        full = log.read_text()
        self.assertEqual(len(full.splitlines()), 2)
        log.write_text("")                                   # emptied by something outside orchestra
        self.assertEqual(len(outcomes.read_all()), 2)        # reading puts it back
        self.assertEqual(log.read_text(), full)
        events = [json.loads(l)["event"] for l in (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("outcomes.restored", events)
        log.write_text(full.splitlines(True)[0])             # or merely shortened
        self.go("review app.py")
        self.assertEqual(len(outcomes.read_all()), 3)
        self.assertEqual(log.read_text(), (self.home / "outcomes.jsonl.bak").read_text())

    def test_first_run_after_upgrade_adopts_the_existing_log(self):
        log = self.home / "outcomes.jsonl"
        log.write_text(json.dumps({"ts": "2026-11-19", "agent": "claude", "status": "ok"}) + "\n")
        self.assertEqual(len(outcomes.read_all()), 1)
        self.assertEqual((self.home / "outcomes.jsonl.bak").read_text(), log.read_text())

    def test_history_can_be_rebuilt_from_the_audit_log(self):
        self.tag(privacy="P1")
        a = self.go("look at app.py")
        b = self.go("change app.py", access=EDIT)
        (self.home / "outcomes.jsonl").unlink()
        (self.home / "outcomes.jsonl.bak").unlink()           # both copies gone
        added = outcomes.recover_from_audit()
        self.assertEqual([e["task_id"] for e in added], [a.task_id, b.task_id])
        self.assertEqual([e["cost_usd"] for e in added], [0.5, 0.5])
        self.assertEqual(added[1]["gate"], "pending")
        self.assertTrue(added[0]["brief"].startswith("[recovered"))
        self.assertEqual(outcomes.recover_from_audit(), [])   # running it twice adds nothing
        self.assertEqual(cli.main(["report", "--recover"]), 0)
        self.assertEqual(cli.main(["report"]), 0)
        self.assertAlmostEqual(router.spend_this_month(), 1.0)


class PrivacyWallTest(Base):
    def test_wall_holds_against_a_named_agent_and_every_class(self):
        for privacy in ("P3", "P4"):
            self.tag(privacy=privacy)
            r = self.go(agent="claude")
            self.assertEqual(r.status, ESCALATED)
        self.assertEqual(self.launched(), [])
        self.tag(privacy="P2")
        self.assertEqual(self.go().status, ESCALATED)                       # nobody approved yet
        self.write_config(routing={"approved_providers": ["codex"]})
        r = self.go()
        self.assertEqual((r.status, r.agent), (OK, "codex"))
        self.assertEqual(self.go(agent="claude").status, ESCALATED)         # approved means that one only

    def test_p3_exception_is_named_and_logged_and_p4_has_none(self):
        self.tag(privacy="P3")
        exception = {"task_class": "triage", "agent": "claude", "approved_by": "mike", "reason": "DPA"}
        self.write_config(routing={"p3_exceptions": [exception]})
        self.assertEqual(self.go(meta={"task_class": "other"}).status, ESCALATED)
        r = self.go(meta={"task_class": "triage"})
        self.assertEqual((r.status, r.agent), (OK, "claude"))
        events = [json.loads(l) for l in (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        used = [e for e in events if e["event"] == "privacy.exception_used"]
        self.assertIn("approved by mike", used[0]["why"])
        self.tag(privacy="P4")
        self.assertEqual(self.go(meta={"task_class": "triage"}).status, ESCALATED)
        self.assertEqual(cli.main(["report", "--by-privacy"]), 0)

    def test_enforcement_does_not_depend_on_the_router(self):
        """A routing bug must not become a privacy incident (10.6)."""
        self.tag(privacy="P4")
        real = router.decide
        router.decide = lambda *a, **k: dict(real(*a, **k), executor="claude", outcome="delegate")
        self.addCleanup(setattr, router, "decide", real)
        r = self.go()
        self.assertEqual(r.status, ESCALATED)
        self.assertIn("privacy wall", r.error)
        self.assertEqual(self.launched(), [])


class LocalModelTest(Base):
    local_model = True

    def test_private_work_is_done_locally_with_the_files_handed_over(self):
        self.tag(privacy="P4")
        r = self.go("summarize what top does", files=["app.py"])
        self.assertEqual((r.status, r.agent, r.result, r.cost_usd), (OK, "local", "local done", 0.0))
        self.assertEqual(r.route["outcome"], "act-direct")
        sent = LocalModel.received[-1]
        self.assertEqual(sent["model"], "stub-model")
        self.assertIn("def top():", sent["messages"][0]["content"])           # it cannot open files itself
        self.assertEqual(self.launched(), [])                                  # no cloud agent involved
        entry = outcomes.read_all()[-1]
        self.assertEqual((entry["location"], entry["privacy"]), ("local", "P4"))

    def test_routine_work_goes_local_and_harder_work_does_not(self):
        self.tag(privacy="P1")
        self.assertEqual(self.go("summarize app.py").agent, "local")
        self.assertEqual(self.decide("refactor everything across the whole codebase",
                                     meta={"stakes": "low"})["executor"], "claude")
        self.assertEqual(self.decide(access="edit")["executor"], "claude")     # the local model cannot edit
        self.tag(privacy="P4")
        self.assertIsNone(self.decide(access="edit")["executor"])              # so private edits wait for a person

    def test_budget_downgrades_to_local(self):
        self.tag(privacy="P1")
        self.write_config(budgets={"classes": [{"task_class": "refactor", "monthly_usd": 0.4,
                                                "on_exhaustion": "downgrade_to_local"}]})
        big = "refactor naming across the whole codebase"
        self.assertEqual(self.go(big, meta={"stakes": "low"}).agent, "claude")
        r = self.go(big, meta={"stakes": "low"})
        self.assertEqual(r.agent, "local")
        self.assertTrue(any("downgraded" in x for x in r.route["reasons"]))

    def test_local_must_mean_local(self):
        self.write_config(local={"base_url": "http://example.com:11434"})
        from orchestra import adapters
        self.assertFalse(adapters.get("local").available())
        self.assertIn("this machine", adapters.get("local").delegate("x").error)
        self.assertEqual(cli.main(["agents", "-v"]), 0)


class MemoryTest(Base):
    def setUp(self):
        super().setUp()
        self.tag(privacy="P1")

    def test_review_states_decide_what_reaches_a_brief(self):
        scope = memory.scope_for(project=str(self.repo))
        mine = memory.add("the invoice parser must keep Python 3.9 support", scope, type="warning")
        theirs = memory.add("the invoice parser is safe to rewrite in Rust", scope,
                            source="agent:mcp", confidence="inferred")
        memory.add("deploys of the invoice parser happen on Fridays",
                   memory.scope_for(project="/somewhere/else"))
        self.go("fix a bug in the invoice parser")
        prompt = (self.home / "prompt-claude.txt").read_text()
        self.assertIn("[tentative, not yet reviewed", prompt)                 # mine: a person's proposal
        self.assertIn("Python 3.9", prompt)
        self.assertNotIn("Rust", prompt)                                      # an agent's proposal waits
        self.assertNotIn("Fridays", prompt)                                   # another project's memory
        self.assertEqual(outcomes.read_all()[-1]["memories_used"], [mine["id"]])

        self.assertEqual(cli.main(["memory", "accept", theirs["id"]]), 0)
        self.assertEqual(cli.main(["memory", "accept", mine["id"]]), 0)
        self.go("fix another bug in the invoice parser")
        prompt = (self.home / "prompt-claude.txt").read_text()
        self.assertIn("[confirmed by the operator", prompt)
        self.assertIn("Rust", prompt)

        # The Rust note turns out to be wrong: trace it, then pull it (7.11).
        self.assertEqual(len(memory.used_by(theirs["id"])), 1)
        self.assertEqual(cli.main(["memory", "quarantine", theirs["id"]]), 0)
        self.go("fix a third bug in the invoice parser")
        self.assertNotIn("Rust", (self.home / "prompt-claude.txt").read_text())
        self.assertEqual(cli.main(["memory", "list"]), 0)
        self.assertEqual(cli.main(["memory", "show", mine["id"]]), 0)
        self.assertEqual(cli.main(["recall", "invoice parser", "--cwd", str(self.repo)]), 0)

    def test_notes_become_curated_records_and_old_notes_still_count(self):
        r = self.go("tidy imports in app.py")
        self.assertEqual(cli.main(["note", r.task_id, "keep imports alphabetical"]), 0)
        rec = memory.load()[-1]
        self.assertEqual((rec["state"], rec["source"], rec["evidence"]), ("curated", "human", r.task_id))
        with (self.home / "notes.jsonl").open("a") as f:                       # written by the chapter 5 build
            f.write(json.dumps({"ts": "2026-11-19", "task_id": r.task_id, "note": "never touch app.py headers"}) + "\n")
        self.go("rename a variable in app.py")
        prompt = (self.home / "prompt-claude.txt").read_text()
        self.assertIn("keep imports alphabetical", prompt)
        self.assertIn("never touch app.py headers", prompt)

    def test_scopes_core_promotion_revisit_and_refusals(self):
        everywhere = memory.add("commit messages use the imperative mood", "global", state_="curated")
        klass = memory.add("docstring work needs no tests run", memory.scope_for(task_class="docs"),
                           state_="curated", revisit="2020-01-01")
        self.go("write a commit message in the imperative mood")
        self.assertIn(everywhere["id"], outcomes.read_all()[-1]["memories_used"])
        self.go("docstring work on app.py", meta={"task_class": "docs"})
        self.assertIn("due for re-checking", (self.home / "prompt-claude.txt").read_text())
        self.assertTrue(memory.is_due(memory.find(klass["id"])))
        self.assertEqual(cli.main(["memory", "list", "--due"]), 0)

        proposed = memory.add("something unreviewed", "global")
        with self.assertRaises(memory.MemoryError_):
            memory.promote_to_core(proposed["id"])                             # only curated may be promoted
        memory.promote_to_core(everywhere["id"])
        self.assertIn("imperative mood", (self.home / "MEMORY.md").read_text())
        with self.assertRaises(memory.MemoryError_):
            memory.add("the key is AKIAIOSFODNN7EXAMPLE", "global")            # no credentials in memory
        with self.assertRaises(memory.MemoryError_):
            memory.add("x" * 700, "global")
        self.assertEqual(cli.main(["memory", "propose", "no scope given"]), 2)
        self.assertEqual(cli.main(["state"]), 0)

    def test_quarantine_filters_new_and_legacy_core_promotions(self):
        old = memory.add("prior system preference", "global", state_="curated")
        new = memory.add("current system preference", "global", state_="curated")
        core = self.home / "MEMORY.md"
        core.write_text("Operator guidance\n- prior system preference\n")
        state.audit("memory.promoted_to_core", memory_id=old["id"])
        memory.promote_to_core(new["id"])
        original = core.read_bytes()
        self.go("inspect the readme")
        self.assertEqual(set(outcomes.read_all()[-1]["memories_used"]), {old["id"], new["id"]})
        for record in (new, old):
            memory.set_state(record["id"], "quarantined")
        self.go("inspect the readme")
        prompt = (self.home / "prompt-claude.txt").read_text()
        self.assertIn("Operator guidance", prompt)
        self.assertNotIn("system preference", prompt)
        self.assertEqual(core.read_bytes(), original)
        memory.set_state(new["id"], "curated")
        memory.promote_to_core(new["id"])
        rendered = dict(state.bounded_memory())["MEMORY.md"]
        self.assertEqual(rendered.count("current system preference"), 1)
        self.assertNotIn("prior system preference", rendered)


class SkillsTest(Base):
    def setUp(self):
        super().setUp()
        self.tag(privacy="P1")
        self.assertEqual(skills.install_examples(), ["check-doc-links", "careful-code-change"])

    def test_a_draft_is_listed_but_never_used(self):
        self.assertEqual(skills.summary("check-doc-links")["status"], "draft")
        decision, log = control.route("check the docs for broken links", cwd=str(self.repo))
        self.assertIsNone(log["chosen"])
        self.assertIn("draft", log["candidates"][0]["rejected"])
        self.assertEqual(decision["executor"], "claude")
        self.assertEqual(cli.main(["skill", "list"]), 0)
        self.assertEqual(cli.main(["skill", "show", "check-doc-links"]), 0)

    def test_trusted_native_skill_does_the_work_itself(self):
        self.assertEqual(cli.main(["skill", "test", "check-doc-links"]), 0)
        self.assertEqual(cli.main(["skill", "trust", "check-doc-links"]), 0)
        r = self.go("check the docs for broken links")
        self.assertEqual((r.status, r.agent, r.route["outcome"]), (OK, "skill:check-doc-links", "act-direct"))
        self.assertIn("1 broken", r.result)
        self.assertIn("README.md:3 -> docs/missing.md", r.result)
        self.assertEqual(self.launched(), [])                                  # no model at all
        entry = outcomes.read_all()[-1]
        self.assertEqual((entry["skill"], entry["location"], entry["cost_usd"]),
                         ("check-doc-links@1.0", "local", 0.0))
        self.assertEqual(skills.usage()["check-doc-links"], {"used": 1, "failed": 0})
        # It works on private data too: nothing leaves the machine.
        self.tag(privacy="P4")
        self.assertEqual(self.go("check the docs for broken links").agent, "skill:check-doc-links")
        # And an unrelated task does not reach for it.
        self.tag(privacy="P1")
        self.assertEqual(self.go("look at app.py", use_skills=True).agent, "claude")
        self.assertEqual(self.go("check the docs for broken links", use_skills=False).agent, "claude")

    def test_editing_a_trusted_skill_revokes_its_trust(self):
        skills.trust("check-doc-links")
        selected = skills.SkillRunner("check-doc-links", str(self.repo), [])
        script = skills.root() / "check-doc-links" / "scripts" / "check_links.py"
        script.write_text(script.read_text() + "\n# a small, innocent-looking change\n")
        self.assertEqual(skills.summary("check-doc-links")["status"], "modified")
        r = self.go("check the docs for broken links")
        self.assertEqual(r.agent, "claude")
        forced = self.go("check the docs for broken links", skill="check-doc-links")
        self.assertEqual(forced.status, ERROR)                                  # naming it does not help
        self.assertIn("review it again", forced.error)
        skills.trust("check-doc-links")
        self.assertEqual(selected.delegate("old selection", cwd=str(self.repo)).status, ERROR)
        self.assertEqual(skills.summary("check-doc-links")["status"], "trusted")
        skills.deprecate("check-doc-links")
        self.assertEqual(self.go("check the docs for broken links").agent, "claude")

    def test_a_skill_whose_own_test_fails_cannot_be_trusted(self):
        script = skills.root() / "check-doc-links" / "scripts" / "check_links.py"
        script.write_text(script.read_text().replace("checked == 3", "checked == 99"))
        with self.assertRaisesRegex(skills.SkillError, "own test fails"):
            skills.trust("check-doc-links")
        self.assertEqual(cli.main(["skill", "new", "audit-wiki", "--category", "native"]), 0)
        with self.assertRaises(skills.SkillError):
            skills.trust("audit-wiki")                                          # still a TODO stub
        self.assertEqual(cli.main(["skill", "new", "Bad Name"]), 2)

    def test_delegation_skill_shapes_the_brief(self):
        with self.assertRaisesRegex(skills.SkillError, "declares no `test:`"):
            skills.trust("careful-code-change")
        skills.trust("careful-code-change", without_test=True)
        r = self.go("fix the bug in top and update app.py", access=EDIT, files=["app.py"])
        self.assertEqual((r.status, r.agent), (OK, "claude"))
        prompt = (self.home / "prompt-claude.txt").read_text()
        self.assertIn("# Procedure (skill: careful-code-change@1.0)", prompt)
        self.assertIn("Make the smallest change that does the task", prompt)
        self.assertEqual(outcomes.read_all()[-1]["skill"], "careful-code-change@1.0")
        # It is not declared safe unattended, so the daemon's request does not get it.
        _, log = control.route("fix the bug in top and update app.py", cwd=str(self.repo),
                               access="edit", source="daemon")
        self.assertIn("unattended", log["candidates"][0]["rejected"])
        events = [json.loads(l)["event"] for l in (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("skill.match", events)                                    # the match log (8.4)


class McpTest(Base):
    def call(self, method, params=None, rid=1):
        return mcp_server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def tool(self, name, **args):
        reply = self.call("tools/call", {"name": name, "arguments": args})["result"]
        return reply["isError"], reply["content"][0]["text"]

    def test_lifecycle_over_stdio(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "nonsense"}]
        out = io.StringIO()
        mcp_server.serve(io.StringIO("\n".join(json.dumps(l) for l in lines) + "\nnot json\n"), out)
        replies = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(len(replies), 4)                                       # the notification gets none
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]],
                         ["search_outcomes", "get_curated_fact", "list_known_procedures",
                          "propose_memory_update"])
        self.assertEqual(replies[2]["error"]["code"], -32601)
        self.assertEqual(replies[3]["error"]["code"], -32700)
        old = self.call("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(old["result"]["protocolVersion"], mcp_server.SUPPORTED[0])

    def test_reads_are_narrow_and_keep_sensitive_work_out(self):
        self.tag(privacy="P1")
        self.go("summarize the invoice parser")
        outcomes.record(Result("local", OK), "summarize the invoice parser for customer Acme",
                        privacy="P4", project=str(self.repo))
        is_error, text = self.tool("search_outcomes", query="invoice parser")
        found = json.loads(text)
        self.assertFalse(is_error)
        self.assertEqual([f["task"] for f in found], ["summarize the invoice parser"])
        self.assertNotIn("result", found[0])                                    # never what an agent said
        memory.add("invoices are parsed with pdfplumber", "global", state_="curated")
        memory.add("invoices should be parsed with regex", "global")            # only proposed
        facts = json.loads(self.tool("get_curated_fact", topic="invoices parsed")[1])
        self.assertEqual([f["summary"] for f in facts], ["invoices are parsed with pdfplumber"])
        skills.install_examples()
        self.assertEqual(json.loads(self.tool("list_known_procedures")[1]), [])  # drafts are not offered
        skills.trust("check-doc-links")
        self.assertEqual(json.loads(self.tool("list_known_procedures")[1])[0]["name"], "check-doc-links")

    def test_the_only_write_is_a_proposal(self):
        is_error, text = self.tool("propose_memory_update", summary="tests live in tests/unit",
                                   project=str(self.repo))
        rec = memory.find(json.loads(text)["id"])
        self.assertEqual((rec["state"], rec["source"], rec["confidence"]),
                         ("proposed", "agent:mcp", "inferred"))
        self.tag(privacy="P1")
        self.go("add tests in tests/unit for app.py")
        self.assertNotIn("tests live in", (self.home / "prompt-claude.txt").read_text())
        # Bad input is refused as a tool error, not a crash.
        for bad in ({"summary": ""}, {"summary": "x", "state": "curated"}, {"summary": 7},
                    {"summary": "key AKIAIOSFODNN7EXAMPLE"}, {"summary": "x", "type": "gospel"}):
            self.assertTrue(self.tool("propose_memory_update", **bad)[0], bad)
        self.assertTrue(self.tool("search_outcomes", query="x", limit=500)[0])
        self.assertEqual(self.call("tools/call", {"name": "edit_curated_memory"})["error"]["code"], -32602)

    def test_registrations_say_what_is_missing(self):
        self.write_config(mcp={"servers": [
            {"name": "tracker", "purpose": "issues", "transport": "stdio", "command": "tracker-mcp",
             "credentials": "TRACKER_READ_TOKEN", "owner": "mike", "reviewed": "2026-11-20",
             "tools": [{"name": "list_issues", "access": "read", "gated": False},
                       {"name": "close_issue", "access": "write", "gated": False}]},
            {"name": "mystery"}]})
        memory_srv, tracker, mystery = mcp_server.registrations()
        self.assertEqual(memory_srv["missing"], [])
        self.assertEqual(tracker["missing"], ["tool close_issue writes but is not gated"])
        self.assertIn("command", mystery["missing"])
        self.assertEqual(cli.main(["mcp", "list"]), 0)


if __name__ == "__main__":
    unittest.main()
