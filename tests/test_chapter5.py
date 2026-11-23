"""Chapter 5: the brief, the secrets check, validation, the outcome record,
environment isolation, bounded retries and the depth cap."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import disable_live_providers

from orchestra import cli, config, control, outcomes, state, tasks
from orchestra.contract import DENIED, EDIT, ERROR, OK, TIMEOUT

# A stand-in agent. It records the brief and the environment it was given.
#   BEHAVE=wander  edits a second file as well as the one it was asked about
#   BEHAVE=silent  reports success with an empty answer
#   BEHAVE=flaky   returns garbage the first time it is called, then works
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path(os.environ["ORCHESTRA_HOME"])
brief, edit, mode = sys.argv[-1], "acceptEdits" in sys.argv, os.environ.get("BEHAVE", "")
(home / "last-prompt.txt").write_text(brief)
(home / "last-env.json").write_text(json.dumps(dict(os.environ)))
calls = home / "calls.txt"
calls.write_text(calls.read_text() + "x" if calls.exists() else "x")
if mode == "flaky" and len(calls.read_text()) == 1:
    print("Segmentation fault"); sys.exit(0)
if edit:
    Path("app.py").write_text(Path("app.py").read_text() + "# doc\n")
    if mode == "wander":
        Path("other.py").write_text("# uninvited\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "" if mode == "silent" else "done", "total_cost_usd": 0.01}))
'''


class Chapter5Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        fake = root / "fake-claude"
        fake.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self._env = dict(os.environ)
        disable_live_providers(self)
        self.addCleanup(self._restore_env)
        self.home = root / "home"
        self.home.mkdir(exist_ok=True)
        (self.home / "config.yaml").write_text("agents:\n  claude:\n    env: [BEHAVE]\n")
        os.environ["ORCHESTRA_CLAUDE_BIN"] = str(fake)
        os.environ["ORCHESTRA_HOME"] = str(self.home)
        # Everything in these tests is ordinary internal work. Without a declared
        # privacy class the router would (rightly) keep it away from cloud agents.
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects.yaml").write_text("/: {privacy: P1}\n")
        for name in ("BEHAVE", "ORCHESTRA_DEPTH"):
            os.environ.pop(name, None)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def top():\n    return 1\n")
        (self.repo / "other.py").write_text("x = 1\n")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def prompt(self) -> str:
        return (self.home / "last-prompt.txt").read_text()

    def calls(self) -> int:
        path = self.home / "calls.txt"
        return len(path.read_text()) if path.exists() else 0

    # --- the brief -----------------------------------------------------------
    def test_edit_from_symlinked_subdirectory_uses_private_copy(self):
        subdir = self.repo / "src"
        subdir.mkdir()
        (subdir / "app.py").write_text("def top():\n    return 1\n")
        alias = self.repo.parent / "project-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        before = (subdir / "app.py").read_bytes()
        result = control.delegate("edit app.py", cwd=str(alias / "src"), access=EDIT, files=["app.py"])
        self.assertEqual(result.gate, "pending", result.error)
        self.assertEqual((subdir / "app.py").read_bytes(), before)
        self.assertEqual(tasks.load_task(result.task_id)["files"], ["src/app.py"])

    def test_brief_carries_guidance_scope_and_task_last(self):
        (self.repo / "AGENTS.md").write_text("Use four spaces. Never touch vendor/.\n")
        r = control.delegate("add a docstring to top", cwd=str(self.repo), files=["app.py"])
        prompt = self.prompt()
        # Claude has no CLAUDE.md here, so the one guidance file that exists briefs it.
        self.assertIn("# Project guidance (from AGENTS.md)", prompt)
        self.assertIn("Never touch vendor/.", prompt)
        self.assertIn("# Files in scope\n- app.py", prompt)
        self.assertTrue(prompt.endswith("Task:\nadd a docstring to top"))
        self.assertEqual((Path(r.artifacts) / "brief.md").read_text(), prompt)   # kept for audit
        self.assertEqual((Path(r.artifacts) / "result.md").read_text(), "done")
        entry = outcomes.read_all()[-1]
        self.assertEqual(entry["files_in_scope"], ["app.py"])
        self.assertEqual(entry["brief_chars"], len(prompt))
        self.assertEqual(len(entry["brief_digest"]), 12)
        self.assertEqual(entry["brief"], "add a docstring to top")   # the log keeps the task itself

    def test_agent_that_reads_its_own_file_is_not_briefed_twice(self):
        (self.repo / "AGENTS.md").write_text("Use four spaces.\n")
        from orchestra import adapters
        cfg = config.load()
        codex = tasks.assemble("do it", adapters.get("codex"), str(self.repo), [], cfg)
        self.assertNotIn("Use four spaces.", codex.text)
        claude = tasks.assemble("do it", adapters.get("claude"), str(self.repo), [], cfg)
        self.assertIn("Use four spaces.", claude.text)

    def test_guidance_is_capped_and_budget_is_flagged(self):
        (self.repo / "CLAUDE.md").write_text("x" * 20000)
        from orchestra import adapters
        brief = tasks.assemble("do it", adapters.get("claude"), str(self.repo), [], config.load())
        self.assertIn("truncated to 6000 characters", brief.text)
        self.assertFalse(brief.over_budget)
        (self.home / "config.yaml").write_text("brief:\n  max_chars: 100\n")
        brief = tasks.assemble("do it", adapters.get("claude"), str(self.repo), [], config.load())
        self.assertTrue(brief.over_budget)

    def test_relevant_memory_is_past_tasks_and_operator_notes_never_agent_output(self):
        first = control.delegate("refactor the invoice parser module", cwd=str(self.repo))
        self.assertEqual(cli.main(["note", first.task_id, "the parser must keep Python 3.9 support"]), 0)
        control.delegate("summarize the readme", cwd=str(self.repo))
        control.delegate("fix a bug in the invoice parser", cwd=str(self.repo))
        prompt = self.prompt()
        self.assertIn("# What we already know", prompt)
        self.assertIn("refactor the invoice parser module", prompt)
        self.assertIn("[confirmed by the operator", prompt)
        self.assertIn("the parser must keep Python 3.9 support", prompt)
        self.assertNotIn("summarize the readme", prompt)        # unrelated history stays out
        self.assertNotIn("done", prompt.split("Task:")[0])      # the agent's own words are not fed back
        # A shared file name is enough to connect two tasks.
        control.delegate("tidy imports in app.py", cwd=str(self.repo))
        control.delegate("rename a variable in app.py", cwd=str(self.repo))
        self.assertIn("tidy imports in app.py", self.prompt().split("Task:")[0])
        # Another project sees none of it.
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        control.delegate("fix a bug in the invoice parser", cwd=str(other))
        self.assertEqual(self.prompt(), "fix a bug in the invoice parser")

    def test_scope_must_be_inside_the_project(self):
        r = control.delegate("x", cwd=str(self.repo), files=["../outside.py"])
        self.assertEqual(r.status, ERROR)
        self.assertIn("outside the project", r.error)
        self.assertEqual(self.calls(), 0)

    def test_dry_run_sends_nothing(self):
        self.assertEqual(cli.main(["delegate", "look at it", "--cwd", str(self.repo),
                                   "--file", "app.py", "--dry-run"]), 0)
        self.assertEqual(self.calls(), 0)
        self.assertEqual(outcomes.read_all(), [])

    # --- secrets ---------------------------------------------------------------
    def test_a_brief_containing_a_secret_is_never_sent(self):
        (self.home / ".env").write_text("DB_PASSWORD=hunter2-hunter2\n")
        cases = [("connect with hunter2-hunter2 and list tables", ".env"),
                 ("use sk-ant-" + "a1B2" * 8 + " to call the API", "Anthropic API key")]
        os.environ["DEPLOY_TOKEN"] = "tok_live_0123456789"
        cases.append(("deploy with tok_live_0123456789", "DEPLOY_TOKEN"))
        for task, label in cases:
            r = control.delegate(task, cwd=str(self.repo))
            self.assertEqual(r.status, ERROR)
            self.assertIn(label, r.error)
            self.assertNotIn(task.split()[2], r.error)                  # the value is never echoed
        self.assertEqual(self.calls(), 0)
        # Nothing orchestra wrote to disk contains the values either.
        written = "".join(p.read_text(errors="replace") for p in self.home.rglob("*")
                          if p.is_file() and p.name != ".env")
        for value in ("hunter2", "a1B2a1B2", "tok_live"):
            self.assertNotIn(value, written)
        self.assertTrue(all(e["brief"].startswith("[withheld") for e in outcomes.read_all()))
        from orchestra import spool
        with self.assertRaises(spool.SpoolError):
            spool.submit("deploy with tok_live_0123456789", cwd=str(self.repo))
        self.assertEqual(spool.all_requests(), [])
        # A secret sitting in the project's guidance file is caught the same way.
        (self.repo / "CLAUDE.md").write_text("The staging key is AKIAIOSFODNN7EXAMPLE\n")
        r = control.delegate("tidy up", cwd=str(self.repo))
        self.assertIn("AWS access key", r.error)
        self.assertEqual(self.calls(), 0)

    def test_agent_sees_only_its_declared_environment(self):
        os.environ.update({"ANTHROPIC_API_KEY": "k-anthropic", "OPENAI_API_KEY": "k-openai",
                           "AWS_SECRET_ACCESS_KEY": "aws-secret", "GITHUB_TOKEN": "gh-token",
                           "BEHAVE": ""})
        control.delegate("look", cwd=str(self.repo))
        seen = json.loads((self.home / "last-env.json").read_text())
        self.assertEqual(seen.get("ANTHROPIC_API_KEY"), "k-anthropic")    # its own credential
        for name in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "ORCHESTRA_CLAUDE_BIN"):
            self.assertNotIn(name, seen)                                  # nobody else's
        self.assertIn("PATH", seen)
        self.assertEqual(seen["ORCHESTRA_DEPTH"], "1")
        self.assertEqual(cli.main(["agents", "-v"]), 0)

    # --- validation -------------------------------------------------------------
    def test_edit_outside_declared_scope_fails_validation_and_is_held_back(self):
        os.environ["BEHAVE"] = "wander"
        r = control.delegate("document top", cwd=str(self.repo), access=EDIT, files=["app.py"])
        self.assertEqual((r.status, r.gate), (ERROR, "blocked"))
        self.assertIn("outside the declared scope: other.py", r.error)
        entry = outcomes.read_all()[-1]
        self.assertFalse(entry["validation"]["ok"])
        with self.assertRaisesRegex(tasks.TaskError, "failed validation"):
            tasks.apply(r.task_id)
        self.assertEqual((self.repo / "other.py").read_text(), "x = 1\n")
        tasks.apply(r.task_id, anyway=True)                               # a human may still overrule
        self.assertEqual((self.repo / "other.py").read_text(), "# uninvited\n")
        # Without a declared scope the same run is simply a diff to review.
        os.environ["BEHAVE"] = "wander"
        r = control.delegate("document top again", cwd=str(self.repo), access=EDIT)
        self.assertEqual((r.status, r.gate), (OK, "pending"))

    def test_success_with_no_answer_is_not_success(self):
        os.environ["BEHAVE"] = "silent"
        r = control.delegate("look", cwd=str(self.repo))
        self.assertEqual(r.status, ERROR)
        self.assertIn("returned no answer", r.error)

    # --- retries and recursion ---------------------------------------------------
    def test_read_run_is_retried_once_on_an_unreadable_reply(self):
        os.environ["BEHAVE"] = "flaky"
        r = control.delegate("look", cwd=str(self.repo))
        self.assertEqual(r.status, OK)
        attempts = [(e["attempt"], e["status"]) for e in outcomes.read_all()]
        self.assertEqual(attempts, [(1, ERROR), (2, OK)])                 # both are on the record
        self.assertEqual(len({e["task_id"] for e in outcomes.read_all()}), 1)

    def test_edit_run_is_never_retried(self):
        os.environ["BEHAVE"] = "flaky"
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        self.assertEqual(r.status, ERROR)
        self.assertEqual(self.calls(), 1)

    def test_depth_cap_stops_an_agent_delegating_again(self):
        os.environ["ORCHESTRA_DEPTH"] = "1"      # as if we were started by an orchestra agent
        r = control.delegate("look", cwd=str(self.repo))
        self.assertEqual(r.status, DENIED)
        self.assertIn("depth cap", r.error)
        self.assertEqual(self.calls(), 0)

    # --- the outcome record ---------------------------------------------------------
    def test_outcome_record_is_regular(self):
        control.delegate("look", cwd=str(self.repo))
        e = outcomes.read_all()[-1]
        for key in ("task_id", "task_class", "project", "agent", "adapter_version", "brief_digest",
                    "status", "exit_code", "started", "finished", "duration_ms", "cost_usd",
                    "artifacts", "tests_run", "validation", "gate", "attempt", "access", "source"):
            self.assertIn(key, e)
        self.assertEqual(e["project"], str(self.repo.resolve()))
        self.assertTrue(e["task_id"].startswith("t-"))
        self.assertLessEqual(e["started"], e["finished"])
        self.assertEqual(cli.main(["state"]), 0)


if __name__ == "__main__":
    unittest.main()
