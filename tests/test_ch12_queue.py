"""Keyed submissions, exclusive daemon startup and bounded crash recovery."""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from orchestra import cli, control, spool, stop_switch
from orchestra.contract import OK, Result


class DurableQueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "state"
        self.project = Path(tmp.name) / "project"
        self.project.mkdir()
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)

    def submit(self, brief="read guide", **kwargs):
        return spool.submit(brief, cwd=str(self.project), **kwargs)

    def abandoned(self, **kwargs):
        req = self.submit(**kwargs)
        req.update(status="running", owner="dead-worker", lease_until="2099-01-01",
                   stop_generation=stop_switch.generation())
        return spool._save(req)

    def test_concurrent_keyed_submit_and_completed_replay(self):
        start = Barrier(6)

        def race(_):
            start.wait(timeout=5)
            return self.submit(idempotency_key="nightly", meta={"stakes": "low"})["id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(race, range(6)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(spool.all_requests()), 1)
        with patch.object(control, "delegate", return_value=Result("local", OK, result="done")) as ran:
            spool.run(once=True, log=lambda _: None)
            replay = self.submit(idempotency_key="nightly", meta={"stakes": "low"})
            spool.run(once=True, log=lambda _: None)
        self.assertEqual((replay["id"], replay["status"], ran.call_count), (ids[0], "done", 1))
        with self.assertRaisesRegex(spool.SpoolError, "different request"):
            self.submit(idempotency_key="nightly", meta={"stakes": "high"})
        with self.assertRaisesRegex(spool.SpoolError, "different request"):
            self.submit(idempotency_key="nightly", meta={"stakes": "low"}, schedule="job")
        self.project.rmdir()
        self.assertEqual(self.submit(idempotency_key="nightly", meta={"stakes": "low"})["status"], "done")

    def test_unkeyed_requests_and_legacy_json_still_work(self):
        first = self.submit()
        second = self.submit()
        self.assertNotEqual(first["id"], second["id"])
        del first["replay_safe"]
        first.pop("agent")
        first.update(files=None, meta=None)
        spool._save(first)
        with patch.object(control, "delegate", return_value=Result("local", OK, result="done")) as ran:
            self.assertEqual(spool.work_once(log=lambda _: None), 2)
        self.assertEqual(ran.call_count, 2)

    def test_stopped_home_accepts_manual_submission_and_holds_new_scheduled_work(self):
        stop_switch.stop()
        self.assertEqual(self.submit()["status"], "pending")
        with self.assertRaises(stop_switch.Stopped):
            self.submit(idempotency_key="schedule.day", schedule="day")
        self.assertEqual(len(spool.all_requests()), 1)
        stop_switch.resume()
        req = self.submit(idempotency_key="schedule.day", schedule="day")
        self.assertEqual((req["source"], req["schedule"]), ("schedule", "day"))

    def test_keyed_record_corruption_cannot_duplicate_or_redirect_work(self):
        req = self.submit(idempotency_key="job")
        path = self.home / "requests" / (req["id"] + ".json")
        req["id"] = "../other"
        path.write_text(json.dumps(req))
        self.assertEqual(spool.all_requests(), [])
        with self.assertRaisesRegex(spool.SpoolError, "cannot verify"):
            self.submit(idempotency_key="job")

    def test_damaged_key_metadata_keeps_reserved_slot(self):
        req = self.submit(idempotency_key="slot")
        path = self.home / "requests" / (req["id"] + ".json")
        for changed in ({"idempotency_key": None}, {"idempotency_key": "bad"}, {"payload_sha256": []}):
            with self.subTest(changed=changed):
                path.write_text(json.dumps(dict(req, **changed)))
                with self.assertRaisesRegex(spool.SpoolError, "cannot verify"):
                    self.submit(idempotency_key="slot")
                self.assertEqual(spool.status_counts(), {"unreadable": 1})

    def test_malformed_requests_leave_healthy_jobs_visible_and_runnable(self):
        healthy = self.submit()
        folder = self.home / "requests"
        bad = [[], {"id": "bad1"}, {"id": "bad2", "status": "pending"},
               dict(healthy, id="bad3", access={}), dict(healthy, id="bad4", status=[]),
               dict(healthy, id="bad5", brief=None), dict(healthy, id="bad6", status="needs_human", result=["wrong"])]
        for i, row in enumerate(bad):
            (folder / f"bad{i}.json").write_text(json.dumps(row))
        with patch.object(control, "delegate", return_value=Result("local", OK, result="done")) as ran:
            spool.run(once=True, log=lambda _: None)
            ran.assert_called_once()
        for args in (["requests", "--all"], ["state"]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(args), 0)
            self.assertIn("done", out.getvalue())
        self.assertEqual(spool.status_counts(), {"done": 1, "unreadable": len(bad)})

    def test_recovery_retries_only_an_opted_in_read_once(self):
        plain = self.abandoned()
        retry = self.abandoned(replay_safe=True, idempotency_key="read-again")
        with self.assertRaisesRegex(spool.SpoolError, "reads"):
            self.submit(access="edit", replay_safe=True)
        self.assertEqual(spool.recover(log=lambda _: None), [plain["id"]])
        waiting = spool.load(retry["id"])
        self.assertEqual((waiting["status"], waiting["crash_retries"], waiting["owner"]), ("pending", 1, None))
        self.assertIsNone(waiting["lease_until"])
        waiting["status"] = "running"
        spool._save(waiting)
        self.assertEqual(spool.recover(log=lambda _: None), [retry["id"]])
        self.assertEqual(spool.load(retry["id"])["status"], "interrupted")

    def test_closed_stop_and_resume_prevents_crash_replay(self):
        req = self.abandoned(replay_safe=True)
        stop_switch.stop()
        stop_switch.resume()
        self.assertEqual(spool.recover(log=lambda _: None), [req["id"]])

    def test_native_registration_prevents_replay_until_operator_reviews_job(self):
        req = self.abandoned(replay_safe=True)
        active = self.home / "active-skills"
        active.mkdir()
        (active / "orphan.socket").touch()
        self.assertEqual(spool.recover(log=lambda _: None), [req["id"]])

    def test_daemon_lifetime_flock_excludes_other_process_after_pid_file_lost(self):
        with spool._daemon_lock():
            (self.home / "daemon.pid").unlink()
            other = subprocess.run([sys.executable, "-m", "orchestra", "daemon", "--once"],
                                   capture_output=True, text=True, timeout=10)
            self.assertNotEqual(other.returncode, 0)
            self.assertIn("already running", other.stderr)
        (self.home / "daemon.pid").write_text(str(os.getpid()))
        spool.run(once=True, log=lambda _: None)
        self.assertFalse((self.home / "daemon.pid").exists())
        self.assertTrue((self.home / "daemon.lock").exists())

    def test_graceful_shutdown_stops_new_claims_after_signal(self):
        first, second = self.submit(), self.submit()
        def finish(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return Result("local", OK, result="done")

        prior = signal.getsignal(signal.SIGTERM)
        with patch.object(control, "delegate", side_effect=finish):
            spool.run(log=lambda _: None)
        self.assertEqual(signal.getsignal(signal.SIGTERM), prior)
        self.assertEqual(spool.load(first["id"])["status"], "done")
        self.assertEqual(spool.load(second["id"])["status"], "pending")

    def test_invalid_scheduler_config_keeps_daemon_available(self):
        self.home.mkdir()
        (self.home / "config.yaml").write_text("scheduler: {enabled: typo}\n")
        messages = []
        spool.run(once=True, log=messages.append)
        self.assertTrue(any("scheduler paused" in message for message in messages))
