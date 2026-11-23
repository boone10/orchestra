# Chapter 5: Delegation via Headless CLIs

Chapter 2's seed, grown into the primitive everything else composes. The four
disciplines (5.1): assemble a brief, invoke through an adapter, validate and
capture, write the outcome back.

## Book to code

| Book | Idea | Here |
|---|---|---|
| 5.2 | The brief: guidance, relevant memory, files in scope; no secrets | `tasks.assemble()`, `--file`, `--dry-run` |
| 5.3 | The sequence, ending in a human gate for edits | `control.delegate()`; `orchestra review / apply / discard` |
| 5.4 | Validate before trusting | `control._validate()` |
| 5.5 | A regular outcome record | `outcomes.record()`; a folder per task under `tasks/` |
| 5.5 | Follow-up notes feed later work | `orchestra note <task> "..."` |
| 5.6 | Agents see only the credentials they need | `Adapter.env`, `Adapter.build_env()`; `agents.<name>.env` in config |
| 5.7 | A secret in a brief is a silent failure | `tasks.find_secrets()` blocks the send |
| 5.8 | Timeouts; bounded, deliberate retries | `delegation.retries` (read-only runs only) |
| 5.10 | Async delegation and its state model | the daemon; `needs_review`, `cancelled`, `orchestra cancel` |
| 5.11 | Cap recursion absolutely | `ORCHESTRA_DEPTH`, `delegation.max_depth` |

## The brief

Order: operator context (SOUL/MEMORY/USER), project guidance, what we already
know, files in scope, then the task last. With no context at all the brief is
just the task.

    orchestra delegate "Add input validation to the form handler" \
        --file handlers/form.py --file tests/test_form.py --dry-run

`--dry-run` prints exactly what would be sent, the size of each part, and
whether it would be blocked. Nothing runs.

- **Project guidance.** One guidance file briefs every agent. If the target
  agent reads its own file by itself (Codex and `AGENTS.md`), it is not
  repeated. Otherwise the brief carries whichever of `CLAUDE.md`, `AGENTS.md`,
  `GEMINI.md` exists, capped at `brief.guidance_max_chars`.
- **What we already know.** Up to `brief.memory_items` earlier tasks from the
  same project that share words or file names with this one, with how they
  ended and any notes you attached. It never includes what an agent wrote:
  feeding agent output back into briefs unreviewed is how memory gets poisoned
  (7.12). Your notes are the valuable part; write them with `orchestra note`.
  This is a keyword match, a placeholder for chapter 7's real retrieval.
- **Files in scope.** Name them with `--file`. They are listed in the brief,
  and an edit run that changes anything else fails validation.

## Secrets

Two separate defences, because there are two channels.

1. **The brief.** Before anything is sent, the whole brief (task, guidance
   file, memory files) is checked for values from `.env`, values of
   environment variables whose names look like credentials, and common key
   formats. A hit blocks the run, names what was found without echoing it,
   and withholds the task text from the logs.
2. **The environment.** An agent process starts with a short list of basics
   (PATH, HOME, locale, proxy settings) plus the variables its adapter
   declares: Claude gets `ANTHROPIC_*`, Codex gets `OPENAI_*`. Neither sees
   the other's key, your cloud credentials, or anything else in your shell.
   `orchestra agents -v` shows the list. To forward one more:

       agents:
         claude:
           env: [AWS_PROFILE]

What this does **not** do: the agent still runs as you, on your machine. A
credential in a file it can read (for example `~/.aws/credentials`) is not
protected by any of the above. Claude's file tools are confined to the
project; Codex's read-only sandbox can still read elsewhere. Real isolation
is the container backend in chapter 11.

## Review, then apply

Edit-level runs never touch your project.

1. Orchestra copies the project (uncommitted work included) to
   `~/.orchestra/tasks/<task>/repo` and marks the starting point.
2. The agent works in the copy.
3. Its changes come back as `changes.diff`.
4. `orchestra review` lists what is waiting; `orchestra review <task>` shows a diff.
5. `orchestra apply <task>` lands it in your project as uncommitted changes
   (you still gate the commit). `orchestra discard <task>` drops it.

If your project changed in the meantime and the diff no longer applies,
nothing is applied and you are told. A run that failed validation is held
back as `blocked`; `apply --anyway` is the deliberate override.

`approvals.mode: off` applies a valid diff automatically (the book's
fire-and-forget pattern, 5.9). Keep it for low-stakes, reversible work.

Consequences worth knowing:

- Edit runs need a git repository.
- The whole project folder is copied per edit run, dependencies and all.
  Fine for small projects; slow for large ones. Chapter 12 is where
  per-task working copies get a cheaper mechanism.
- Copies accumulate under `~/.orchestra/tasks/`. Delete old ones freely.
- The pre-run approval prompt is gone; `approve`/`deny` became
  `apply`/`discard`. The human decision moved from "may it run" to "may this
  land", which is the decision you can actually judge.

## Validation

Rules catch what rules can: a success with no answer, a read-only run that
changed the tree, an edit outside the declared scope. A failed check turns the
run into an error and is recorded. Rules cannot catch a confident, well-formed,
wrong answer; that is what the review step is for.

## Retries and recursion

- A read-only run is retried `delegation.retries` times (default 1) on a
  timeout or an unreadable reply. Each attempt is its own line in the outcome
  log, same task id. Edit runs are never retried automatically.
- An agent started by orchestra is told its depth and cannot start further
  agents through orchestra (`delegation.max_depth: 1`).

## Patterns (5.9, 5.11): what exists

| Pattern | Status |
|---|---|
| Single supervised delegation | Built: the default for edits |
| Fire-and-forget | Available via `approvals.mode: off`; no scheduler to drive it yet |
| Chained | Not built (chapter 13) |
| Parallel fan-out | Not built: the daemon runs one request at a time (chapter 12) |
| Recursive | Refused by the depth cap |

## Mistakes to avoid (5.14): status

| Mistake | Status |
|---|---|
| Delegating without a brief | Fixed, to the extent memory exists; richer in chapter 7 |
| Trusting unvalidated output | Structural checks built; correctness is the human's call |
| Forwarding the whole environment | Fixed: stripped by default |
| Skipping the write-back | Impossible by construction |
| Hard-coding the agent | Adapters only |

## Not built from chapter 5

Skill declarations as files (Listing 5.2; the same information lives on each
adapter until chapter 8), stopping a run already in progress, progress
reporting from long runs, running tests as part of validation (13), and
choosing the agent (6).

## November 2026 outcome-log incident

On November 19 the personal Mac's outcome log became empty in the same
second a native skill finished. Shell history contains pasted terminal output
whose old status footer would redirect output into the outcome log if
interpreted by zsh. The history entry has no timestamp, so a pasted footer is
the probable cause. On November 22 recovery restored all 42 audit completions
and $2.2707 of recorded cost. Seven records lack the original task wording.
The backup matched byte for byte, and repeating recovery added zero rows.

Use `orchestra report --recover` to fill missing records from the audit log.
Newer audit events omit task wording. The CLI prints saved paths in plain
language so the status footer is safe to paste into a shell.
