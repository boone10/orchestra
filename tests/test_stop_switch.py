import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestra import backends, cli, stop_switch


class StopSwitchTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = patch.dict(os.environ, ORCHESTRA_HOME=home.name)
        env.start()
        self.addCleanup(env.stop)

    def command(self, name):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([name])
        return code, out.getvalue(), err.getvalue()

    def test_failed_cleanup_holds_queue_until_resume_can_confirm_removal(self):
        with patch.object(backends, "stop_skills", side_effect=OSError("engine offline")):
            for name in ("stop", "resume"):
                code, _, err = self.command(name)
                self.assertEqual(code, 1)
                self.assertIn("engine offline", err)
                self.assertTrue(stop_switch.is_stopped())
                with self.assertRaises(stop_switch.Stopped), stop_switch.admission():
                    self.fail("a failed cleanup admitted new work")
        with patch.object(backends, "stop_skills", return_value=1) as cleanup:
            self.assertEqual(self.command("resume")[0], 0)
            cleanup.assert_called_once()
        self.assertFalse(stop_switch.is_stopped())

    def test_repeated_commands_and_status(self):
        with patch.object(backends, "stop_skills", return_value=0) as cleanup:
            self.assertIn("already enabled", self.command("resume")[1])
            cleanup.assert_not_called()
            self.assertEqual(self.command("stop")[0], 0)
            self.assertEqual(self.command("stop")[0], 0)
            self.assertIn("dispatch       stopped", self.command("state")[1])
            self.assertEqual(self.command("resume")[0], 0)
            self.assertIn("dispatch       enabled", self.command("state")[1])
            self.assertEqual(cleanup.call_count, 3)

    def test_stop_removes_orphan_snapshot_but_keeps_live_and_other_home_snapshots(self):
        with backends._skill_stage(Path("/var/run/docker.sock")) as live:
            prefix = "skill-" + backends._home_id() + "-"
            abandoned = live.parent / (prefix + "abandoned")
            abandoned.mkdir()
            (abandoned / "project-input").write_text("fixture")
            other = live.parent / ("skill-" + "0" * 64 + "-example")
            other.mkdir()
            stop_switch.stop()
            self.assertFalse(abandoned.exists())
            self.assertTrue(live.is_dir())
            self.assertTrue(other.is_dir())
        self.assertFalse(live.exists())
