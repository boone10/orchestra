"""Docker stop behavior when a container is registered, created or running."""

import fcntl
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from orchestra import backends, stop_switch


class SkillStopTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "state"
        self.skill = root / "skill"
        self.skill.mkdir()
        self.socket = Path("/var/run/docker.sock")
        for stub in (patch.dict(os.environ, ORCHESTRA_HOME=str(self.home)),
                     patch.object(backends.shutil, "which", return_value="/usr/bin/docker"),
                     patch.object(backends, "_docker_socket", return_value=self.socket)):
            stub.start()
            self.addCleanup(stub.stop)

    def launch(self):
        return backends.run_skill(["python3", "-V"], skill_dir=self.skill, cwd=self.skill,
                                  project=None, timeout=5)

    def register(self, name, socket):
        marker = self.home / "active-skills" / (name + ".socket")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(socket))
        return marker

    def test_missing_engine_only_matters_for_registered_work(self):
        with patch.object(backends.shutil, "which", return_value=None):
            self.assertEqual(stop_switch.stop(), 0)
            marker = self.register("old-run", self.socket)
            with self.assertRaisesRegex(OSError, "registered"):
                stop_switch.stop()
            self.assertTrue(marker.exists())
            self.assertTrue(stop_switch.is_stopped())
            with self.assertRaises(stop_switch.Stopped):
                self.launch()

    def test_uncertain_create_looks_up_its_label_and_exact_name(self):
        cid = "e" * 64
        name = []

        def lose_response(command, env, timeout, **kwargs):
            name.append(command[command.index("--name") + 1])
            raise subprocess.TimeoutExpired(command, timeout)

        def cleanup(command, **kwargs):
            if "ps" in command:
                self.assertIn("label=" + backends._skill_label(), command)
                self.assertIn(f"name=^/{re.escape(name[0])}$", command)
                return subprocess.CompletedProcess(command, 0, cid, "")
            self.assertEqual(command[-3:], ["rm", "-f", cid])
            return subprocess.CompletedProcess(command, 0, cid, "")

        with patch.object(backends, "_run_bounded", side_effect=lose_response), \
                patch.object(backends.subprocess, "run", side_effect=cleanup):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.launch()
        self.assertEqual(list((self.home / "active-skills").iterdir()), [])

    def test_cleanup_follows_registered_local_engines_and_labels(self):
        sockets = [self.socket, Path.home() / ".colima/orchestra/docker.sock"]
        ids = ["a" * 64, "b" * 64]
        markers = [self.register("run-" + str(i), sock) for i, sock in enumerate(sockets)]
        removed = []

        def engine(command, **kwargs):
            sock = command[command.index("--host") + 1]
            cid = ids[[f"unix://{s}" for s in sockets].index(sock)]
            if "ps" in command:
                self.assertIn("-aq", command)
                self.assertIn("label=" + backends._skill_label(), command)
                return subprocess.CompletedProcess(command, 0, cid, "")
            self.assertEqual(command[-3:], ["rm", "-f", cid])
            removed.append(cid)
            return subprocess.CompletedProcess(command, 0, cid, "")

        with patch.object(backends.subprocess, "run", side_effect=engine):
            self.assertEqual(stop_switch.stop(), 2)
        self.assertCountEqual(removed, ids)
        self.assertFalse(any(marker.exists() for marker in markers))

    def test_stop_sweeps_and_keeps_orphaned_create_registered_until_it_exits(self):
        root = self.skill.parent
        fake = root / "docker"
        fake.write_text("#!" + sys.executable + """
import sys, time
from pathlib import Path
root = Path(__file__).parent
present = root / 'container'
cid = 'a' * 64
if 'create' in sys.argv:
    (root / 'creating').touch()
    until = time.monotonic() + 10
    while not (root / 'release').exists():
        if time.monotonic() > until:
            sys.exit(1)
        time.sleep(0.01)
    present.write_text(cid)
elif 'ps' in sys.argv:
    print(*(p.read_text() for p in root.glob('container*')))
elif 'rm' in sys.argv:
    path, = (p for p in root.glob('container*') if p.read_text() == sys.argv[-1])
    path.unlink()
else:
    sys.exit(1)
""")
        fake.chmod(0o700)
        script = """
import sys
from pathlib import Path
from orchestra import backends
fake, skill = sys.argv[1:]
backends.shutil.which = lambda name: fake
backends._docker_socket = lambda: Path('/var/run/docker.sock')
backends.run_skill(['python3', '-V'], skill_dir=Path(skill), cwd=Path(skill), project=None, timeout=8)
"""
        other = root / "container-other"
        other.write_text("b" * 64)
        with patch.object(backends.shutil, "which", return_value=str(fake)), \
                subprocess.Popen([sys.executable, "-c", script, str(fake), str(self.skill)]) as runner:
            try:
                until = time.monotonic() + 5
                while not (root / "creating").exists():
                    self.assertIsNone(runner.poll())
                    self.assertLess(time.monotonic(), until, "Docker create did not start")
                    time.sleep(0.01)
                runner.kill()
                runner.wait(timeout=5)
                with self.assertRaisesRegex(OSError, "removed 1 container.*create is still in progress"):
                    stop_switch.stop()
                self.assertTrue(stop_switch.is_stopped())
                self.assertFalse(other.exists())
                marker, = (self.home / "active-skills").glob("*.socket")
                with self.assertRaisesRegex(OSError, "retry orchestra stop"):
                    stop_switch.resume()
            finally:
                (root / "release").touch()
                if runner.poll() is None:
                    runner.kill()
            # The child closes the lock only after its create finishes.
            with marker.open("rb") as reg:
                fcntl.flock(reg, fcntl.LOCK_SH)
            self.assertEqual(stop_switch.stop(), 1)
            self.assertFalse(marker.exists())
            self.assertTrue(stop_switch.resume())
        self.assertFalse((root / "container").exists())

    def test_unanswered_create_preserves_engine_for_later_cleanup(self):
        cid = "b" * 64
        timeout = subprocess.TimeoutExpired("create", 1)
        with patch.object(backends, "_run_bounded", side_effect=timeout), \
                patch.object(backends, "_skill_containers", return_value=[]):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.launch()
        markers = list((self.home / "active-skills").glob("*.socket"))
        self.assertEqual(len(markers), 1)
        with patch.object(backends, "_skill_containers", return_value=[cid]), \
                patch.object(backends, "_remove_container", return_value=True) as remove:
            self.assertEqual(stop_switch.stop(), 1)
        self.assertEqual(remove.call_args.args[1], cid)
        self.assertFalse(markers[0].exists())

    def test_stop_and_resume_during_attach_timeout_prevent_retry(self):
        cid = "c" * 64

        def run(command, env, timeout, **kwargs):
            if "create" in command:
                return subprocess.CompletedProcess(command, 0, cid, "")
            self.assertEqual(stop_switch.stop(), 1)
            self.assertTrue(stop_switch.resume())
            raise subprocess.TimeoutExpired(command, timeout)

        with patch.object(backends, "_run_bounded", side_effect=run), \
                patch.object(backends, "_skill_containers", return_value=[cid]), \
                patch.object(backends, "_remove_container", return_value=True):
            with self.assertRaises(stop_switch.Stopped):
                self.launch()

    def test_cleanup_timeout_is_nonretryable_and_keeps_registration(self):
        cid = "d" * 64
        created = subprocess.CompletedProcess([], 0, cid, "")
        timeout = subprocess.TimeoutExpired("Docker", 1)
        with patch.object(backends, "_run_bounded", side_effect=[created, timeout]), \
                patch.object(backends, "_remove_container", side_effect=timeout):
            with self.assertRaisesRegex(OSError, "timed out confirming"):
                self.launch()
        self.assertEqual(len(list((self.home / "active-skills").glob("*.socket"))), 1)

    def test_create_rejected_by_engine_clears_registration(self):
        refusal = subprocess.CompletedProcess([], 1, "", "Error response from daemon: No such image")
        with patch.object(backends, "_run_bounded", return_value=refusal), \
                patch.object(backends, "_skill_containers", return_value=[]):
            self.assertIs(self.launch(), refusal)
        self.assertEqual(list((self.home / "active-skills").iterdir()), [])

    def test_delayed_start_cannot_recreate_container_after_stop_and_resume(self):
        # Stall just after Docker create returns. Stop can discover the ID
        # before start, and a rapid resume cannot resurrect that ID.
        waiting, release = Event(), Event()
        cid = "a" * 64
        present = set()

        def run(command, env, timeout, **kwargs):
            if "create" in command:
                self.assertIn(backends._skill_label(), command)
                present.add(cid)
                return subprocess.CompletedProcess(command, 0, cid, "")
            self.assertEqual(command[-3:], ["start", "--attach", cid])
            waiting.set()
            self.assertTrue(release.wait(5))
            self.assertNotIn(cid, present)
            return subprocess.CompletedProcess(command, 1, "", "No such container")

        def cleanup(command, **kwargs):
            if "ps" in command:
                return subprocess.CompletedProcess(command, 0, "\n".join(present), "")
            self.assertEqual(command[-3:], ["rm", "-f", cid])
            present.remove(cid)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(backends, "_run_bounded", side_effect=run), \
                patch.object(backends.subprocess, "run", side_effect=cleanup), \
                ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(self.launch)
            try:
                self.assertTrue(waiting.wait(5))
                self.assertEqual(stop_switch.stop(), 1)
                self.assertTrue(stop_switch.resume())
            finally:
                release.set()
            with self.assertRaises(stop_switch.Stopped):
                job.result(timeout=5)

    def test_failed_removal_leaves_registration_and_attempts_remaining_ids(self):
        marker = self.register("unfinished", self.socket)
        first, second = "c" * 64, "d" * 64
        remaining = [first, second]
        fail = True

        def engine(command, **kwargs):
            if "ps" in command:
                return subprocess.CompletedProcess(command, 0, "\n".join(remaining), "")
            if fail and command[-1] == first:
                return subprocess.CompletedProcess(command, 1, "", "engine busy")
            remaining.remove(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(backends.subprocess, "run", side_effect=engine):
            with self.assertRaisesRegex(OSError, "removed 1 container"):
                stop_switch.stop()
            self.assertEqual(remaining, [first])
            self.assertTrue(marker.exists())
            self.assertTrue(stop_switch.is_stopped())
            fail = False
            self.assertEqual(stop_switch.stop(), 1)
        self.assertFalse(marker.exists())
