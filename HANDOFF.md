# Orchestra handoff

**Resume with Mike at chapter 14, §14.1, “The Accounting Seam.”** It starts on printed page 216, PDF page 228, of *Orchestrating AI Agents* by Yohan J. Rodríguez. The desktop copy is `~/Downloads/orchestrating-ai-agents.pdf`. For the reading session, use Mike’s copy of the PDF on whichever Mac he is using.

## Where we left off · November 24, 2026

- **Code.** Orchestra is a Python control plane for Claude Code, Codex CLI, Ollama, and trusted native skills. Commit `170a1a1` is the chapter 12 baseline, with 159 passing tests. Native skills run in offline Docker containers. Cloud runs are supervised. Desktop tests exercised recurring jobs, parallel reads, and recovery after a daemon crash. [Chapter 11 containment](docs/chapter-11.md) · [Chapter 12 scheduling](docs/chapter-12.md)
- **Reading.** Mike finished chapter 13 and its §13.7 exercise. The notes trace a bug report through a failing test, a private patch, verification, and human approval at merge. Chapter 13 was a design exercise. A general compile-and-verify workflow remains future implementation work. Continue the shared study notes in this repo so they travel between machines. [Chapter 13 notes and worked example](docs/chapter-13.md)
- **Approval rule.** Routine fixes can proceed from an approved brief through coding and tests, with Mike approving the merge. For large or risky changes he reviews the proposed approach before coding. **Any change to stored data or any database migration requires Mike’s approval before an agent starts coding, even for a small patch.**

For §14.1, reuse the chapter 13 discount bug. What would you record to find the total cost of one completed fix, including retries? The outcome log already records request and task IDs, attempts, tokens, and reported cost. Claude supplies a dollar figure when available. Codex supplies tokens with an unknown dollar cost, so its runs are absent from the monthly dollar total used by the budget check. Start the discussion there. [Outcome records](orchestra/outcomes.py) · [Budget checks](orchestra/router.py)

## First session on the laptop

The shared remote is [boone10/orchestra](https://github.com/boone10/orchestra), a private GitHub repo. The laptop needs Git credentials with read access to clone and write access to push. Use the laptop’s own GitHub login or SSH key.

If the laptop already has a checkout of this remote, open it and run `git status -sb` and `git remote -v`. With a clean `main`, run `git pull --ff-only`. An older unpacked project may exist at `~/_source/orchestra`. If that folder has work outside Git, preserve it and clone alongside it. For a fresh clone, these commands use a new folder under your home directory.

```sh
git clone git@github.com:boone10/orchestra.git ~/orchestra-laptop
cd ~/orchestra-laptop
```

For reading, open `HANDOFF.md` and the linked chapter notes. For development, run the following from the checkout root using Python 3.9 or newer. The desktop baseline used standard Python 3.12.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest
orchestra init
orchestra state
```

The tests use stand-ins. In a later terminal, activate the same virtual environment to use its `orchestra` command. For live native skills, follow the [local Docker setup](docs/chapter-11.md#running-a-skill). Cloud CLI logins are also local to each Mac.

## Switching computers

1. **Before leaving a Mac**, update this handoff with the next section or open decision. Review `git status -sb`, commit the code or notes you want to share, and `git push`. Push work in progress on a named branch and tell the next agent its name.
2. **On the other Mac**, open its checkout, check for local edits, switch to the shared branch, and run `git pull --ff-only`. If the branches have diverged, preserve both sets of commits and reconcile them before continuing.
3. **Keep the desktop as the schedule owner.** Its `daily-links` job checks `~/throwaway` at 9 a.m. Louisville time while its daemon and Docker engine are running. Each Mac has its own `~/.orchestra` containing configuration, skill trust, credentials in `.env`, schedules, request history, and spend records. Keep that directory out of Git. A fresh laptop state starts with scheduling disabled. Either Mac can edit the repo while the desktop owns the recurring job.
