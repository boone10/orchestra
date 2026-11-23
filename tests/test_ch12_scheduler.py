"""Recurring jobs on a simulated clock, using a real disposable request spool."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestra import backends, scheduler, spool, stop_switch


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "state"
        self.home.mkdir()
        self.project = self.root / "site"
        self.project.mkdir()
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.messages = []

    def enable(self):
        (self.home / "config.yaml").write_text("scheduler:\n  enabled: true\n")

    def add(self, name="links", **kwargs):
        return scheduler.add(name, "check the docs", cwd=self.project, every=60, now=self.start, **kwargs)

    def tick(self, seconds):
        return scheduler.enqueue_due(self.messages.append, now=self.start + timedelta(seconds=seconds))

    def test_disabled_stopped_and_paused_clocks_coalesce_on_resume(self):
        job = self.add(skill="check-doc-links", files=["guide.md"], meta={"privacy": "P3"})
        self.assertEqual(job["next_due"], (self.start + timedelta(seconds=60)).isoformat())
        self.assertEqual(self.tick(3600), 0)
        self.enable()
        stop_switch.stop()
        self.assertEqual(self.tick(3601), 0)
        stop_switch.resume()
        scheduler.pause("links")
        self.assertEqual(self.tick(3602), 0)
        self.assertEqual(scheduler.list_jobs()[0]["next_due"], job["next_due"])
        scheduler.resume("links")
        self.assertEqual(self.tick(3603), 1)
        self.assertEqual(self.tick(3604), 0)
        current = scheduler.list_jobs()[0]
        self.assertEqual(current["next_due"], (self.start + timedelta(seconds=3660)).isoformat())
        req = spool.load(current["last_request_id"])
        self.assertEqual((req["status"], req["source"], req["schedule"]), ("pending", "schedule", "links"))
        self.assertEqual(req["files"], [str((self.project / "guide.md").resolve())])
        self.assertEqual(req["meta"]["privacy"], "P3")

    def test_recovery_after_enqueue_reuses_persisted_slot_even_after_more_intervals(self):
        self.enable()
        job = self.add()
        with patch.object(scheduler, "_save", side_effect=OSError("fixture write failed")):
            self.assertEqual(self.tick(60), 0)
        req, = spool.all_requests()
        req["status"] = "done"
        spool._save(req)
        self.assertEqual(scheduler.list_jobs()[0]["next_due"], job["next_due"])
        self.assertEqual(self.tick(400), 1)
        self.assertEqual(len(spool.all_requests()), 1)
        self.assertEqual(scheduler.list_jobs()[0]["last_request_id"], req["id"])
        self.assertEqual(self.tick(420), 1)
        self.assertEqual(len(spool.all_requests()), 2)

    def test_two_producer_processes_publish_one_occurrence(self):
        self.enable()
        self.add()
        script = ("from datetime import datetime; from orchestra import scheduler; "
                  "print(scheduler.enqueue_due(lambda _: None, now=datetime.fromisoformat("
                  "'2026-01-01T12:01:00+00:00')))")
        jobs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(2)]
        totals = []
        for proc in jobs:
            with proc:
                out, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, err)
                totals.append(int(out))
        self.assertEqual(sorted(totals), [0, 1])
        self.assertEqual(len(spool.all_requests()), 1)

    def test_stop_racing_with_submission_leaves_slot_due(self):
        self.enable()
        job = self.add()
        submit = spool.submit

        def stop_before_submit(**kwargs):
            stop_switch.stop()
            return submit(**kwargs)

        with patch.object(spool, "submit", side_effect=stop_before_submit):
            self.assertEqual(self.tick(60), 0)
        self.assertEqual(spool.all_requests(), [])
        self.assertEqual(scheduler.list_jobs()[0]["next_due"], job["next_due"])
        stop_switch.resume()
        self.assertEqual(self.tick(600), 1)

    def test_each_open_or_escalated_status_suppresses_overlap(self):
        self.enable()
        self.add()
        self.assertEqual(self.tick(60), 1)
        req, = spool.all_requests()
        for i, status in enumerate(("pending", "running", "needs_review", "needs_human"), start=2):
            req["status"] = status
            spool._save(req)
            self.assertEqual(self.tick(60 * i), 0)
        self.assertEqual(len(spool.all_requests()), 1)
        req["status"] = "done"
        spool._save(req)
        self.assertEqual(self.tick(359), 0)
        self.assertEqual(self.tick(360), 1)

    def test_interrupted_native_predecessor_holds_future_slots_until_cleanup(self):
        self.enable()
        self.add(skill="check-doc-links", replay_safe=True)
        self.assertEqual(self.tick(60), 1)
        req, = spool.all_requests()
        req.update(status="running", stop_generation=stop_switch.generation())
        spool._save(req)
        active = self.home / "active-skills"
        active.mkdir()
        orphan = active / "old.socket"
        orphan.write_text("fixture")
        spool.recover(log=self.messages.append)
        self.assertEqual(spool.load(req["id"])["status"], "interrupted")
        self.assertEqual(self.tick(120), 0)
        with patch.object(backends, "stop_skills", side_effect=lambda: orphan.unlink(missing_ok=True) or 0):
            stop_switch.stop()
            stop_switch.resume()
        # Other current-generation work may legitimately run now.
        (active / "unrelated.socket").touch()
        self.assertEqual(self.tick(180), 1)
        self.assertEqual(len(spool.all_requests()), 2)

    def test_damaged_job_does_not_block_healthy_schedule(self):
        self.enable()
        job = self.add(name="zzz")
        (self.home / "cron/broken.json").write_text("{")
        (self.home / "cron/list.json").write_text("[]")
        job["name"] = "bad-meta"
        job["request"]["meta"] = ["wrong shape"]
        (self.home / "cron/bad-meta.json").write_text(json.dumps(job))
        self.assertEqual(self.tick(60), 1)
        for name in ("broken", "list", "bad-meta"):
            self.assertTrue(any(f"{name}.json" in message for message in self.messages))
            scheduler.remove(name)
        self.assertEqual([job["name"] for job in scheduler.list_jobs()], ["zzz"])

    def test_recreate_after_remove_preserves_queue_and_starts_new_generation(self):
        self.enable()
        old = self.add()
        self.assertEqual(self.tick(60), 1)
        original, = spool.all_requests()
        self.assertEqual(scheduler.remove("links")["id"], old["id"])
        self.assertEqual(scheduler.list_jobs(), [])
        new = self.add()
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(self.tick(60), 0)
        self.assertEqual(spool.load(original["id"])["status"], "pending")
        original["status"] = "needs_review"
        spool._save(original)
        self.assertEqual(self.tick(120), 0)
        spool.close_review(original["id"], "done")
        self.assertEqual(self.tick(180), 1)
        self.assertEqual(len(spool.all_requests()), 2)
        # Reusing a slot after the previous job has finished is legitimate
        # for a new generation, even with exactly the same name and payload.
        _, latest = spool.all_requests()
        latest["status"] = "done"
        spool._save(latest)
        scheduler.remove("links")
        self.add()
        self.assertEqual(self.tick(60), 1)
        self.assertEqual(len(spool.all_requests()), 3)

    def test_schedule_validation_precedes_persistence(self):
        for opts in ({"every": 0}, {"daily": "24:00"}, {"daily": "12:00", "timezone": "No/Such_Zone"},
                     {"every": 10, "daily": "02:00"}):
            with self.subTest(opts=opts), self.assertRaises(scheduler.SchedulerError):
                scheduler.add("job", "read", cwd=self.project, **opts)
        with self.assertRaises(scheduler.SchedulerError):
            self.add("../escape")
        with patch.dict(os.environ, CH12_TEST_SECRET="fixture-secret-in-env"), self.assertRaises(spool.SpoolError):
            scheduler.add("job", "read fixture-secret-in-env", every=1, cwd=self.project)
        self.assertFalse((self.home / "cron").exists())

    def test_daily_spring_gap_and_fall_fold_fire_at_most_once(self):
        zone = "America/New_York"
        job = scheduler.add("spring", "read", daily="02:30", timezone=zone, cwd=self.project,
                            now=datetime(2026, 3, 7, 18, tzinfo=timezone.utc))
        self.assertEqual(job["next_due"], "2026-03-09T06:30:00+00:00")
        scheduler.remove("spring")
        job = scheduler.add("fall", "read", daily="01:30", timezone=zone, cwd=self.project,
                            now=datetime(2026, 10, 31, 18, tzinfo=timezone.utc))
        self.assertEqual(job["next_due"], "2026-11-01T05:30:00+00:00")
        self.enable()
        self.assertEqual(scheduler.enqueue_due(self.messages.append, now=datetime.fromisoformat(job["next_due"])), 1)
        self.assertEqual(scheduler.list_jobs()[0]["next_due"], "2026-11-02T06:30:00+00:00")
