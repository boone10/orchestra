# Chapter 8: Skills as a Capability Registry

A skill is a folder in `~/.orchestra/skills/<name>/`: `skill.yaml` (what the
registry matches on), `SKILL.md` (the procedure), and for native skills
`scripts/` and `fixtures/`.

## Book to code

| Book | Idea | Here |
|---|---|---|
| 8.1, 8.2 | A procedure on disk, with owner, version, test | the folder; `skill.yaml` fields |
| 8.3 | Three loading levels | `skills.names()`, `summaries()`, `body()` |
| 8.4 | Matching, and a match log | `skills.match()`; `skill.match` in the audit log; shown by `orchestra route` |
| 8.5 | Native, delegation, tool | `category:` |
| 8.7 | Draft, test, review, trust, observe | `orchestra skill new / test / trust / list` |
| 8.8 | Versioning and deprecation | version recorded on every outcome; `orchestra skill deprecate` |
| 8.9 | Permission is part of matching | `_admissible()`; declared environment only |
| 8.10, 8.11 | Agent-written and imported skills are drafts until read | same trust step for every skill |

## The lifecycle

    orchestra skill examples                 # two examples, installed as drafts
    orchestra skill show check-doc-links     # read every file in it
    orchestra skill test check-doc-links     # run its own fixture test
    orchestra skill trust check-doc-links    # your review, recorded
    orchestra skill list

Trust is stored outside the skill folder (`skills.lock.json`) together with a
hash of every file in the skill. **Change any file and the skill becomes
`modified` and stops being used until you trust it again.** That is the
supply-chain rule from 8.11 applied to every skill, including your own.

A skill whose test fails cannot be trusted. A skill with no test needs
`--without-test`, on purpose.

## The three classes

- **Native skills** run locally and incur no model charge. Chapter 11 runs
  them against read-only project snapshots in Docker. The same boundary
  applies to manifest tests during review. [Runner and setup](chapter-11.md)
- **delegation**: the body of `SKILL.md` goes into the brief as the procedure,
  and the skill may name a preferred agent. The router's privacy and stakes
  rules still apply to that choice.
- **tool**: declared and listed, not runnable. See chapter 9.

## Matching

A task matches a skill when it shares enough words with the skill's name,
description and `triggers` (`skills.match_threshold`, default 2). Then it must
be admissible: trusted; not a writer on a read-only task; declared
`unattended: true` if the request came from the daemon; not a writer on a
high-stakes task; within its `max_privacy`. The best admissible match is used.
`orchestra route "<task>"` shows every candidate and why it was or was not used.

`--skill NAME` forces one (it must still be trusted and admissible);
`--no-skill` turns matching off.

## The examples

- `check-doc-links` (native): reports relative Markdown links whose target
  does not exist. Read-only, no network.
- `careful-code-change` (delegation): a five-step procedure for a supervised
  edit, added to the brief.

Read them before trusting them, like any other skill.

## Not built from chapter 8

Agents drafting skills from their own runs (8.10); a fixture harness beyond
"run the declared test command"; the
periodic registry review as a command (`skill list` shows use and failures).
