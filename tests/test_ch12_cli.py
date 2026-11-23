import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestra import cli, config, spool, state, stop_switch


class QueueCommandsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "state"
        self.project = Path(tmp.name) / "work"
        self.project.mkdir()
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        state.init()

    def command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_schedule_lifecycle_while_global_clock_disabled(self):
        code, out, _ = self.command("schedule", "add", "docs", "check guides", "--every", "60",
                                    "--skill", "check-doc-links", "--cwd", str(self.project))
        self.assertEqual(code, 0)
        self.assertIn("scheduler.enabled", out)
        self.assertIn("every 60s", self.command("schedule", "list")[1])
        self.assertIn("paused", self.command("schedule", "pause", "docs")[1])
        self.assertIn("active", self.command("schedule", "resume", "docs")[1])
        self.assertEqual(self.command("schedule", "remove", "docs")[0], 0)
        self.assertIn("No scheduled jobs", self.command("schedule", "list")[1])
        self.assertFalse(spool.all_requests())

    def test_duplicate_submission_and_operator_resolution_have_one_trace(self):
        submit = ("submit", "review docs", "--cwd", str(self.project), "--idempotency-key", "docs-v5")
        code, request_id, _ = self.command(*submit)
        self.assertEqual(code, 0)
        self.assertEqual(self.command(*submit)[1], request_id)
        req = spool.load(request_id)
        req.update(status="needs_human", result={"error": "requires supervision"}, task_id="t-fixture")
        spool._save(req)
        state.audit("route.decided", request_id=request_id, task_id=req["task_id"])
        state.audit("review.prepared", task_id=req["task_id"])
        state.audit("unrelated", request_id="another")
        self.assertIn("requires supervision", self.command("requests", "--last", "0")[1])
        self.assertEqual(self.command("resolve", request_id, "--reason", "handled in person")[0], 0)
        self.assertEqual(spool.load(request_id)["status"], "resolved")
        self.assertIn("no requests", self.command("requests", "--last", "0")[1])
        events = [json.loads(line)["event"] for line in self.command("trace", request_id)[1].splitlines()]
        self.assertIn("request.submitted", events)
        self.assertIn("review.prepared", events)
        self.assertIn("request.resolved", events)
        self.assertNotIn("unrelated", events)

    def test_stop_generation_survives_resume(self):
        self.assertEqual(stop_switch.generation(), "")
        stop_switch.stop()
        first = stop_switch.generation()
        self.assertTrue(first)
        stop_switch.resume()
        self.assertEqual(stop_switch.generation(), first)
        stop_switch.stop()
        self.assertNotEqual(stop_switch.generation(), first)

    def test_invalid_scheduler_and_worker_settings_fail_closed(self):
        for setting in ("scheduler: {enabled: 'false'}", "daemon: {workers: true}",
                        "daemon: {workers: 9}", "daemon: {poll_seconds: .inf}",
                        "approvals: null", "budgets: {classes: [null]}"):
            with self.subTest(setting=setting):
                (self.home / "config.yaml").write_text(setting)
                with self.assertRaises(config.ConfigError):
                    config.load()
