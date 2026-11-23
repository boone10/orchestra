# orchestra

A small control plane that operates command-line agents, built chapter by
chapter from *Orchestrating AI Agents* (Yohan J. Rodríguez). Python 3.9+, one
dependency (PyYAML).

Status: chapters 2 to 10 built. `docs/chapter-0N.md` maps the
code to the book and lists what is deliberately left for later chapters.

## Install

From this folder:

    pip install -e .
    orchestra init
    orchestra state

After that `orchestra` works from any directory. (Without installing,
`python3 -m orchestra ...` works from this folder if PyYAML is present.)

## Commands

Declare each project once (untagged work is treated as sensitive and is not
sent to cloud agents):

    orchestra project set ~/work/site --privacy P1      # P0 public .. P4 never leaves
    orchestra project list

Run a task. The router picks the agent; `--agent` overrules it:

    orchestra route "Refactor the parser"              # what would happen, and why
    orchestra delegate "Summarize what this repository does" --cwd ~/work/site
    orchestra delegate "..." --file src/app.py --dry-run     # see the brief; send nothing
    orchestra delegate "Add a docstring to top()" --file app.py --access edit
    orchestra delegate "..." --stakes high --class refactor  # declare what you know

Review what agents changed (edit runs never touch your project directly):

    orchestra review                 # diffs waiting for you
    orchestra apply <task>           # land it, uncommitted
    orchestra discard <task>

Memory:

    orchestra note <task> "lesson"                     # tied to a past task
    orchestra memory propose "..." --project ~/work/site
    orchestra memory list  |  accept <id>  |  quarantine <id>
    orchestra recall "topic"

Skills:

    orchestra skill examples         # install two examples as drafts
    orchestra skill show <name>      # read it
    orchestra skill trust <name>     # your review; editing it revokes trust
    orchestra skill list

Queue work for the daemon:

    orchestra daemon                 # foreground; Ctrl-C to stop
    orchestra submit "Audit the docs for broken links" --cwd ~/work/site
    orchestra requests  |  cancel <id>

Know your agents and your spend:

    orchestra agents -v              # claude, codex, local: profiles and last bench
    orchestra bench claude
    orchestra report --by-class      # the weekly review
    orchestra report --by-privacy    # did anything sensitive reach the cloud?
    orchestra mcp list               # tool servers on record
    orchestra state

`--access read` (default) lets an agent look and answer. `--access edit` lets
it work in a private copy of the project and hand you a diff. High-stakes work
is never applied for you.

## State

Everything the control plane knows is in `~/.orchestra` (set `ORCHESTRA_HOME`
to move it): `config.yaml`, `.env` (secrets), `SOUL.md` / `MEMORY.md` /
`USER.md` (sent with every brief; keep them small), `tasks/` (the brief,
answer and diff for every delegation), `requests/`, `outcomes.jsonl`,
`notes.jsonl`, `logs/audit.jsonl`. It is all plain text. Back it up.

## Layout

    orchestra/
      contract.py          the one Result shape the control plane sees
      config.py            config.yaml + .env, kept separate
      state.py             state directory, bounded memory, audit log
      control.py           the delegation primitive: delegate() and compare()
      router.py            four axes, three outcomes, the privacy wall, budgets
      memory.py            memory records: propose, curate, retrieve, quarantine
      skills.py            the skills registry and the native skill runner
      mcp_server.py        tool-server registrations and the memory server
      tasks.py             briefs, the secrets check, review-then-apply
      bench.py             capability and safety probes for an agent
      backends.py          where commands run + the hardline floor
      spool.py             the daemon and its durable request spool
      adapters/            one file per agent (claude, codex, local)
      workspace.py         what a run did to the tree, isolated copies, context files
      outcomes.py          outcomes log: record, read, summarize
      cli.py               the commands above
    tests/                 run against stand-in agents; cost nothing

## Tests

    python3 -m unittest
