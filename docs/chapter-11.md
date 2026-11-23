# Chapter 11 · Sandboxing and guardrails

The first increment contains native skills and their manifest tests on the
personal Mac. Claude Code and Codex use their existing logins for supervised
tasks. Queued cloud work requires a person. Ollama can still answer local
read requests through the daemon.

| Book section | Implementation |
| --- | --- |
| §11.3, listing 11.1, pp. 160–163 | `backends.run_skill()` launches a fresh container on a local Docker Engine. |
| §§11.2, 11.4–11.6 | Manifest commands pass the existing hardline check. Reviewed skill content is pinned by hash. The router checks supervision and privacy before dispatch. |
| §11.7, p. 166 | Each skill has CPU, memory, process, output, and time limits. The operator stop switch removes active native skill containers and holds pending requests until resume. |
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

## Operator stop and resume

Run `orchestra stop` from another terminal. It closes admission for the
current `ORCHESTRA_HOME` and removes that home's registered native skill
containers, including manifest self-tests. Check `orchestra state` for
dispatch status and `orchestra requests` for individual jobs. The daemon
can stay running or restart while stopped. Newly submitted requests and
requests still waiting in the spool stay pending.

Run `orchestra resume` to release pending work. If Docker was unavailable
during a stop, resume retries cleanup before releasing the queue. A claimed
job interrupted by the operator is recorded as failed with the stop reason.
After a daemon crash, recovery marks a previously running job interrupted.
Both cases require a new submission to run the job again.

If a daemon dies during a native skill, run `orchestra stop` before restarting
it. The command can remove that home's orphaned container and staged input
files even after their coordinator is gone.
Cleanup identifies skills launched by this version through their home label
and registration. A skill already running at upgrade time has neither.

The switch gates new agent attempts, retries and automatic patch application.
Already admitted supervised cloud runs and local Ollama calls can finish.
An operator can still inspect and manually apply a patch or cancel an
individual pending request. Each Orchestra home controls its own work.

## Verification on the personal Mac

Two disposable Orchestra homes ran skills on the same Colima engine.
Stopping the first home removed its running container. Its first request
was recorded as failed and its second stayed pending across a daemon
restart. A second stop removed zero containers. After resume the waiting
request completed exactly once. The other home's original container ran
to completion, and neither home had a container left at the end.

A separate test killed the daemon while its Docker skill was running. A new
`orchestra stop` process removed the orphaned container, its registration,
and its staged input files before the daemon restarted. Recovery marked the
first request interrupted. The second stayed pending until resume, then ran
once.

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
agents, smart approvals, fleet-wide shutdown and spend limits, revocation of
active agent sessions, and credential rotation. The current timeout and
resource limits apply to one skill process tree. [Chapter 12](chapter-12.md)
uses these controls for scheduled work.

Docker's CLI and bind mount specifications describe the runtime options used
here. [Docker run](https://docs.docker.com/reference/cli/docker/container/run/)
and [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/).
