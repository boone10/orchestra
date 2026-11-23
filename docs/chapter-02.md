# Chapter 2: Orchestrate, Don't Replace

## Book to code

| Book | Idea | Here |
|---|---|---|
| 2.3, Listing 2.1 | Delegate one task to a headless agent, parse a structured result | `adapters/claude.py`, `orchestra delegate` |
| 2.8, Listing 2.2 | Record a durable outcome after every run (seed of memory) | `outcomes.record()`, called from `control.delegate()` |
| 2.8, Listing 2.3 | Read the record back (evidence for a future router) | `outcomes.summarize()`, `orchestra report` |
| 2.10, Listing 2.4 | One thin adapter per agent behind a stable contract | `contract.Result`, `adapters/` |
| 2.8 (end) | Scheduling is "a single cron entry away" | Example below; not installed |
| 2.7 | Four roles: memory, orchestrator/scheduler, registry, router | `outcomes.py`, `control.py`, `adapters.REGISTRY`; router not built yet |

## Where this differs from the listings, and why

- **Python instead of shell**, so later chapters extend it without a rewrite.
- **JSON lines instead of tab-separated text** for the outcomes log. Briefs
  contain tabs and newlines; JSON survives them. (The book's `date --iso-8601`
  is also GNU-only and fails on macOS.)
- **The contract has more than three fields**: `duration_ms`, `exit_code` and
  `error` are added, following the note in 3.1 that a routable tool must report
  enough to decide the next step. Unknown cost is `null`, never a silent `0`.
- **Failures are outcomes, not exceptions.** Missing binary, timeout, non-zero
  exit and unparseable output all come back as a `Result` and are logged. The
  shell listings would abort before writing the log line.
- **Recording is not optional.** `control.delegate()` is the only path to an
  agent and always writes the outcome (2.13, "delegating and forgetting").

## The scheduling seed

Not installed. Read "Where it runs" first. To try it, `crontab -e` and add:

    # Weekdays at 07:30: one standing delegation
    30 7 * * 1-5 cd /Users/mike.boone/_source/orchestra && ORCHESTRA_CLAUDE_BIN=/absolute/path/to/claude /usr/bin/python3 -m orchestra delegate --cwd /path/to/some/repo "Summarize yesterday's commits and list anything that looks risky" >> "$HOME/.orchestra/cron.log" 2>&1

cron runs with a minimal PATH, so give the absolute path to `claude`
(`which claude` prints it). On macOS the Mac must be awake at that time.

## Where it runs (2.11)

Fine on your workstation while you are the one pressing Enter. Before anything
runs on a schedule, the book's position is that the coordinator gets its own
isolated home with narrow access (chapter 11). Two things currently limit the
reach of a run:

- Headless `claude --print` is invoked with no permission flags, so the agent
  cannot run tools that need approval. That makes it read-and-answer only, which is
  the right default for now. Do not add a skip-permissions flag to the adapter
  until chapter 11's guardrails exist.
- `--timeout` bounds each run. There is no spend cap yet (chapter 14).

## Mistakes to avoid (2.13): status

| Mistake | Status |
|---|---|
| Building one agent to rule them all | Avoided: no model calls here, only dispatch |
| Scraping interactive output | Avoided: structured JSON only; unparseable output is an error |
| Delegating without a brief | **Open.** The prompt is forwarded as-is. Chapter 5 |
| Delegating and forgetting | Avoided by construction |
| Letting autonomy outrun stakes | Partly: read-only by default; no routing on stakes yet. Chapter 6 |
| Welding to one vendor's flags | Avoided: flags live only in `adapters/claude.py` |

## Not built yet

Briefs and context assembly (5), router (6), real memory (7), skills (8),
MCP (9), local models (10), sandboxing (11), work queue (12), budgets (14).
`control.delegate()` has TODO markers at the two seams chapters 5 and 6 plug into.

2.12 (planes operating planes) needs no code now. The one thing to preserve is
that `orchestra delegate --json` itself returns the contract, so a higher-level
coordinator could later treat this one as just another adapter.

## Adding an agent

Subclass `Adapter` in `adapters/<name>.py`, return a `Result` for every
outcome including failures, add it to `REGISTRY`. Nothing else changes.
