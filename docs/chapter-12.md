# Chapter 12 · Scheduling and the work queue

The daemon can schedule recurring work and run several queued reads on one
Mac. The default is one worker with scheduling disabled. Existing request
files keep their IDs, and an existing daemon needs a restart to pick up
the new code.

## Run a daily link check

In `~/.orchestra/config.yaml`, set the existing `scheduler.enabled` value to
`true`. To allow two concurrent reads, set `daemon.workers` to `2`. The
corresponding sections look like this.

```yaml
scheduler:
  enabled: true
daemon:
  workers: 2
  poll_seconds: 2
```

Start the local Docker engine and the foreground daemon in a terminal.

```sh
colima start --profile orchestra --activate=false
orchestra daemon
```

In a second terminal, register the trusted link checker for 9 a.m. Louisville
time. Use a project directory that exists on your Mac.

```sh
orchestra schedule add docs "Check Markdown links" --cwd ~/throwaway \
  --skill check-doc-links --daily 09:00 --timezone America/Kentucky/Louisville
orchestra schedule list
orchestra requests
```

For an interval, use `--every 1800` to run every thirty minutes. The first
occurrence is in the future. Daily schedules default to UTC. A daily time
skipped by a spring clock change skips that day. A repeated fall time fires
at its first occurrence.

`orchestra schedule pause docs` pauses that producer. `orchestra schedule
resume docs` enables it again. `orchestra schedule remove docs` retires the
schedule and preserves its request history. Already queued jobs have their
own lifecycle. Use `orchestra cancel <id>` to withdraw a pending request.
Use `orchestra stop` to terminate active native skills for the entire home.

## What happens when a run is due

Each schedule stores its next due time under `ORCHESTRA_HOME/cron/`. The
daemon submits a request through the ordinary queue, which still applies
skill trust, project privacy, budgets, and supervision rules. A named cloud
agent requires human supervision on this Mac. A request routed to it from
the daemon stays visible as `needs_human`.

- **Downtime and overlap.** Missed occurrences become one run against the
  current project. A previous request still pending, running, in review, or
  waiting for a human blocks overlapping occurrences of that schedule.
  After a daemon crash, a potentially orphaned native run also holds the
  next occurrence while registrations from that Orchestra home remain.
  `orchestra stop` followed by `orchestra resume` confirms cleanup.
- **Duplicate submissions.** A scheduled occurrence has a stable key. If the
  producer dies after enqueueing and before saving its next due time, it can
  recover the existing request. Manual producers can use
  `orchestra submit "Check Markdown links" --idempotency-key docs-revision-42`.
  The same key and inputs return the original request ID, including after
  completion. Reusing the key with different inputs produces an error.
- **Worker capacity.** One daemon owns the spool per Orchestra home. It can
  run one to eight reads concurrently. A pending edit waits for earlier
  reads, then runs alone. Workers claim work under a file lock. Each claim
  has a unique owner and only that owner can update its lease or finish it.
  A running claim gets a heartbeat every ten seconds and a 45-second lease.
- **Crash recovery.** The new daemon acquires the previous daemon's lock
  before recovering its claims. The ordinary outcome for an abandoned
  request is `interrupted`. A read submitted with `--replay-safe` can get
  one automatic crash retry. Use it only when repeating the whole task is
  safe. An intervening operator stop or an outstanding native container
  registration blocks automatic replay.
- **Human decisions.** `orchestra requests` keeps escalations visible.
  `orchestra resolve <id> --reason "Handled in person"` records the decision
  and closes the escalation. For edits, `orchestra review`, `apply`, and
  `discard` still govern the patch. `orchestra trace <id>` prints the
  request's audit events in order.

## Coverage and verification

The implementation covers the local time and interval triggers, atomic
claims, reporting and traces, human gates, deduplication, and coalesced
catch-up in §§12.1–12.10 of *Orchestrating AI Agents* (pp. 178–189).
The queue is scoped to one Mac. Multi-host workers, external event triggers,
priority queues, and backfill that processes every missed interval remain
outside this implementation.

In disposable state on the Mac, three missed intervals produced one real
Docker link check. It reported one broken link out of two. Three identical
manual submissions shared one request ID, including after a human resolved
the escalation. With two workers, a second Docker skill started and finished
while the first was still running. Both left distinct task receipts and
outcomes, and the engine had no remaining fixture containers.

Killing the daemon during a scheduled 45-second Docker skill left its
container running. Two subsequent due intervals produced no second request
or container. After scoped stop and resume, the next slot ran once and the
original request stayed interrupted.

A separate process test used a loopback stand-in for Ollama and killed the
daemon after requests reached the server. An opted-in read finished on its
second claim. A normal read stayed interrupted after one call. Killing the
daemon during a second opted-in job's retry left that job interrupted after
two calls. Two further restarts produced no additional calls. The trace
linked each attempt to the original request ID.
