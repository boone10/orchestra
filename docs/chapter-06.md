# Chapter 6: The Routing Engine

You no longer name the agent. The router reads four properties of a task and
picks one of three outcomes; `--agent` remains as an override.

    orchestra route "Refactor the parser across the codebase" --cwd ~/proj
    orchestra delegate "..." --stakes high --privacy P1 --class refactor

## Book to code

| Book | Idea | Here |
|---|---|---|
| 6.1 | Four axes: stakes, context, privacy, autonomy | `router.classify()`; `--stakes --context --privacy --autonomy` |
| 6.2 | Three outcomes | `act-direct`, `delegate`, `escalate-human` in `router.decide()` |
| 6.3 | High stakes always escalates | first check in `decide()`; enforced at the apply gate in `control.delegate()` |
| 6.5 | Declare the risky axes, infer the rest conservatively | every axis records `declared_by` and `confidence` |
| 6.5 | Then choose the agent by fit plus evidence | adapter `fit`, `router.evidence()` |
| 6.8, 6.9 | Route on the outcome record; review it weekly | `orchestra report --by-class` |
| 6.10 | Record the decision, not just the outcome | `route` in every outcome line; `route.decided` in the audit log |
| 6.11 | Routing tables as code | the rules are `router.py` and `config.yaml`, both plain text you can version |

## How a task gets its values

| Axis | Declared by | Otherwise |
|---|---|---|
| privacy | `--privacy`, or the project's tag | `routing.untagged_privacy` (P3): treated as sensitive |
| stakes | `--stakes`, or the project's tag | `high` if the wording or a file path suggests consequence (payments, deploy, auth, CI...); else `med` |
| context | `--context` | `large` if more than three files or words like "refactor", "across", "whole" |
| autonomy | `--autonomy`, or the project's tag | `unattended` for the daemon, `supervised` at the command line |
| task class | `--class`, or a matched skill | inferred from the wording (docs, test, refactor, review, ...) |

Declare a project once, and everything under that folder inherits it:

    orchestra project set ~/work/site --privacy P1
    orchestra project set ~/work/billing --privacy P2 --stakes high
    orchestra project list

Inference only ever rounds up. A declared value always stands, and if the
wording disagrees the router says so in the decision.

## The decision, in order

1. **Stakes.** `high` means escalate. See below for what that does.
2. **Privacy.** Decides which agents may be considered at all (chapter 10).
3. **Size and autonomy.** Large context goes to the agent whose profile says
   large context (Claude). An unattended run goes to the agent that sandboxes
   itself (Codex). Small, bounded read-only work goes to the local model if
   one is running; otherwise to a cloud agent.
4. **Evidence.** If the chosen cloud agent has succeeded on under 60% of at
   least `routing.min_evidence` runs of this task class, and another eligible
   agent on at least 80%, the router switches and says why.
5. **Budget.** Chapter 10.

## What "escalate" means here

The book: agents may prepare; humans authorize consequence. So a high-stakes
task still runs, but:

- its diff is never applied automatically, whatever `approvals.mode` says;
- `orchestra review` marks it HIGH STAKES;
- a write-capable skill will not match it.

Three levels in practice: `low` may land unread if you have set
`approvals.mode: off`; `med` (the default) always waits for you; `high`
always waits for you and no setting changes that.

If no agent is eligible at all (privacy, budget, nothing installed) the task
ends as `escalated` and nothing is launched. The daemon marks such a request
`needs_human`.

## Decisions that differ from the book

- **Escalation does not block the run.** Since edits already land only on
  `orchestra apply`, the human gate is where the consequence is. Blocking the
  preparation as well would add friction without adding safety.
- **Tasks are not split along a seam** (inspect vs change, draft vs apply)
  automatically. You split them: a read-only `delegate` to inspect, then an
  `--access edit` one to draft.
- **No learned routing, no replay of a rule change against history, no A/B**
  (6.11). The evidence rule above is the only thing learned, it is one
  readable threshold, and it cannot cross the privacy wall or the stakes rule.
- **Re-routing after a failure** (6.10) is not automatic. A failed run is
  recorded; you decide what happens next.

## Mistakes to avoid (6.13): status

| Mistake | Status |
|---|---|
| Letting reliability erode the hard rule | No code path applies a high-stakes diff without `orchestra apply` |
| Inferring stakes from a prompt | Inference only raises stakes; lowering takes a declaration |
| Routing by capability instead of fit | Privacy and stakes first; capability only among what they allow |
| An unauditable router | `orchestra route` prints every axis, where it came from, and each reason |
| Ignoring the evidence | `report --by-class`, and the evidence rule |
