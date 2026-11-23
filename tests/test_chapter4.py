"""Chapter 4: state directory, config and secrets, bounded memory, the human gate,
the hardline floor, and the daemon's durable spool. Stand-in agent throughout."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import disable_live_providers

from orchestra import backends, cli, config, control, outcomes, spool, state, tasks
from orchestra.adapters.claude import ClaudeAdapter
from orchestra.adapters.local import LocalAdapter
from orchestra.contract import DENIED, EDIT, ERROR, OK

# Records the exact prompt it was given, and edits when allowed to.
FAKE_CLAUDE = """#!/bin/sh
for last; do :; done
printf '%s' "$last" > "$ORCHESTRA_HOME/last-prompt.txt"
echo ran >> "$ORCHESTRA_HOME/launches.txt"
edit=no; for a; do [ "$a" = "acceptEdits" ] && edit=yes; done
[ "$edit" = yes ] && echo '# doc' >> app.py
case "$last" in *slow*) sleep 30 ;; esac
echo '{"type":"result","subtype":"success","is_error":false,"result":"done","total_cost_usd":0.1,"duration_ms":7}'
"""


def run_fixture_daemon():
    # The fixture is a local shell script using the Claude reply format.
    with patch.object(ClaudeAdapter, "location", "local"), patch.object(LocalAdapter, "models", return_value=[]):
        spool.run()


class Chapter4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        fake = root / "fake-claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self._env = dict(os.environ)
        disable_live_providers(self)
        self.addCleanup(self._restore_env)
        self.home = root / "home"
        os.environ["ORCHESTRA_CLAUDE_BIN"] = str(fake)
        os.environ["ORCHESTRA_HOME"] = str(self.home)
        # Everything in these tests is ordinary internal work. Without a declared
        # privacy class the router would (rightly) keep it away from cloud agents.
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects.yaml").write_text("/: {privacy: P1}\n")
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def top():\n    return 1\n")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def launches(self) -> int:
        path = self.home / "launches.txt"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def set_config(self, text: str):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text(text)

    # --- state directory -------------------------------------------------
    def test_init_creates_readable_state_and_never_overwrites(self):
        created = state.init()
        for name in ("config.yaml", ".env", "SOUL.md", "MEMORY.md", "USER.md", "skills/", "requests/"):
            self.assertIn(name, created)
        self.assertEqual(stat.S_IMODE((self.home / ".env").stat().st_mode), 0o600)
        (self.home / "SOUL.md").write_text("my own rules\n")
        self.assertEqual(state.init(), [])
        self.assertEqual((self.home / "SOUL.md").read_text(), "my own rules\n")
        self.assertEqual(config.load()["approvals"]["mode"], "manual")  # template parses
        text = "\n".join(state.describe())
        self.assertIn("approvals=manual", text)
        self.assertIn("daemon         not running", text)
        self.assertEqual(cli.main(["state"]), 0)

    def test_state_before_init(self):
        os.environ["ORCHESTRA_HOME"] = str(Path(self.tmp.name) / "never-created")
        self.assertIn("orchestra init", "\n".join(state.describe()))

    # --- config and secrets ----------------------------------------------
    def test_config_merges_substitutes_and_validates(self):
        state.init()
        (self.home / ".env").write_text("# comment\nAPI_TOKEN='s3cret'\n")
        self.set_config("defaults:\n  timeout: 42\nmodel:\n  name: ${API_TOKEN}\n")
        cfg = config.load()
        self.assertEqual(cfg["defaults"]["timeout"], 42)
        self.assertEqual(cfg["defaults"]["agent"], "claude")  # untouched default survives
        self.assertEqual(cfg["model"]["name"], "s3cret")
        self.set_config("model:\n  name: ${MISSING}\n")
        with self.assertRaises(config.ConfigError):
            config.load()
        self.set_config("approvals:\n  mode: yolo\n")
        with self.assertRaises(config.ConfigError):
            config.load()
        self.set_config("terminal:\n  backend: docker\n")
        with self.assertRaisesRegex(config.ConfigError, "not built yet"):
            config.load()
        self.assertEqual(cli.main(["delegate", "x"]), 2)  # reported, not a traceback

    def test_secrets_never_reach_the_agent(self):
        state.init()
        (self.home / ".env").write_text("API_TOKEN=s3cret\n")
        control.delegate("hello", cwd=str(self.repo))
        self.assertNotIn("s3cret", (self.home / "last-prompt.txt").read_text())
        self.assertNotIn("API_TOKEN", os.environ)

    # --- bounded memory ---------------------------------------------------
    def test_bounded_memory_is_sent_with_every_brief(self):
        control.delegate("the task", cwd=str(self.repo))
        self.assertEqual((self.home / "last-prompt.txt").read_text(), "the task")  # no state yet
        state.init()
        (self.home / "MEMORY.md").write_text("<!-- note to self -->\nThe wiki lives in docs/.\n")
        control.delegate("the task", cwd=str(self.repo))
        prompt = (self.home / "last-prompt.txt").read_text()
        self.assertIn("## SOUL.md", prompt)
        self.assertIn("The wiki lives in docs/.", prompt)
        self.assertNotIn("note to self", prompt)   # comments are stripped
        self.assertNotIn("USER.md", prompt)        # empty files are skipped
        self.assertTrue(prompt.endswith("Task:\nthe task"))
        self.assertEqual(outcomes.read_all()[-1]["brief"], "the task")  # log keeps the task itself

    def test_memory_budget_warning(self):
        state.init()
        (self.home / "MEMORY.md").write_text("x" * 5000)
        self.assertIn("over budget", "\n".join(state.describe()))

    # --- the human gate: review, then apply ----------------------------------
    def git_status(self) -> str:
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                              capture_output=True, text=True).stdout

    def test_edit_runs_wait_for_review_and_reads_do_not(self):
        r = control.delegate("look", cwd=str(self.repo))
        self.assertEqual((r.status, r.gate), (OK, "not_required"))
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        self.assertEqual((r.status, r.gate), (OK, "pending"))
        self.assertEqual(self.git_status(), "")                 # the real project is untouched
        self.assertEqual(outcomes.read_all()[-1]["gate"], "pending")
        task = tasks.load_task(r.task_id)
        self.assertEqual(task["files"], ["app.py"])
        self.assertIn("+# doc", tasks.patch_text(task))
        self.assertEqual(cli.main(["review"]), 0)
        self.assertEqual(cli.main(["review", r.task_id[-4:]]), 0)
        self.assertEqual(cli.main(["apply", r.task_id]), 0)
        self.assertEqual(self.git_status(), " M app.py\n")      # landed, uncommitted
        self.assertEqual(cli.main(["apply", r.task_id]), 2)      # a decision is final

    def test_discard_and_stale_diff(self):
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        self.assertEqual(cli.main(["discard", r.task_id]), 0)
        self.assertEqual(self.git_status(), "")
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        (self.repo / "app.py").write_text("completely different now\n")
        with self.assertRaisesRegex(tasks.TaskError, "no longer applies"):
            tasks.apply(r.task_id)
        self.assertEqual((self.repo / "app.py").read_text(), "completely different now\n")
        self.assertEqual(tasks.load_task(r.task_id)["status"], "pending")

    def test_uncommitted_work_is_carried_into_the_copy_and_kept_out_of_the_diff(self):
        (self.repo / "notes.txt").write_text("mine, not committed\n")
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        task = tasks.load_task(r.task_id)
        self.assertEqual(task["files"], ["app.py"])              # only the agent's change
        self.assertTrue((Path(r.artifacts) / "repo" / "notes.txt").exists())
        tasks.apply(r.task_id)
        self.assertEqual(sorted(self.git_status().split()), ["??", "M", "app.py", "notes.txt"])

    def test_edit_needs_a_git_repository(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        r = control.delegate("change it", cwd=str(plain), access=EDIT)
        self.assertEqual(r.status, ERROR)
        self.assertIn("git init", r.error)
        self.assertEqual(self.launches(), 0)

    def test_approvals_off_applies_automatically(self):
        self.set_config("approvals:\n  mode: off\n")
        # Reviews are off, but nothing lands unread unless it was declared low-stakes.
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        self.assertEqual(r.gate, "pending")
        self.assertEqual(self.git_status(), "")
        tasks.discard(r.task_id)
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT, meta={"stakes": "low"})
        self.assertEqual(r.gate, "auto_applied")
        self.assertEqual(self.git_status(), " M app.py\n")

    def test_audit_log_tells_the_story(self):
        r = control.delegate("change it", cwd=str(self.repo), access=EDIT)
        tasks.apply(r.task_id)
        events = [json.loads(l)["event"] for l in
                  (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["request.received", "route.decided", "run.started",
                                  "run.finished", "review.applied"])

    # --- access levels are stated, then verified ----------------------------
    def test_claude_flags_are_explicit_and_brief_stays_last(self):
        fake = Path(os.environ["ORCHESTRA_CLAUDE_BIN"])
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ORCHESTRA_HOME/argv.txt"\n'
                        "echo '{\"subtype\":\"success\",\"is_error\":false,\"result\":\"ok\"}'\n")
        control.delegate("the task", cwd=str(self.repo))
        argv = (self.home / "argv.txt").read_text().splitlines()
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--restricted", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__*")
        self.assertEqual(argv[-3:], ["--output-format", "json", "the task"])
        control.delegate("the task", cwd=str(self.repo), access=EDIT)
        argv = (self.home / "argv.txt").read_text().splitlines()
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("Bash", " ".join(argv))

    def test_read_run_that_edits_is_flagged(self):
        fake = Path(os.environ["ORCHESTRA_CLAUDE_BIN"])
        fake.write_text("#!/bin/sh\necho '# sneaky' >> app.py\n"
                        "echo '{\"subtype\":\"success\",\"is_error\":false,\"result\":\"done\"}'\n")
        r = control.delegate("just look", cwd=str(self.repo))
        self.assertEqual(r.status, ERROR)
        self.assertIn("access violation", r.error)
        self.assertEqual(outcomes.read_all()[-1]["status"], ERROR)
        events = [json.loads(l)["event"] for l in
                  (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("validation.failed", events)

    # --- hardline floor ----------------------------------------------------
    def test_floor_refuses_regardless_of_config(self):
        self.set_config("approvals:\n  mode: off\n")
        for cmd in (["claude", "--dangerously-skip-permissions"],
                    ["codex", "exec", "--sandbox", "danger-full-access"],
                    ["sh", "-c", "rm -rf /"], ["sh", "-c", "rm -fr ~"]):
            with self.assertRaises(backends.HardlineRefusal):
                backends.check_floor(cmd)
        backends.check_floor(["claude", "--print", "--output-format", "json"])
        backends.check_floor(["sh", "-c", "rm -rf ./build"])
        # A brief that only talks about a dangerous command is data, and runs.
        self.assertTrue(control.delegate("explain why rm -rf / is dangerous", cwd=str(self.repo)).ok)

    def test_floor_refusal_becomes_a_result(self):
        from orchestra.adapters.claude import ClaudeAdapter
        proc, failure, _ = ClaudeAdapter()._run(["claude", "--dangerously-skip-permissions"], None, 5)
        self.assertIsNone(proc)
        self.assertEqual(failure.status, ERROR)
        self.assertIn("hardline floor", failure.error)

    # --- daemon and spool ---------------------------------------------------
    @patch.object(ClaudeAdapter, "location", "local")
    def test_submit_run_and_read_back(self):
        req = spool.submit("look around", cwd=str(self.repo))
        self.assertEqual(req["status"], "pending")
        self.assertEqual(spool.work_once(log=lambda m: None), 1)
        done = spool.load(req["id"])
        self.assertEqual((done["status"], done["result"]["result"]), ("done", "done"))
        entry = outcomes.read_all()[-1]
        self.assertEqual((entry["source"], entry["request_id"]), ("daemon", req["id"]))
        self.assertEqual(spool.work_once(log=lambda m: None), 0)  # not run twice
        self.assertEqual(self.launches(), 1)

    @patch.object(ClaudeAdapter, "location", "local")
    def test_edit_request_lands_in_needs_review(self):
        req = spool.submit("change it", cwd=str(self.repo), access=EDIT)
        spool.work_once(log=lambda m: None)
        waiting = spool.load(req["id"])
        self.assertEqual(waiting["status"], "needs_review")
        self.assertEqual(self.git_status(), "")
        self.assertEqual(cli.main(["requests"]), 0)
        self.assertEqual(cli.main(["apply", waiting["task_id"]]), 0)
        self.assertEqual(spool.load(req["id"])["status"], "done")
        self.assertEqual(self.git_status(), " M app.py\n")
        self.assertEqual(spool.work_once(log=lambda m: None), 0)   # nothing runs twice

    def test_cancel_only_before_it_starts(self):
        req = spool.submit("look", cwd=str(self.repo))
        self.assertEqual(cli.main(["cancel", req["id"]]), 0)
        self.assertEqual(spool.work_once(log=lambda m: None), 0)
        self.assertEqual(self.launches(), 0)
        self.assertEqual(cli.main(["cancel", req["id"]]), 2)

    def test_submit_validates_at_the_door(self):
        self.assertEqual(cli.main(["submit", "x", "--agent", "nope"]), 2)
        self.assertEqual(cli.main(["submit", "x", "--cwd", "/no/such/dir"]), 2)
        self.assertEqual(spool.all_requests(), [])

    @patch.object(ClaudeAdapter, "location", "local")
    def test_restart_does_not_rerun_interrupted_work(self):
        """Book 4.14: kill the daemon mid-job; it must restart cleanly, not
        silently duplicate the job, and leave a record explaining it."""
        req = spool.submit("slow job", cwd=str(self.repo))
        waiting = spool.submit("next job", cwd=str(self.repo))
        root = str(Path(__file__).resolve().parents[1])
        daemon = subprocess.Popen([sys.executable, "-c", "from tests.test_chapter4 import run_fixture_daemon; "
                                   "run_fixture_daemon()"], cwd=root,
                                  env=dict(os.environ, PYTHONPATH=root),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Wait until the agent itself has started, not merely until the
            # request is marked running: routing happens in between.
            for _ in range(200):
                if spool.load(req["id"])["status"] == "running" and self.launches() == 1:
                    break
                time.sleep(0.1)
            self.assertEqual(spool.load(req["id"])["status"], "running")
            with self.assertRaises(spool.SpoolError):  # a second daemon is refused
                spool.run(once=True, log=lambda m: None)
        finally:
            daemon.kill()
            daemon.wait()
        self.assertEqual(self.launches(), 1)

        spool.run(once=True, log=lambda m: None)  # the restart
        self.assertEqual(spool.load(req["id"])["status"], "interrupted")
        self.assertEqual(spool.load(waiting["id"])["status"], "done")
        self.assertEqual(self.launches(), 2)  # only the waiting job ran; the slow one was not repeated
        events = [json.loads(l)["event"] for l in
                  (self.home / "logs" / "audit.jsonl").read_text().splitlines()]
        self.assertIn("request.interrupted", events)
        self.assertFalse((self.home / "daemon.pid").exists())


if __name__ == "__main__":
    unittest.main()
