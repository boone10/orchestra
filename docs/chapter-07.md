# Chapter 7: Memory Across Harnesses

Three places, by trust and by how often they are read.

| Layer | File | Who writes | When read |
|---|---|---|---|
| Bounded core | `SOUL.md`, `MEMORY.md`, `USER.md` | you | every brief |
| Memory records | `memory.jsonl` | you; agents may only propose | by relevance |
| Archive | `outcomes.jsonl` | orchestra, after every run | by relevance, and by the router |

## Book to code

| Book | Idea | Here |
|---|---|---|
| 7.3 | The core is a budget | `orchestra state` shows its size against `memory.max_chars` |
| 7.4 | A record carries type, scope, source, time, confidence, review state, summary, evidence, revisit | `memory.add()` |
| 7.5 | Archive freely, propose cautiously, curate by hand | `proposed` -> `orchestra memory accept` -> `curated`; `--core` for MEMORY.md |
| 7.6 | Every harness writes back to one memory | unchanged from chapter 5: one outcome log for all agents |
| 7.7 | Query, do not dump; rank by fit, filter by trust | `memory.search()`, `tasks.relevant()`, `orchestra recall` |
| 7.8, 7.11 | Record which memories a brief used, so a bad one can be traced | `memories_used` on every outcome; `orchestra memory show <id>` |
| 7.12 | Poisoning, staleness | `quarantine`, `stale`, `--revisit` dates, `memory list --due` |
| 7.10 | Records are the asset; indexes are derived | plain JSON lines, schema-versioned |

## Using it

    orchestra memory propose "API tests use pytest; frontend tests use Jest" --project ~/work/site --type fact
    orchestra memory list
    orchestra memory accept <id>            # now trusted
    orchestra memory accept <id> --core     # and sent with every brief; use sparingly
    orchestra memory quarantine <id>        # pulled from use; shows which briefs had it
    orchestra recall "test runner" --cwd ~/work/site
    orchestra note <task> "lesson"          # shortcut: a curated record tied to a task

Scope is one of a project (`--project`), a task class (`--class`), or
everywhere (`--global`). A record is only considered for tasks in its scope.

## What reaches a brief

Records in scope that share at least two meaningful words with the task (a
shared file name counts double), best match first, then most trusted. Each is
labelled so the agent can weigh it: `confirmed by the operator`,
`tentative, not yet reviewed`, `due for re-checking`. Then earlier runs in the
same project, described by what was asked and how it ended.

Two deliberate restrictions:

- **An agent's unreviewed proposal is never put in a brief.** The book allows
  proposed memories to be used with a label. Here that holds for proposals
  you wrote; one from an agent waits until you accept it. A lesson planted by
  hostile repository content is the obvious attack, and this closes it.
- **What an agent said is never fed back.** Earlier runs appear as the task
  and its status, not the agent's answer.

## Not built from chapter 7

- **Full-text or semantic search.** Matching is on shared words. It will miss
  a relevant lesson phrased differently (a recall failure, 7.8). The book's
  FTS5 index is the natural upgrade; the records would not change.
- **Measuring retrieval quality.** The trail exists (`memories_used`), but
  nothing yet scores whether a memory helped.
- **Automatic lesson extraction** from runs. Agents can propose through the
  MCP server (chapter 9); orchestra does not mine their answers.
- **A session archive.** There are no multi-turn sessions to archive.
- **Sharding and federation** (7.13) beyond the three scopes.
