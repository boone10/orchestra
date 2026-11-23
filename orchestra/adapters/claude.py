"""Claude Code adapter.

Everything this project knows about Claude Code's command line is in this
file. If a flag is renamed or an output field moves, this is the only edit.

Headless shape:
  claude --print --restricted --tools <list> --disallowedTools "mcp__*" \
         --permission-mode <mode> --output-format json "<brief>"
Fields read from the JSON reply: result, subtype, is_error, total_cost_usd,
duration_ms, usage.

Access mapping. Both levels are stated explicitly on every run, because what a
bare `claude --print` may do depends on the settings of the machine it runs
on. Observed 2026-11-19: with no flags, a headless run edited a file on a
machine whose own settings allowed edits.
  read -> --tools "Read,Glob,Grep" --permission-mode default
          The editing and command tools are removed outright, so there is
          nothing to approve or deny.
  edit -> --tools "Read,Glob,Grep,Edit,Write" --permission-mode acceptEdits
          File edits in the working directory, no shell commands.
MCP tools are removed at both levels: they are the operator's personal
integrations and can do anything.

--restricted (Claude Code v2.1.248 or later) is the vendor's mode for a
harness driving `claude`: it ignores the machine's user and project settings,
confines the file tools to the working directory, and refuses the
bypass-permissions mode. Added after the bench observed (2026-11-19) an
edit-level run writing ../bench_escape.txt, outside its working directory.
On an older Claude Code the flag is rejected and every run fails with an
"unknown option" error: update Claude Code.

Flag order matters: --disallowedTools takes a list of values, so it must not
be the last flag before the brief or it would swallow the brief.

Source: Claude Code CLI reference, checked 2026-11-19. Observed working
2026-11-19: both tool lists, the JSON reply shape, and error reporting.
"""

from __future__ import annotations

import json
from typing import Optional

from ..contract import EDIT, ERROR, OK, READ, Result
from .base import Adapter


class ClaudeAdapter(Adapter):
    name = "claude"
    default_binary = "claude"
    binary_env = "ORCHESTRA_CLAUDE_BIN"
    version = "2026.11.19-3"
    fit = ("large_context", "supervised")
    context_file = "CLAUDE.md"
    # Under --restricted, Claude Code ignores project settings; whether it still
    # loads CLAUDE.md is not something we have observed, so orchestra puts the
    # guidance in the brief itself.
    auto_reads_context = False
    env = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR")
    profile = {
        "backend": "host shell",
        "local_models": "no",
        "safety_default": "depends on the machine's own settings; do not rely on it",
        "context_file": "CLAUDE.md",
        "edit_model": "edits the tree, never commits on its own",
        "best_for": "hard, supervised work",
        "claim": "book table 3.1 says 'asks'; observed 2026-11-19 editing without asking "
                 "when launched with no flags, so orchestra states the tool list itself. "
                 "With explicit lists: read left the tree untouched, edit changed files "
                 "without committing but also wrote outside its folder, so runs now use "
                 "--restricted. On 2026-11-22 Claude Code 2.1.329 passed all six probes.",
    }

    def delegate(self, brief: str, cwd: Optional[str] = None, timeout: int = 600,
                 access: str = READ) -> Result:
        if access == EDIT:
            tools, mode = "Read,Glob,Grep,Edit,Write", "acceptEdits"
        else:
            tools, mode = "Read,Glob,Grep", "default"
        cmd = [self.binary, "--print",
               "--restricted",
               "--tools", tools,
               "--disallowedTools", "mcp__*",
               "--permission-mode", mode,
               "--output-format", "json"]  # keep a single-value flag last

        proc, failure, elapsed = self._run(cmd, cwd, timeout, prompt=brief)
        if failure:
            return failure

        try:
            data = json.loads(proc.stdout)
            if not isinstance(data, dict):
                raise ValueError("reply is not a JSON object")
        except ValueError:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return Result(self.name, ERROR, exit_code=proc.returncode, duration_ms=elapsed,
                          error=f"unparseable reply: {detail or 'empty output'}")

        failed = proc.returncode != 0 or bool(data.get("is_error")) \
            or data.get("subtype", "success") != "success"
        cost = data.get("total_cost_usd")
        duration = data.get("duration_ms")
        text = data.get("result") or ""
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        in_parts = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                           "cache_creation_input_tokens")]
        in_parts = [v for v in in_parts if isinstance(v, int)]
        out = usage.get("output_tokens")
        return Result(
            agent=self.name,
            status=ERROR if failed else OK,
            result=text,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            input_tokens=sum(in_parts) if in_parts else None,
            output_tokens=out if isinstance(out, int) else None,
            duration_ms=int(duration) if isinstance(duration, (int, float)) else elapsed,
            exit_code=proc.returncode,
            error=self._describe_failure(data, text, proc) if failed else None,
        )

    @staticmethod
    def _describe_failure(data: dict, text: str, proc) -> str:
        # Claude Code can report is_error=true with subtype "success" (for
        # example an API rate-limit refusal), so the subtype alone is not a
        # usable reason. Prefer a non-success subtype, then the agent's text.
        subtype = data.get("subtype")
        if subtype and subtype != "success":
            return f"{subtype}: {text[:500]}" if text else subtype
        return text[:500] or (proc.stderr or "").strip()[:500] \
            or f"agent reported an error (exit code {proc.returncode})"
