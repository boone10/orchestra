"""MCP: registrations, and the memory server (book chapter 9).

Two things live here.

1. Registration records (9.4). A server is not "registered" because something
   can connect to it; it is registered when a reader can tell what it is, how
   it starts, which credential it receives, what it may read and write, who
   may use it, who owns it, and when that was last reviewed. `orchestra mcp
   list` prints the records and says what any of them is missing.

2. The memory server (9.13): this control plane's memory offered as tools, so
   an agent can look things up instead of relying only on its brief. Run it
   with `orchestra mcp serve memory`; it speaks MCP over stdio (one JSON-RPC
   message per line).

   Read tools, free to grant:
     search_outcomes         what was asked before, by whom, and how it ended
     get_curated_fact        lessons a person has reviewed
     list_known_procedures   names and summaries of trusted skills (no bodies)
   Write tool, narrow by construction:
     propose_memory_update   creates a PROPOSED record. It is not used in any
                             brief until you accept it. There is no tool that
                             edits curated memory or the core files.

   Reads disperse, writes converge (9.10): the only write lands in the review
   queue the coordinator already governs.

What is not built: a client. The coordinator cannot yet call other servers'
tools, so "tool" skills are declared but not runnable, and orchestra does not
wire this server into Claude Code or Codex for you. See docs/chapter-09.md.

Protocol shapes follow the MCP specification (lifecycle and tools), checked
2026-11-20. Not yet observed against a real MCP client from this project.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from . import __version__, config, memory, outcomes, skills

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 300
MAX_PROPOSALS = 20  # per server process: a runaway agent cannot flood the review queue

MEMORY_SERVER = {
    "name": "orchestra-memory",
    "purpose": "Let agents read this control plane's outcomes, reviewed lessons and skill "
               "list, and propose (never write) new lessons.",
    "transport": "stdio",
    "command": "orchestra mcp serve memory",
    "credentials": "none",
    "tools": [
        {"name": "search_outcomes", "access": "read", "gated": False},
        {"name": "get_curated_fact", "access": "read", "gated": False},
        {"name": "list_known_procedures", "access": "read", "gated": False},
        {"name": "propose_memory_update", "access": "write", "gated": True,
         "note": "creates a proposal for operator review; changes nothing an agent will be briefed with"},
    ],
    "exposed_to_agents": "all four tools",
    "owner": "built in",
    "reviewed": "2026-11-20",
}
REQUIRED = ("name", "purpose", "transport", "command", "credentials", "tools", "owner", "reviewed")

TOOLS = [
    {"name": "search_outcomes",
     "description": "Search earlier delegated tasks recorded by this control plane. Returns the "
                    "task as asked, the agent, the date, the status and the task class. Read-only.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"],
                     "properties": {"query": {"type": "string", "maxLength": 200},
                                    "project": {"type": "string", "maxLength": 400},
                                    "task_class": {"type": "string", "maxLength": 60},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}}}},
    {"name": "get_curated_fact",
     "description": "Look up lessons a human operator has reviewed and accepted, by topic. "
                    "Returns each with its source, confidence and date. Read-only.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["topic"],
                     "properties": {"topic": {"type": "string", "maxLength": 200},
                                    "project": {"type": "string", "maxLength": 400}}}},
    {"name": "list_known_procedures",
     "description": "List the names and one-line summaries of trusted skills. Read-only.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "propose_memory_update",
     "description": "Propose a lesson for the operator to review. Creates a proposal only: it "
                    "does not change memory and is not used until a human accepts it.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["summary"],
                     "properties": {"summary": {"type": "string", "maxLength": memory.MAX_SUMMARY},
                                    "type": {"type": "string", "enum": list(memory.TYPES)},
                                    "project": {"type": "string", "maxLength": 400},
                                    "evidence": {"type": "string", "maxLength": 80}}}},
]


class ToolError(Exception):
    pass


def _str(args: dict, key: str, limit: int, required: bool = False) -> Optional[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' must be a non-empty string")
    if len(value) > limit:
        raise ToolError(f"'{key}' is longer than {limit} characters")
    return value


def _short(text, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def search_outcomes(args: dict) -> list:
    query = _str(args, "query", 200, required=True)
    project, task_class = _str(args, "project", 400), _str(args, "task_class", 60)
    limit = args.get("limit", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ToolError("'limit' must be a whole number from 1 to 20")
    want, found = memory.tokens(query), []
    for e in outcomes.read_all():
        # Tasks over sensitive data are not described to whoever is asking:
        # the caller may be a cloud agent.
        if e.get("privacy") in ("P3", "P4") or str(e.get("brief", "")).startswith("[withheld"):
            continue
        if project and e.get("project") != project:
            continue
        if task_class and e.get("task_class") != task_class:
            continue
        score = memory.overlap(want, memory.tokens(str(e.get("brief") or "")))
        if score:
            found.append((score, e.get("ts", ""), e))
    found.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [{"date": e.get("ts", "")[:10], "task_id": e.get("task_id"), "agent": e.get("agent"),
             "status": e.get("status"), "task_class": e.get("task_class"),
             "task": _short(e.get("brief"))} for _, _, e in found[:limit]]


def get_curated_fact(args: dict) -> list:
    topic, project = _str(args, "topic", 200, required=True), _str(args, "project", 400)
    want, out = memory.tokens(topic), []
    scopes = {"global"} | ({f"project:{project}"} if project else set())
    for rec in memory.load():
        if rec["state"] != "curated" or rec["scope"] not in scopes:
            continue
        if memory.overlap(want, memory.tokens(rec["summary"])):
            out.append({"id": rec["id"], "summary": _short(rec["summary"], memory.MAX_SUMMARY),
                        "type": rec["type"], "source": rec["source"],
                        "confidence": rec["confidence"], "review_state": rec["state"],
                        "date": rec.get("ts", "")[:10]})
    return out[:10]


def list_known_procedures(args: dict) -> list:
    return [{"name": s["name"], "category": s["category"], "summary": _short(s["description"])}
            for s in skills.summaries() if s["status"] == skills.TRUSTED]


_proposals = {"count": 0}


def propose_memory_update(args: dict) -> dict:
    if _proposals["count"] >= MAX_PROPOSALS:
        raise ToolError("this session has reached its limit of proposals")
    summary = _str(args, "summary", memory.MAX_SUMMARY, required=True)
    kind = args.get("type", "fact")
    project = _str(args, "project", 400)
    try:
        rec = memory.add(summary, scope=memory.scope_for(project=project, everywhere=not project),
                         type=kind, source="agent:mcp", confidence="inferred", state_="proposed",
                         evidence=_str(args, "evidence", 80))
    except memory.MemoryError_ as e:
        raise ToolError(str(e)) from None
    _proposals["count"] += 1
    return {"id": rec["id"], "review_state": "proposed",
            "note": "Recorded as a proposal. It is not in use; the operator decides."}


HANDLERS = {"search_outcomes": search_outcomes, "get_curated_fact": get_curated_fact,
            "list_known_procedures": list_known_procedures,
            "propose_memory_update": propose_memory_update}


def handle(msg: dict) -> Optional[dict]:
    """One JSON-RPC message in, one reply out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # notifications (such as notifications/initialized) get no reply

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "orchestra-memory", "version": __version__}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in HANDLERS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        allowed = set(next(t for t in TOOLS if t["name"] == name)["inputSchema"]["properties"])
        try:
            extra = set(args) - allowed
            if extra:
                raise ToolError("unexpected argument(s): " + ", ".join(sorted(extra)))
            payload = HANDLERS[name](args)
            return ok({"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                       "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # never let one bad call take the server down
            return ok({"content": [{"type": "text", "text": f"internal error: {type(e).__name__}"}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def registrations() -> list:
    """The built-in memory server plus whatever config.yaml declares, each
    with a list of what its record is missing."""
    servers = [dict(MEMORY_SERVER)]
    for entry in config.load()["mcp"].get("servers") or []:
        if isinstance(entry, dict):
            servers.append(dict(entry))
    for s in servers:
        s["missing"] = [k for k in REQUIRED if not s.get(k)]
        for tool in s.get("tools") or []:
            if isinstance(tool, dict) and tool.get("access") == "write" and not tool.get("gated"):
                s["missing"].append(f"tool {tool.get('name')} writes but is not gated")
    return servers
