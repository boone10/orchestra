"""The terminal backend: where dispatched commands actually run (book 4.5).

Supervised cloud CLIs use the local terminal. Native skills and manifest
self-tests run in offline Docker containers through run_skill().

Under every backend sits the hardline floor (book 4.6): patterns refused no
matter what the configuration says. It is a backstop against catastrophe, not
the safety strategy. The approval mode and (later) the sandbox do that work.
"""

from __future__ import annotations

import csv
import io
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence

from . import config

HARDLINE = [
    # Launching an agent with its own safety switched off.
    (r"--dangerously-skip-permissions", "agent launched with permissions disabled"),
    (r"--dangerously-bypass-approvals-and-sandbox", "agent launched with sandbox disabled"),
    (r"danger-full-access", "agent launched with full-access sandbox"),
    (r"bypassPermissions", "agent launched with permissions bypassed"),
    # Classic catastrophes. These matter once the coordinator runs commands a
    # model chose; today it only launches agent CLIs.
    (r"\brm\s+-[a-zA-Z]*([rR][a-zA-Z]*f|f[a-zA-Z]*[rR])[a-zA-Z]*\s+(/|~|\$HOME)/?(\s|$)",
     "recursive delete of root or home"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b.*\bof=/dev/(sd|disk|nvme|hd)", "raw write to a disk device"),
]
_HARDLINE = [(re.compile(p), why) for p, why in HARDLINE]


class HardlineRefusal(Exception):
    pass


def check_floor(cmd: List[str]) -> None:
    line = " ".join(cmd)
    for pattern, why in _HARDLINE:
        if pattern.search(line):
            raise HardlineRefusal(why)


class LocalBackend:
    """Run on this machine's shell: no isolation. A bad command reaches
    everything your user can reach. Acceptable while a human starts each run."""

    name = "local"
    isolated = False

    def run(self, cmd: List[str], cwd: Optional[str], timeout: int,
            prompt: Optional[str] = None, env: Optional[dict] = None
            ) -> subprocess.CompletedProcess:
        # The floor inspects the command, not the prompt: a brief that merely
        # mentions a dangerous command is data, not something we execute.
        check_floor(cmd)
        argv = cmd + ([prompt] if prompt is not None else [])
        # env=None would inherit everything the coordinator can see. Adapters
        # always pass the stripped environment they built (book 5.6).
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def get(cfg: dict):
    # config.load() has already rejected backends that are not built.
    return LocalBackend()


def _docker_socket() -> Path:
    # Pick an actual local socket. Docker contexts and DOCKER_HOST can point
    # at a remote engine, which would invalidate Orchestra's privacy decision.
    for path in (Path.home() / ".colima/orchestra/docker.sock", Path("/var/run/docker.sock"),
                 Path.home() / ".docker/run/docker.sock",
                 Path.home() / ".colima/default/docker.sock"):
        if path.is_socket():
            return path
    raise OSError("no local Docker Engine socket; start Docker Desktop or Colima")


def _stage_tree(source: Path, dest: Path, *, skill: bool) -> None:
    """Stage regular files. A read-only bind of a live tree could still expose
    host sockets. Directory fds and O_NOFOLLOW keep copies inside that tree.
    """
    excluded = {"__pycache__"} if skill else {
        ".git", ".env", ".ssh", ".aws", ".docker", ".orchestra", ".codex",
        ".venv", "node_modules", "__pycache__",
    }
    dest.mkdir(mode=0o755)
    count = size = 0
    state_root = config.home().resolve()

    def fail(error):
        raise error

    for directory, dirs, files, fd in os.fwalk(source, onerror=fail):
        here = Path(directory)
        folder = dest / here.relative_to(source)
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
        for name in list(dirs):
            if name in excluded or (here / name) in (dest.parent, state_root):
                dirs.remove(name)
            elif stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                if skill:
                    raise OSError("skill contains a symbolic link")
                dirs.remove(name)
        count += len(dirs)
        for name in files:
            if name in excluded or (not skill and name.startswith(".env.")):
                continue
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                if skill:
                    raise OSError("skill contains a symbolic link or special file")
                continue
            count += 1
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=fd), "rb") as src:
                info = os.fstat(src.fileno())
                size += info.st_size
                if not stat.S_ISREG(info.st_mode) or count > 20000 or size > 128 * 1024**2:
                    raise OSError("skill input exceeds the regular-file snapshot limits")
                target = folder / name
                with target.open("xb") as out:
                    # Limit reads even if a host process is growing a file.
                    remaining = info.st_size
                    while remaining:
                        chunk = src.read(min(remaining, 1024**2))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                target.chmod(0o555 if mode & 0o111 else 0o444)
        if count > 20000:
            raise OSError("skill input exceeds the snapshot entry limit")


