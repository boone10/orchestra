# Chapter 10: Local vs Cloud Routing

## Book to code

| Book | Idea | Here |
|---|---|---|
| 10.1 | Local-first | small read-only work goes to the local model when one is running |
| 10.2 | Privacy classes as a gate that runs first | `router.allowed()` |
| 10.3 | The secrets boundary | chapter 5's brief check, unchanged |
| 10.5 | One chokepoint for policy, budget and audit | every run passes through `control.delegate()` |
| 10.5 | Per-class budgets, refuse or downgrade | `budgets` in config; `router.budget_state()` |
| 10.5 | "Did any sensitive task reach the cloud?" is a query | `orchestra report --by-privacy` |
| 10.6 | Decision and enforcement are separate | the wall is checked again at the moment of sending |

## The privacy gate

| Class | Meaning | May go to a cloud agent? |
|---|---|---|
| P0 | public | yes |
| P1 | internal | yes |
| P2 | confidential | only agents listed in `routing.approved_providers` |
| P3 | sensitive | no, unless a named exception exists for that task class and agent |
| P4 | secret | never; there is no override |
| untagged | | treated as `routing.untagged_privacy` (P3) |

A P3 exception is deliberate and attributable:

    routing:
      p3_exceptions:
        - {task_class: support-triage, agent: claude, approved_by: mike, reason: "DPA signed 2026-11"}

Each use is written to the audit log as `privacy.exception_used`, and
`report --by-privacy` lists every sensitive run that reached a cloud agent.

**The wall does not depend on the router.** `control.delegate()` asks
`router.allowed()` again just before sending, and there is a test that feeds
it a deliberately wrong routing decision to prove a P4 brief still does not
go out.

**Consequence of the strict default:** until you tag a project, orchestra
will not send its work to Claude or Codex. Tag it once:

    orchestra project set ~/throwaway --privacy P0

## The local model

`local` is a third agent. It talks to Ollama's OpenAI-compatible endpoint on
this machine (`local.base_url`, default `http://127.0.0.1:11434`) and uses
`local.model`, or the first model Ollama lists.

- It has no tools: it cannot open files or run commands. Name files with
  `--file` and orchestra puts their contents in the prompt (up to 40,000
  characters). That is acceptable only because the prompt stays here.
- It cannot edit. Private work that needs edits therefore has no eligible
  agent and waits for you.
- Its cost is recorded as 0.0, a known zero, so budgets and reports are honest.
- "Local" is checked: a `base_url` that is not this machine is refused unless
  `local.allow_remote` is set, because the privacy wall trusts that label.

Check it with `orchestra agents -v`. It should say `local: available` and name
the model. **This adapter has not been run against a real Ollama.**

## Budgets

    budgets:
      global_monthly_usd: 60
      classes:
        - {task_class: research, monthly_usd: 40, on_exhaustion: refuse}
        - {task_class: refactor, monthly_usd: 25, on_exhaustion: downgrade_to_local}

Spend is summed from the outcome log for the calendar month. When a class is
spent, a cloud run is refused (`escalated`, nothing launched) or, for
read-only work with a local model running, downgraded to it.

Blind spot: **Codex reports tokens, not dollars, so its runs count as zero.**
A budget protects you from Claude spend only until chapter 14 adds prices.

## The gateway: what was and was not done

The book's chokepoint is a proxy every *model call* passes through. Claude
Code and Codex use your subscription logins and call their providers
directly; orchestra cannot see or meter those calls, only the totals each
agent reports afterwards. What orchestra does own is the step before: every
*delegation* passes through one function, and that is where the privacy wall,
the budgets and the audit live. A run that bypasses orchestra (you typing
`claude` yourself) is outside all of it.

`docs/gateway/litellm.example.yaml` is a starting point if you later move the
agents to API keys behind a LiteLLM proxy. It is unverified.

## Not built from chapter 10

Cascades, fallbacks and speculative routing (10.9); capacity planning for
local inference (10.10); a local agent that can edit (the book's Aider);
automatic "is the local model good enough for this class" from evidence
beyond the one evidence rule in chapter 6.
