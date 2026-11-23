# Chapter 4: The Control Plane

> Updated by chapter 5: the approval step described here (approve before an
> edit run) was replaced by review-then-apply. See `chapter-05.md`.

The book uses Hermes as its worked example and says to read the chapter for
the shape, not the product. Orchestra takes the "build" path from 4.11.

## Book to code

| Book | Idea | Here |
|---|---|---|
| 4.1 | A daemon, not a command | `orchestra daemon`, `spool.py` |
| 4.2, 4.9 | State is a directory you can read | `orchestra init`, `orchestra state`, `state.py` |
| 4.3, 4.10 | One request lifecycle for every surface | `control.delegate()`; the CLI and the daemon both call it |
| 4.4 | Small always-in-context memory | `SOUL.md`, `MEMORY.md`, `USER.md`, prepended to every brief |
| 4.5 | The terminal backend is a config knob | `backends.py`; adapters no longer call subprocess themselves |
| 4.6 | Approval mode | `approvals.mode`; gate in `control.delegate()` |
| 4.6 | A hardline floor no setting disables | `backends.HARDLINE` |
| 4.7 | Gateways | the `requests/` spool, written by `orchestra submit` |
| 4.8 | Config separate from secrets | `config.yaml` and `.env`, `${NAME}` substitution |
| 4.14 | Survive a restart without duplicating work | `spool.recover()`; covered by a kill-the-daemon test |

## The request lifecycle

    intake -> route -> approval gate -> assemble brief -> execute -> capture -> persist

- **Route**: uses the default agent unless you name one. A real router is chapter 6.
- **Approval gate**: any `edit` run needs a human while `approvals.mode` is
  `manual`. At the command line you are asked (or pass `--approve`); with no
  terminal the answer is no. In the daemon the request waits as
  `awaiting_approval` until you run `orchestra approve <id>`. A refusal is
  recorded as an outcome with status `denied`, and no agent is launched.
- **Assemble brief**: the three memory files, then the task. Use
  `orchestra delegate "..." --dry-run` to see exactly what an agent receives.
  HTML comments in those files are not sent. This is the first step toward
  fixing "delegating without a brief" from 2.13.
- **Persist**: one line in `outcomes.jsonl`, plus the decision trail in
  `logs/audit.jsonl`.

## Decisions that differ from Hermes, and why

- **Rule-based coordinator.** Orchestra makes no model calls of its own. The
  `model` and `auxiliary` settings exist in config but nothing reads them.
  Consequence: `approvals.mode: smart` (a model classifies risk) cannot work
  yet and behaves as `manual`.
- **No network port.** The daemon's only gateway is a directory. Anyone who
  can submit a request can already write to your home folder, so there is no
  new attack surface (4.15: a daemon listening on a port is a target). A
  messaging or HTTP gateway can be added later behind the same spool.
- **Interrupted work is not re-run.** If the daemon dies mid-request, that
  request becomes `interrupted` and stays that way. The agent may already
  have changed files, and repeating it blindly is the double-action failure
  4.13 warns about. Resubmit it yourself if it is safe.
- **No scheduler.** You asked for the daemon; the book is firm that
  unattended scheduling comes last (4.11, 4.16), and the `cron/` folder is
  created but nothing reads it. Chapter 12 builds this on top of the spool.
- **Only the `local` backend.** The knob and the seam exist; `docker`, `ssh`
  and `sandbox` are rejected with a clear message until chapter 11.

## What the safety layers do and do not cover

| Layer (4.6) | Status |
|---|---|
| Pre-execution scanner | Not built (chapter 11) |
| Approval mode | Built for the read/edit distinction |
| Read-level verification | Built: a `read` run that changes the tree is recorded as an access violation (measured from git, so only in git repositories) |
| Hardline floor | Built: refuses launching an agent with its own safety disabled, plus classic catastrophic commands |
| Backend isolation | **Not built.** `local` means no isolation at all |

Be clear-eyed about the floor: orchestra only launches agent CLIs, so today
the floor mainly guarantees no adapter can ever start an agent in a
skip-all-permissions mode. What the agent then does inside an `edit` run is
governed by the agent's own permission system, not by orchestra.

## Running the daemon

    orchestra daemon

runs in the foreground and logs each step. That is the right way to run it
for now. The book's warning (2.11, 4.16) applies in full: this process
launches other programs with your credentials, on your machine, with no
isolation. With approvals on `manual` and no scheduler it cannot do anything
you did not ask for, which is why those two defaults matter. Do not set
`approvals.mode: off` and leave it running unattended on a workstation.

Note: YAML reads a bare `off` as false. Orchestra accepts that for
`approvals.mode`, but quoting it (`"off"`) is clearer.

## Mistakes to avoid (4.16): status

| Mistake | Status |
|---|---|
| Running it on your main machine | **Open.** That is where it runs today. Acceptable only while a human starts and approves the work |
| Starting with the scheduler | Avoided: not built |
| Letting the bounded memory bloat | Guarded: `orchestra state` shows the size against `memory.max_chars` and warns |
| Trusting the blocklist as your safety | See the table above |
| Treating Hermes as the point | Avoided: same shape, different decisions |

## Not built from chapter 4

Agent loop with a model, skills matching (8), searchable session archive (7),
auxiliary models, messaging gateways, scheduler (12), high availability and
multi-environment promotion (4.12), backup and restore tooling (4.13).
For backup today: copy `~/.orchestra`, minus `.env` if the copy leaves your machine.