def run_skill(cmd: Sequence[str], *, skill_dir: Path, project: Optional[Path], cwd: Path,
              timeout: int, task: str = "", files: Sequence[str] = (),
              expected_hash: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a manifest command against read-only snapshots on a local engine."""
    if not isinstance(cmd, (tuple, list)) or not cmd or not all(isinstance(a, str) and a for a in cmd):
        raise OSError("a skill command must be a nonempty list of strings")
    check_floor(list(cmd))
    binary = shutil.which("docker")
    if not binary:
        raise OSError("Docker CLI is required for skills; install Docker and start its local engine")
    socket = _docker_socket()
    image = config.load()["skills"]["docker_image"]
    project = project.resolve() if project is not None else None
    if project and (project in (Path.home(), *Path.home().parents)
                    or project == config.home().resolve()):
        raise OSError("select a project directory for the skill workspace")
    workdir = "/skill"
    if project:
        try:
            workdir = str(Path("/workspace") / cwd.resolve().relative_to(project))
        except ValueError:
            raise OSError("skill working directory is outside the project") from None

    # A dedicated Colima VM only needs this one read-only host mount.
    if socket.parent == Path.home() / ".colima/orchestra":
        parent = Path.home() / ".orchestra/docker-stage"
    else:
        parent = config.home() / "docker-stage"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skill-", dir=parent) as temporary:
        root = Path(temporary).resolve()
        skill_copy = root / "skill"
        _stage_tree(skill_dir, skill_copy, skill=True)
        if expected_hash is not None:
            from .skills import content_hash_at
            if content_hash_at(skill_copy) != expected_hash:
                raise OSError("skill changed since review")
            import yaml
            manifest = yaml.safe_load((skill_copy / "skill.yaml").read_text(encoding="utf-8"))
            action = "run" if project else "test"
            if manifest.get(action) != list(cmd) or manifest.get("required_environment_variables"):
                raise OSError("manifest command or environment differs from the reviewed offline skill")
        # Keep host credentials, Docker client proxy settings and user
        # contexts out of the launch. The image is provisioned separately.
        client_config = root / "client"
        client_config.mkdir()
        docker = [binary, "--config", str(client_config), "--host", f"unix://{socket}"]
        name = "orchestra-skill-" + uuid.uuid4().hex
        command = [*docker, "run", "--rm", "--name", name, "--pull=never",
                   "--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--user=65534:65534",
                   "--memory=256m", "--memory-swap=256m", "--cpus=1", "--pids-limit=64",
                   "--log-driver=none", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
                   "--workdir", workdir, "--mount", _mount(skill_copy, "/skill")]
        env = {"PATH": os.defpath, "ORCHESTRA_TASK": task, "ORCHESTRA_FILES": "\n".join(files),
               "ORCHESTRA_PROJECT": "/workspace" if project else "/skill"}
        if project:
            workspace = root / "workspace"
            _stage_tree(project, workspace, skill=False)
            command.extend(["--mount", _mount(workspace, "/workspace")])
        for key in ("ORCHESTRA_TASK", "ORCHESTRA_FILES", "ORCHESTRA_PROJECT"):
            command.extend(["--env", key])
        argv = [str(Path("/skill") / a) if not Path(a).is_absolute()
                and ".." not in Path(a).parts and (skill_copy / a).is_file() else a for a in cmd]
        command.extend(["--entrypoint", argv[0], image, *argv[1:]])
        try:
            return _run_bounded(command, env, timeout)
        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            # Killing the CLI alone leaves its container running.
            cleanup = subprocess.run([*docker, "rm", "-f", name], env={"PATH": os.defpath},
                                     capture_output=True, text=True, timeout=15)
            if cleanup.returncode and "no such container" not in cleanup.stderr.lower():
                raise OSError(f"Docker could not confirm removal of {name}: {cleanup.stderr.strip()}") from error
            raise


def _mount(source: Path, destination: str) -> str:
    # --mount uses CSV-style option parsing. Temp roots can contain commas.
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        ["type=bind", f"source={source}", f"target={destination}", "readonly"])
    return buf.getvalue().rstrip("\n")


def _run_bounded(command: List[str], env: dict, timeout: int) -> subprocess.CompletedProcess:
    """Stop at one MiB of combined output, including on the host side."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process, selectors.DefaultSelector() as sel:
        buffers = [bytearray(), bytearray()]
        sel.register(process.stdout, selectors.EVENT_READ, buffers[0])
        sel.register(process.stderr, selectors.EVENT_READ, buffers[1])
        size = 0
        try:
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in sel.select(remaining):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fd)
                        continue
                    size += len(chunk)
                    if size > 1024**2:
                        raise OSError("skill exceeded the 1 MiB output limit")
                    key.data.extend(chunk)
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            if process.poll() is None:
                process.kill()
        return subprocess.CompletedProcess(command, code, *(b.decode("utf-8", "replace") for b in buffers))
