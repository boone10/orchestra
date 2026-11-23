"""Native skill containment at the Docker client boundary, without a daemon."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import backends, config, control, router
from orchestra.contract import ESCALATED


class DockerSkillTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "state"
        self.skill = self.home / "skills" / "example"
        self.skill.mkdir(parents=True)
        (self.skill / "check.py").write_text("print('ok')")
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.subdir = self.repo / "docs"
        self.subdir.mkdir()
        (self.subdir / "guide.md").write_text("Example")
        env = patch.dict(os.environ, ORCHESTRA_HOME=str(self.home), PRIVATE_TOKEN="host-only",
                         DOCKER_HOST="tcp://example.invalid:1234")
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("shutil.which", "/usr/bin/docker"),
                            ("_docker_socket", Path("/var/run/docker.sock"))):
            stub = patch("orchestra.backends." + name, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def run_job(self, **kwargs):
        return backends.run_skill(["python3", "check.py"], skill_dir=self.skill,
                                  project=self.repo, cwd=self.subdir, timeout=5, **kwargs)

    def test_project_run_uses_only_staged_readonly_inputs(self):
        (self.repo / ".env").write_text("private")
        (self.repo / "link").symlink_to(self.repo / ".env")
        os.mkfifo(self.repo / "pipe")

        def inspect(command, env, timeout):
            for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--user=65534:65534",
                         "--memory=256m", "--cpus=1", "--pids-limit=64"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--host") + 1], "unix:///var/run/docker.sock")
            self.assertEqual(command[command.index("--workdir") + 1], "/workspace/docs")
            self.assertEqual(command[-2:], ["python:3.12-slim", "/skill/check.py"])
            mounts = [command[i + 1] for i, v in enumerate(command) if v == "--mount"]
            self.assertEqual(len(mounts), 2)
            self.assertTrue(all(m.endswith(",readonly") for m in mounts))
            self.assertNotIn(str(self.repo), " ".join(mounts))
            workspace = next((self.home / "docker-stage").glob("skill-*/workspace"))
            self.assertEqual([p.name for p in workspace.iterdir()], ["docs"])
            self.assertEqual((workspace / "docs/guide.md").read_text(), "Example")
            self.assertEqual(env, {"PATH": os.defpath, "ORCHESTRA_TASK": "read docs",
                                   "ORCHESTRA_FILES": "docs/guide.md", "ORCHESTRA_PROJECT": "/workspace"})
            return subprocess.CompletedProcess(command, 0, "ok", "")

        with patch.object(backends, "_run_bounded", side_effect=inspect):
            self.assertEqual(self.run_job(task="read docs", files=["docs/guide.md"]).stdout, "ok")
        self.assertEqual(list((self.home / "docker-stage").iterdir()), [])

    def test_changed_skill_never_launches(self):
        with patch.object(backends, "_run_bounded") as launch:
            with self.assertRaisesRegex(OSError, "changed since review"):
                self.run_job(expected_hash="old-hash")
            launch.assert_not_called()

    def test_timeout_removes_only_this_container(self):
        error = subprocess.TimeoutExpired("fixture", 5)
        with patch.object(backends, "_run_bounded", side_effect=error) as launch, \
                patch.object(backends.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_job()
            command = launch.call_args.args[0]
            name = command[command.index("--name") + 1]
            self.assertEqual(cleanup.call_args.args[0], command[:command.index("run")] + ["rm", "-f", name])

    def test_cli_output_has_a_host_side_limit(self):
        with self.assertRaisesRegex(OSError, "output limit"):
            backends._run_bounded([sys.executable, "-c", "print('x' * 1100000)"], {}, 5)

    def test_queue_supervision_is_checked_again_before_dispatch(self):
        options = {"privacy": "P0", "stakes": "low", "autonomy": "supervised"}
        axes = router.classify("read docs", options, str(self.repo), [], "daemon", config.load())
        self.assertEqual(axes["autonomy"]["value"], "unattended")
        bad_decision = {"executor": "codex", "outcome": "delegate", "reasons": [],
                        "not_eligible": [], "axes": axes, "task_class": "general"}
        with patch.object(router, "decide", return_value=bad_decision), \
                patch.object(control.adapters.get("codex"), "delegate") as launch:
            result = control.delegate("read docs", cwd=str(self.repo), meta=options,
                                      source="daemon", use_skills=False)
        self.assertEqual(result.status, ESCALATED)
        self.assertIn("supervised", result.error)
        launch.assert_not_called()
