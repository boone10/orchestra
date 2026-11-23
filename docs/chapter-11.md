# Chapter 11 · Sandboxing and guardrails

The first increment contains native skills and their manifest tests on the
personal Mac. Claude Code and Codex use their existing logins for supervised
tasks. Queued cloud work requires a person. Ollama can still answer local
read requests through the daemon.

| Book section | Implementation |
| --- | --- |
| §11.3, listing 11.1, pp. 160–163 | `backends.run_skill()` launches a fresh container on a local Docker Engine. |
| §§11.2, 11.4–11.6 | Manifest commands pass the existing hardline check. Reviewed skill content is pinned by hash. The router checks supervision and privacy before dispatch. |
| §11.7 | Each skill has CPU, memory, process, output, and time limits. Orchestra requests removal of that container on timeout or interruption. |
| §§11.8–11.9 | The audit and outcome logs capture delegation decisions. Interactive cloud work keeps the existing review-then-apply gate. |

## Running a skill

Use a local Docker Engine with `python:3.12-slim` preloaded. To use a
different preloaded image, set `skills.docker_image` to its reference in
`config.yaml`. An image digest pins the runtime across machines. The Python
image includes the standard library used by the shipped link checker.

The personal Mac uses a Colima profile named `orchestra`. Its only configured
host mount is the read-only staging directory at `~/.orchestra/docker-stage`.
After a reboot, these commands start that profile and test the installed
example. They work from any directory.

```sh
colima start --profile orchestra --activate=false
orchestra skill test check-doc-links
```

On another Mac with Docker CLI and Colima installed, create the dedicated
profile from any directory with these commands.

```sh
mkdir -p -m 700 "$HOME/.orchestra/docker-stage"
colima start --profile orchestra --activate=false --runtime docker --cpus 2 --memory 2 --disk 12 --port-forwarder none --mount "$HOME/.orchestra/docker-stage"
docker --context colima-orchestra pull python:3.12-slim
```

The runner also recognizes standard local Docker Engine, Docker Desktop,
and default Colima sockets. A missing engine or image produces an error.
For the other engines, the state directory's `docker-stage` folder must be
accessible to the local Docker daemon.

## The execution boundary

The container runs as UID and GID 65534 with all Linux capabilities dropped,
privilege escalation disabled, a read-only root, and a 32 MiB temporary
filesystem. Its limits are one CPU, 256 MiB of memory, 64 processes,
1 MiB of combined output, and the run timeout. The engine uses a local image
and the container has only the loopback interface.

Orchestra stages regular files, mounts the reviewed skill at `/skill`, and
mounts project input at `/workspace` for a delegation. A manifest self-test
sees the skill and its fixtures. Both mounts are read-only. The workspace
omits symlinks, sockets, Git metadata, environment files and common private
state or dependency directories. Projects whose links rely on omitted files
can therefore produce different link-check results. Each input tree is
limited to 20,000 entries and 128 MiB. The image supplies the interpreter.
The only task metadata forwarded is the task, project path inside the
container, and project-relative file list. Skills that declare a host
environment dependency require a different implementation before they can run.

## Verification on the personal Mac

On November 22, 2026, a diagnostic inside the real container observed UID
65534, zero effective capabilities, `NoNewPrivs=1`, and only loopback. The
root, skill and workspace mounts were read-only. The kernel reported a
268,435,456-byte memory limit, a 64-process limit and one CPU of quota. A
host environment marker was absent. A second run timed out after one second,
and the dedicated engine had no remaining skill container. The preloaded image
digest was `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`.

The installed link checker's self-test passed in Docker. In disposable state,
the normal trust command ran its test, a queued invocation found the one
broken link among two, and a subsequent change to the skill revoked trust.
An explicitly named cloud request submitted with supervised metadata went
to `needs_human`. A supervised Claude edit through a symlinked project path
produced a one-file patch for review and left the original project clean.

The local daemon returned the expected value from a named file through
Ollama. The Claude benchmark incurred $0.4267 of reported usage. Setting a
disposable class budget to that measured spend caused the next request to
escalate before launch. The [cloud benchmark record](chapter-03.md) covers
both supervised CLIs.

## Remaining chapter work

The system still needs inspection of individual tool actions inside cloud
agents, smart approvals, a fleet-wide stop switch and spend limits, credential
rotation, and testing of recovery after the coordinator is killed abruptly.
The current timeout and limits apply to one skill process tree. Scheduling
remains deferred.

Docker's CLI and bind mount specifications describe the runtime options used
here. [Docker run](https://docs.docker.com/reference/cli/docker/container/run/)
and [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).
