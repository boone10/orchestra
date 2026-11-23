"""Stop admission, queue preservation, and bounded retries with stand-in agents."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import adapters, backends, config, control, outcomes, spool, stop_switch
from orchestra.contract import DENIED, EDIT, ERROR, OK, TIMEOUT, Result
from tests import disable_live_providers


class QueueStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.home = root / "state"
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        disable_live_providers(self)
        self.agent = adapters.get("claude")
        for stub in (patch.object(self.agent, "available", return_value=True),
                     patch.object(self.agent, "location", "local"),
                     patch.object(backends, "stop_skills", return_value=0)):
            stub.start()
            self.addCleanup(stub.stop)

    def submit(self, brief):
        return spool.submit(brief, agent="claude", cwd=str(self.project))["id"]

    def started_attempts(self):
        events = (self.home / "logs/audit.jsonl").read_text().splitlines()
        return [event["attempt"] for event in map(json.loads, events) if event["event"] == "run.started"]

    def test_stop_mid_batch_leaves_waiting_work_until_resume(self):
        first = self.submit("busy")
        next_job = self.submit("waiting")

        def interrupted(*args, **kwargs):
            stop_switch.stop()
            return Result("claude", ERROR, error="interrupted fixture")

        with patch.object(self.agent, "delegate", side_effect=interrupted) as launch:
            self.assertEqual(spool.work_once(log=lambda _: None), 1)
            self.assertEqual(spool.load(first)["status"], "failed")
            self.assertEqual(spool.load(next_job)["status"], "pending")
            later = self.submit("submitted while stopped")
            spool.run(once=True, log=lambda _: None)
            self.assertEqual(spool.load(later)["status"], "pending")
            launch.assert_called_once()
        stop_switch.resume()
        with patch.object(self.agent, "delegate", return_value=Result("claude", OK, result="done")) as launch:
            self.assertEqual(spool.work_once(log=lambda _: None), 2)
            self.assertEqual(launch.call_count, 2)
        self.assertEqual([spool.load(i)["status"] for i in (first, next_job, later)], ["failed", "done", "done"])

    def test_stopped_direct_delegation_redacts_unchecked_task(self):
        stop_switch.stop()
        with patch.object(self.agent, "delegate") as launch:
            result = control.delegate("unscanned payload", agent="claude", cwd=str(self.project))
        self.assertEqual(result.status, DENIED)
        self.assertIn("resume", result.error)
        launch.assert_not_called()
        self.assertNotIn("unscanned payload", outcomes.log_path().read_text())
        self.assertEqual(self.started_attempts(), [])

    def test_cancel_while_stopped_is_preserved_in_current_daemon_batch(self):
        first = self.submit("first")
        withdrawn = self.submit("withdraw me")
        last = self.submit("last")
        started = []

        def finish(brief, **kwargs):
            started.append(brief)
            if brief == "first":
                stop_switch.stop()
                spool.cancel(withdrawn)
                stop_switch.resume()
            return Result("claude", OK, result="done")

        with patch.object(control, "delegate", side_effect=finish):
            self.assertEqual(spool.work_once(log=lambda _: None), 2)
        self.assertEqual(started, ["first", "last"])
        self.assertEqual([spool.load(i)["status"] for i in (first, withdrawn, last)], ["done", "cancelled", "done"])

    def test_cancel_between_pending_scan_and_claim_is_preserved(self):
        withdrawn = self.submit("withdraw me")
        with patch.object(config, "load", side_effect=lambda: spool.cancel(withdrawn)), \
                patch.object(control, "delegate") as launch:
            self.assertEqual(spool.work_once(log=lambda _: None), 0)
        launch.assert_not_called()
        self.assertEqual(spool.load(withdrawn)["status"], "cancelled")

    def test_stop_during_preparation_blocks_first_attempt(self):
        with patch.object(control.workspace, "snapshot", side_effect=lambda _: stop_switch.stop()), \
                patch.object(self.agent, "delegate") as launch:
            result = control.delegate("test", agent="claude", cwd=str(self.project))
        self.assertEqual(result.status, DENIED)
        launch.assert_not_called()
        self.assertEqual(self.started_attempts(), [])

    def test_stop_during_first_attempt_blocks_retry(self):
        def timeout(*args, **kwargs):
            stop_switch.stop()
            return Result("claude", TIMEOUT, error="timed out")

        with patch.object(self.agent, "delegate", side_effect=timeout) as launch:
            result = control.delegate("test", agent="claude", cwd=str(self.project))
        launch.assert_called_once()
        self.assertEqual(result.status, DENIED)
        self.assertEqual(self.started_attempts(), [1])
        self.assertEqual([(r["attempt"], r["status"]) for r in outcomes.read_all()], [(1, TIMEOUT), (2, DENIED)])

    def test_compare_stops_between_legs(self):
        def finish_first(*args, **kwargs):
            stop_switch.stop()
            return Result("claude", OK, result="done")

        with patch.object(self.agent, "delegate", side_effect=finish_first):
            first, second = control.compare("test", ["claude", "codex"], cwd=str(self.project))
        self.assertEqual(first[0].status, OK)
        self.assertTrue(Path(first[1]).is_dir())
        self.assertEqual((second[0].status, second[1]), (DENIED, ""))
        self.assertEqual(len(list((config.home() / "runs").iterdir())), 1)

    def test_edit_completed_after_stop_stays_pending_review(self):
        self.home.mkdir()
        (self.home / "config.yaml").write_text("approvals:\n  mode: off\n")
        original = self.project / "guide.md"
        original.write_text("draft\n")
        for args in (["init", "-q"], ["add", "guide.md"], ["-c", "user.name=Fixture",
                     "-c", "user.email=fixture@orchestra.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(["git", *args], cwd=self.project, check=True, capture_output=True)

        def edit(brief, *, cwd, **kwargs):
            (Path(cwd) / "guide.md").write_text("updated\n")
            stop_switch.stop()
            return Result("claude", OK, result="edited")

        with patch.object(self.agent, "delegate", side_effect=edit):
            result = control.delegate("update guide.md", agent="claude", cwd=str(self.project),
                                      files=["guide.md"], access=EDIT, meta={"stakes": "low"})
        self.assertEqual((result.status, result.gate), (OK, "pending"))
        self.assertEqual(original.read_text(), "draft\n")
