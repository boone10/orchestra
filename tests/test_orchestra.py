"""Tests run against a stand-in `claude` executable, so they cost nothing."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tests import disable_live_providers

from orchestra import cli, control, outcomes
from orchestra.contract import ERROR, OK, TIMEOUT

FAKE = """#!/bin/sh
# Stand-in for Claude Code's headless mode. Behaviour is chosen by the brief.
for last; do :; done
case "$last" in
  fail)    echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"","total_cost_usd":0.01,"duration_ms":5}'; exit 1 ;;
  ratelimit) echo '{"type":"result","subtype":"success","is_error":true,"result":"This request would exceed the rate limit","total_cost_usd":0,"duration_ms":5}'; exit 1 ;;
  garbage) echo 'this is not json' ;;
  slow)    sleep 5 ;;
  nocost)  echo '{"type":"result","subtype":"success","is_error":false,"result":"done"}' ;;
  *)       echo '{"type":"result","subtype":"success","is_error":false,"result":"hello from fake","total_cost_usd":0.25,"duration_ms":1234}' ;;
esac
"""


class OrchestraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "fake-claude"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self._env = dict(os.environ)
        disable_live_providers(self)
        os.environ["ORCHESTRA_CLAUDE_BIN"] = str(fake)
        os.environ["ORCHESTRA_HOME"] = str(Path(self.tmp.name) / "home")
        self.home = Path(os.environ["ORCHESTRA_HOME"])
        # Everything in these tests is ordinary internal work. Without a declared
        # privacy class the router would (rightly) keep it away from cloud agents.
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects.yaml").write_text("/: {privacy: P1}\n")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_success_is_normalised_and_recorded(self):
        r = control.delegate("summarise this repo")
        self.assertEqual((r.status, r.result, r.cost_usd, r.duration_ms),
                         (OK, "hello from fake", 0.25, 1234))
        (entry,) = outcomes.read_all()
        self.assertEqual(entry["agent"], "claude")
        self.assertEqual(entry["status"], OK)
        self.assertEqual(entry["brief"], "summarise this repo")

    def test_agent_error_is_recorded_not_raised(self):
        r = control.delegate("fail")
        self.assertEqual(r.status, ERROR)
        self.assertEqual(r.exit_code, 1)
        self.assertEqual(outcomes.read_all()[0]["status"], ERROR)

    def test_error_with_success_subtype_reports_the_text(self):
        r = control.delegate("ratelimit")
        self.assertEqual(r.status, ERROR)
        self.assertIn("rate limit", r.error)

    def test_unparseable_reply(self):
        r = control.delegate("garbage")
        self.assertEqual(r.status, ERROR)
        self.assertIn("unparseable", r.error)

    def test_timeout(self):
        r = control.delegate("slow", timeout=1)
        self.assertEqual(r.status, TIMEOUT)
        self.assertEqual(outcomes.read_all()[0]["status"], TIMEOUT)

    def test_missing_binary(self):
        os.environ["ORCHESTRA_CLAUDE_BIN"] = "/nonexistent/claude"
        r = control.delegate("anything", agent="claude")   # named, but not installed
        self.assertEqual(r.status, ERROR)
        self.assertIn("not found", r.error)
        self.assertEqual(len(outcomes.read_all()), 1)

    def test_unknown_cost_stays_unknown(self):
        r = control.delegate("nocost")
        self.assertTrue(r.ok)
        self.assertIsNone(r.cost_usd)
        self.assertIsNotNone(r.duration_ms)

    def test_unknown_agent(self):
        self.assertEqual(cli.main(["delegate", "--agent", "nope", "x"]), 2)
        self.assertEqual(outcomes.read_all(), [])

    def test_summary_and_torn_lines(self):
        for brief in ("a", "b", "fail", "nocost"):
            control.delegate(brief)
        with outcomes.log_path().open("a") as f:
            f.write('{"half a line\n')
        s = outcomes.summarize(outcomes.read_all())
        self.assertEqual((s["runs"], s["failures"], s["cost_unknown"]), (4, 1, 1))
        self.assertAlmostEqual(s["cost_usd"], 0.51)
        self.assertEqual(cli.main(["report"]), 0)

    def test_cli_exit_codes_and_json(self):
        self.assertEqual(cli.main(["delegate", "ok", "--json"]), 0)
        self.assertEqual(cli.main(["delegate", "fail"]), 1)
        json.dumps(outcomes.read_all())


if __name__ == "__main__":
    unittest.main()
