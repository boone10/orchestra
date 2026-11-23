"""The terminal backend: where dispatched commands actually run (book 4.5).

Supervised cloud CLIs use the local terminal. Native skills and manifest
self-tests run in offline Docker containers through run_skill().

Under every backend sits the hardline floor (book 4.6): patterns refused no
matter what the configuration says. It is a backstop against catastrophe, not
the safety strategy. The approval mode and (later) the sandbox do that work.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
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
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import List, Optional, Sequence

from . import config, stop_switch

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


def _local_sockets():
    return (Path.home() / ".colima/orchestra/docker.sock", Path("/var/run/docker.sock"),
            Path.home() / ".docker/run/docker.sock", Path.home() / ".colima/default/docker.sock")


def _docker_socket() -> Path:
    # Pick an actual local socket. Docker contexts and DOCKER_HOST can point
    # at a remote engine, which would invalidate Orchestra's privacy decision.
    for path in _local_sockets():
        if path.is_socket():
            return path
    raise OSError("no local Docker Engine socket; start Docker Desktop or Colima")


def _home_id() -> str:
    return hashlib.sha256(os.fsencode(config.home().resolve())).hexdigest()


def _skill_label() -> str:
    return "io.orchestra.skill-home=" + _home_id()


def _skill_containers(docker: List[str], name: Optional[str] = None) -> List[str]:
    args = ["ps", "-aq", "--no-trunc", "--filter", "label=" + _skill_label()]
    if name:
        args.extend(["--filter", f"name=^/{re.escape(name)}$"])
    reply = subprocess.run([*docker, *args], env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=10)
    if reply.returncode:
        raise OSError(f"cannot list Orchestra skill containers: {reply.stderr.strip()}")
    ids = reply.stdout.split()
    if any(not re.fullmatch("[0-9a-f]{64}", cid) for cid in ids):
        raise OSError("Docker returned an invalid container ID")
    return ids


def _remove_container(docker: List[str], cid: str) -> bool:
    reply = subprocess.run([*docker, "rm", "-f", cid], env={"PATH": os.defpath}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=15)
    if not reply.returncode:
        return True
    if "no such container" in reply.stderr.lower():
        return False
    raise OSError(f"Docker could not confirm removal of {cid}: {reply.stderr.strip()}")


def stop_skills() -> int:
    """Sweep this home's containers while stop holds the exclusive admission lock."""
    engines = {}
    removed, failures = 0, []
    for marker in (config.home() / "active-skills").glob("*.socket"):
        try:
            with marker.open(encoding="utf-8") as reg:
                socket = Path(reg.read())
                reapable = engines.setdefault(socket, [])
                # No new create can enter now. Check the lock before the
                # listing so a late orphan response can't escape that sweep.
                try:
                    fcntl.flock(reg, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    failures.append(f"{marker.stem}: Docker create is still in progress; "
                                    "retry orchestra stop when the Docker client exits")
                else:
                    reapable.append(marker)
        except FileNotFoundError:
            continue  # a completed run removed its registration
    if not engines:
        return 0
    binary = shutil.which("docker")
    if not binary:
        raise OSError("active skills are registered but the Docker CLI is unavailable")
    # A run might have started on a different local engine before the current
    # preferred socket appeared. Only use previously registered local sockets.
    with tempfile.TemporaryDirectory(prefix="orchestra-docker-client-") as client:
        for socket, markers in engines.items():
            if socket not in _local_sockets():
                failures.append("unrecognized Docker socket in active skill registration")
                continue
            docker = [binary, "--config", client, "--host", f"unix://{socket}"]
            try:
                ids = _skill_containers(docker)
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(f"{socket}: {error}")
                continue
            before = len(failures)
            for cid in ids:
                try:
                    removed += int(_remove_container(docker, cid))
                except (OSError, subprocess.TimeoutExpired) as error:
                    failures.append(f"{socket}: {error}")
            if len(failures) == before:
                for marker in markers:
                    marker.unlink(missing_ok=True)
    if failures:
        raise OSError(f"removed {removed} container(s); cleanup unconfirmed on " + "; ".join(failures))
    return removed


@contextmanager
def _skill_stage(socket: Path):
    parent = config.home() / "docker-stage"
    if socket.parent == Path.home() / ".colima/orchestra":
        parent = Path.home() / ".orchestra/docker-stage"
    with ExitStack() as cleanup:
        with stop_switch.admission():
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = tempfile.TemporaryDirectory(prefix=f"skill-{_home_id()}-", dir=parent)
            fd = os.open(stage.name, os.O_RDONLY | os.O_DIRECTORY)
            cleanup.callback(os.close, fd)
            cleanup.enter_context(stage)  # remove files before releasing their lock
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield Path(stage.name).resolve()


def reap_abandoned_stages() -> None:
    """Called under exclusive dispatch lock after container removal succeeds."""
    prefix = f"skill-{_home_id()}-"
    parents = {config.home() / "docker-stage", Path.home() / ".orchestra/docker-stage"}
    for parent in parents:
        for stage in parent.glob(prefix + "*"):
            try:
                fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue  # a finishing runner cleaned its files
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # a live coordinator owns this snapshot
                if stage.exists():
                    shutil.rmtree(stage)
            finally:
                os.close(fd)


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
    if stop_switch.is_stopped():
        raise stop_switch.Stopped()
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

    with _skill_stage(socket) as root:
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
        command = [*docker, "create", "--rm", "--name", name, "--label", _skill_label(), "--pull=never",
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
        marker = config.home() / "active-skills" / (name + ".socket")
        registered = False
        cid = None
        rejected = False
        deadline = time.monotonic() + timeout
        try:
            with stop_switch.admission():
                marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                tmp = marker.with_suffix(".tmp")
                with tmp.open("x", encoding="utf-8") as reg:
                    # Only create inherits this registration lock. Stop can
                    # latch and sweep even if the coordinator dies and the
                    # orphaned Docker client is still talking to its engine.
                    fcntl.flock(reg, fcntl.LOCK_EX)
                    reg.write(str(socket))
                    reg.flush()
                    tmp.replace(marker)
                    registered = True
                    created = _run_bounded(command, env, min(10, timeout), pass_fds=(reg.fileno(),))
                if created.returncode:
                    # A daemon rejection (e.g. missing image) has a definite
                    # response. A transport failure can still be in flight.
                    rejected = "error response from daemon:" in created.stderr.lower()
                    return created
                cid = created.stdout.strip()
                if not re.fullmatch("[0-9a-f]{64}", cid):
                    cid = None
                    raise OSError("Docker create returned an invalid container ID")
            # An intervening stop removes even a created-but-not-started ID.
            # Docker cannot recreate a missing container with 'start'.
            result = None
            try:
                result = _run_bounded([*docker, "start", "--attach", cid], {"PATH": os.defpath},
                                      max(0.0, deadline - time.monotonic()))
            finally:
                # Even if attach times out, a stop that removed this job
                # wins over a retry, including after a quick resume.
                with stop_switch.admission():
                    if not marker.exists():
                        raise stop_switch.Stopped()
                    if result is not None and result.returncode == 0:
                        # A successfully attached run has exited and --rm reaps
                        # it even if the runner dies after we drop this marker.
                        marker.unlink()
                        registered = False
            return result
        finally:
            if registered and marker.exists():
                # Even if the creating client lost its response, a label and
                # exact-name lookup can find only this home's own container.
                try:
                    ids = [cid] if cid else _skill_containers(docker, name)
                    for item in ids:
                        _remove_container(docker, item)
                except subprocess.TimeoutExpired as exc:
                    raise OSError("Docker timed out confirming skill cleanup") from exc
                # An unanswered create may still be settling in the engine.
                # Keep that registration for a later sweep if no ID is visible.
                if ids or rejected:
                    marker.unlink(missing_ok=True)


def _mount(source: Path, destination: str) -> str:
    # --mount uses CSV-style option parsing. Temp roots can contain commas.
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        ["type=bind", f"source={source}", f"target={destination}", "readonly"])
    return buf.getvalue().rstrip("\n")


def _run_bounded(command: List[str], env: dict, timeout: float, *,
                 pass_fds: Sequence[int] = ()) -> subprocess.CompletedProcess:
    """Stop at one MiB of combined output, including on the host side."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, env=env, pass_fds=pass_fds, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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
