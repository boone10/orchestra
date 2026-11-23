# Chapter 9: MCP, the Shared Tool Layer

Two things are built: registration records, and one server.

## Book to code

| Book | Idea | Here |
|---|---|---|
| 9.4 | A registration answers: what, how started, which credential, what it exposes, who may use it | `mcp.servers` in config; `orchestra mcp list` flags what a record is missing |
| 9.6 | Read tools free, write tools gated, narrow names and schemas | each tool declares `access` and `gated`; an ungated write is flagged |
| 9.9 | Validate what comes back and what goes in | strict argument checks, size limits, tool errors instead of crashes |
| 9.10, 9.11 | Reads disperse, writes converge on one seam | the only write tool creates a proposal for you to review |
| 9.13 | A memory server | `orchestra mcp serve memory` |

## The memory server

    orchestra mcp serve memory      # MCP over stdio, one JSON-RPC message per line

| Tool | Access | Returns |
|---|---|---|
| `search_outcomes` | read | earlier tasks: what was asked, agent, date, status, class |
| `get_curated_fact` | read | lessons you have accepted, with source and confidence |
| `list_known_procedures` | read | names and summaries of trusted skills, not their bodies |
| `propose_memory_update` | write, gated | creates a **proposed** record; nothing uses it until you accept it |

What it will not do: return what an agent said in an earlier run; describe
tasks tagged P3 or P4 (the caller may be a cloud agent); edit curated memory
or the core files (there is no such tool); accept more than 20 proposals per
session.

## Registering other servers

Add a record under `mcp.servers` in `config.yaml`:

    mcp:
      servers:
        - name: tracker
          purpose: Read and comment on issues
          transport: stdio
          command: tracker-mcp
          credentials: TRACKER_READ_TOKEN     # a name, never the value
          owner: mike
          reviewed: 2026-11-20
          tools:
            - {name: list_issues, access: read, gated: false}
            - {name: close_issue, access: write, gated: true}

`orchestra mcp list` prints every record and says `NOT FULLY REGISTERED` when
one cannot answer the questions above, or declares a write that is not gated.
It is a map of what the fleet can reach, kept as deliberately as the agent
profiles.

## What is not built, plainly

- **No MCP client.** The coordinator is rule-based and has no loop that calls
  tools, so it cannot call any server, and `tool` skills are declared but not
  runnable. The registration records are documentation that orchestra checks
  for completeness, not connections it manages.
- **Not wired into delegated runs.** Orchestra launches Claude with every MCP
  tool removed and its machine settings ignored, for the containment reasons
  found by the bench. So agents run by orchestra do not see the memory server
  today. Enabling specific read tools for delegated runs is a deliberate next
  step, and should go through the bench again when it happens.
- **Using it yourself.** You can register the server in your own interactive
  Claude Code or Codex sessions with each tool's `mcp add` command, pointing
  it at `orchestra mcp serve memory`. Check the current syntax in their
  documentation; I have not verified it from here.
- **Not observed with a real client.** The server follows the MCP
  specification (checked 2026-11-20) and is tested by speaking the protocol to
  it directly. No real MCP client has connected to it yet.
- Outages, mixed protocol versions and HTTP transport (9.16, 9.17).
