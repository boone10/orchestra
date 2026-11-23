"""Parallel claims, queue admission, leases, and worker completion fencing."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from orchestra import backends, control, outcomes, skills, spool, stop_switch, tasks
from orchestra.contract import OK, Result
from tests import disable_live_providers


class QueueWorkersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.project = root / "project"
        self.project.mkdir()
        self.config = self.home / "config.yaml"
        self.config.write_text("daemon:\n  workers: 2\n  poll_seconds: 0.2\n")
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        disable_live_providers(self)

    def submit(self, brief, **options):
        return spool.submit(brief, cwd=str(self.project), **options)["id"]

    def test_native_reads_overlap_at_capacity_and_stop_preserves_next(self):
        skills.install_examples()
        with patch.object(backends, "run_skill", return_value=subprocess.CompletedProcess([], 0, "ok", "")):
            skills.trust("check-doc-links")
        ids = [self.submit(text, skill="check-doc-links") for text in ("first", "second", "waiting")]
        both_entered = threading.Barrier(3)
        release = threading.Event()

        def read(cmd, *, task, **kwargs):
            both_entered.wait(5)
            if not release.wait(5):
                raise RuntimeError("fixture timed out")
            return subprocess.CompletedProcess(cmd, 0, task, "")

        with patch.object(backends, "run_skill", side_effect=read), \
                patch.object(spool, "_HEARTBEAT_SECONDS", 0.02), ThreadPoolExecutor(1) as pool:
            worker = pool.submit(spool.work_once, lambda _: None)
            try:
                both_entered.wait(5)
                due = time.monotonic() + 4
                while time.monotonic() < due:
                    snapshot = [spool.load(ident) for ident in ids]
                    if all(req["heartbeat_at"] != req["claimed_at"] for req in snapshot[:2]):
                        break
                    time.sleep(0.01)
                self.assertEqual([req["status"] for req in snapshot], ["running", "running", "pending"])
                self.assertNotEqual(snapshot[0]["owner"], snapshot[1]["owner"])
                for req in snapshot[:2]:
                    self.assertGreater(req["heartbeat_at"], req["claimed_at"])
                    self.assertGreater(datetime.fromisoformat(req["lease_until"]),
                                       datetime.fromisoformat(req["heartbeat_at"]))
                stop_switch.stop()
            finally:
                release.set()
            self.assertEqual(worker.result(5), 2)
        self.assertEqual(spool.load(ids[2])["status"], "pending")
        self.assertEqual({r["request_id"] for r in outcomes.read_all()}, set(ids[:2]))
        self.assertEqual((self.home / "outcomes.jsonl").read_bytes(),
                         (self.home / "outcomes.jsonl.bak").read_bytes())
        audit = [json.loads(line) for line in (self.home / "logs/audit.jsonl").read_text().splitlines()]
        for ident in ids[:2]:
            events = [e["event"] for e in audit if e.get("request_id") == ident]
            self.assertLess(events.index("request.claimed"), events.index("request.finished"))

    def test_poll_admits_arrival_before_long_request_finishes(self):
        self.submit("busy")
        arrival_finished = threading.Event()
        enqueued = []

        def tick():
            if not enqueued:
                enqueued.append(self.submit("new arrival"))

        def run(brief, **kwargs):
            if brief == "busy":
                self.assertTrue(arrival_finished.wait(4))
            else:
                arrival_finished.set()
            return Result("fixture", OK, result=brief)

        with patch.object(control, "delegate", side_effect=run):
            self.assertEqual(spool.work_once(lambda _: None, on_poll=tick), 2)
        self.assertEqual([r["status"] for r in spool.all_requests()], ["done", "done"])

    def test_edit_has_exclusive_slot_and_preserves_fifo(self):
        first, edit, last = [self.submit(name, access=access) for name, access in
                             (("before", "read"), ("edit", "edit"), ("after", "read"))]
        observed = []

        def run(brief, **kwargs):
            observed.append((brief, [spool.load(i)["status"] for i in (first, edit, last)]))
            return Result("fixture", OK, result=brief)

        with patch.object(control, "delegate", side_effect=run):
            self.assertEqual(spool.work_once(lambda _: None), 3)
        self.assertEqual(observed, [("before", ["running", "pending", "pending"]),
                                    ("edit", ["done", "running", "pending"]),
                                    ("after", ["done", "done", "running"])])

    def test_stale_worker_cannot_finish_or_renew_a_new_claim(self):
        ident = self.submit("obsolete")

        def superseded(brief, **kwargs):
            req = spool.load(ident)
            old_claim = req.copy()
            with stop_switch.dispatch_change():
                req.update(owner="replacement", heartbeat_at="unchanged")
                spool._save(req)
            spool._heartbeat([old_claim])
            return Result("fixture", OK, result=brief)

        with patch.object(control, "delegate", side_effect=superseded):
            self.assertEqual(spool.work_once(lambda _: None), 1)
        req = spool.load(ident)
        self.assertEqual((req["status"], req["owner"], req["heartbeat_at"]),
                         ("running", "replacement", "unchanged"))

    def test_invalid_config_during_batch_keeps_next_request_pending(self):
        first = self.submit("edit", access="edit")
        waiting = self.submit("wait")

        def change_config(brief, **kwargs):
            self.config.write_text("approvals:\n  mode: invalid\n")
            return Result("fixture", OK, result=brief)

        with patch.object(control, "delegate", side_effect=change_config):
            self.assertEqual(spool.work_once(lambda _: None), 1)
        self.assertEqual((spool.load(first)["status"], spool.load(waiting)["status"]),
                         ("done", "pending"))

    def test_review_closed_before_worker_returns(self):
        ident = self.submit("edit", access="edit")
        task_id = "t-fixture"

        def reviewed(brief, **kwargs):
            tasks.save_task(task_id, {"task_id": task_id, "status": tasks.DISCARDED,
                                      "request_id": ident})
            spool.close_review(ident, "done")
            return Result("fixture", OK, result=brief, task_id=task_id, gate="pending")

        with patch.object(control, "delegate", side_effect=reviewed):
            self.assertEqual(spool.work_once(lambda _: None), 1)
        self.assertEqual(spool.load(ident)["status"], "done")
