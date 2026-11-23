"""Codex CLI adapter.

Everything this project knows about Codex's command line is in this file.

Headless shape:  codex exec --json --sandbox <mode> "<brief>"
With --json, stdout is a stream of JSON lines, one event each. Events read:
  item.completed  with item.type == "agent_message"  -> the answer (last one wins)
  turn.completed  with usage{input_tokens, output_tokens} -> token counts
  turn.failed / error                                 -> failure reason

Access mapping:
  read -> --sandbox read-only        (Codex's own default for exec)
  edit -> --sandbox workspace-write  (may edit files AND run commands inside
          the working directory; no network). Note this is broader than
          Claude's edit level, which does not auto-approve shell commands.

Codex reports tokens, not dollars, so cost_usd is None (unknown) until
chapter 14 adds price tables.

Source: OpenAI's non-interactive mode docs, checked 2026-11-19.
Workspace-write overrides checked against the configuration reference and
observed on 2026-11-22: https://developers.openai.com/codex/config-reference/
Observed working 2026-11-19 on codex-cli 0.170.0 (answer, token counts, error
events, both sandbox levels). An older CLI (0.144.0) failed with "model not
supported": if that error appears, update Codex before suspecting anything else.
"""

from __future__ import annotations

import json
from typing import Optional

from ..contract import EDIT, ERROR, OK, READ, Result
from .base import Adapter


class CodexAdapter(Adapter):
    name = "codex"
    default_binary = "codex"
    binary_env = "ORCHESTRA_CODEX_BIN"
    version = "2026.11.22-1"
    sandboxed = True
    fit = ("unattended", "long_run")
    context_file = "AGENTS.md"
    auto_reads_context = True
    env = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")
    profile = {
        "backend": "OS sandbox on the host",
        "local_models": "limited",
        "safety_default": "sandboxed, read-only in exec",
        "context_file": "AGENTS.md",
        "edit_model": "edits the tree inside its sandbox, does not ask",
        "best_for": "long autonomous runs",
        "claim": "observed 2026-11-19 (codex-cli 0.170.0): read-only left the tree untouched; "
                 "workspace-write edited without committing and ran a command to check its work; "
                 "reports tokens but no dollar cost. On 2026-11-22 it passed 6/6 in /private/tmp "
                 "after explicit writable-root restrictions; the initial run passed 5/6.",
    }

    def delegate(self, brief: str, cwd: Optional[str] = None, timeout: int = 600,
                 access: str = READ) -> Result:
        cmd = [self.binary, "exec", "--json",
               "--sandbox", "workspace-write" if access == EDIT else "read-only"]
        if access == EDIT:
            # workspace-write also permits shared temporary directories by
            # default. A repo under one of them then loses its parent boundary.
            for setting in ("exclude_slash_tmp=true", "exclude_tmpdir_env_var=true",
                            "writable_roots=[]", "network_access=false"):
                cmd.extend(["-c", "sandbox_workspace_write." + setting])
        else:
            # Codex refuses to run outside a git repository unless told
            # otherwise. That guard matters for edits; for read-only it only
            # gets in the way.
            cmd.append("--skip-git-repo-check")

        proc, failure, elapsed = self._run(cmd, cwd, timeout, prompt=brief)
        if failure:
            return failure

        answer, usage, errors, completed, events = "", {}, [], False, 0
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            events += 1
            kind = event.get("type")
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                answer = item.get("text") or answer
            elif kind == "turn.completed":
                completed = True
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            elif kind in ("turn.failed", "error"):
                err = event.get("error")
                msg = err.get("message") if isinstance(err, dict) else event.get("message")
                msg = self._plain(msg or kind)
                if msg not in errors:  # Codex repeats the same failure as two events
                    errors.append(msg)

        if events == 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return Result(self.name, ERROR, exit_code=proc.returncode, duration_ms=elapsed,
                          error=f"unparseable reply: {detail or 'empty output'}")

        failed = proc.returncode != 0 or bool(errors) or not completed
        reason = None
        if failed:
            reason = "; ".join(errors)[:500] or (proc.stderr or "").strip()[-500:] \
                or f"run did not complete (exit code {proc.returncode})"
        tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
        return Result(
            agent=self.name,
            status=ERROR if failed else OK,
            result=answer,
            cost_usd=None,
            input_tokens=tin if isinstance(tin, int) else None,
            output_tokens=tout if isinstance(tout, int) else None,
            duration_ms=elapsed,
            exit_code=proc.returncode,
            error=reason,
        )

    @staticmethod
    def _plain(message) -> str:
        """Codex sometimes wraps the provider's error as a JSON string inside
        the message. Dig out the human sentence."""
        text = str(message)
        try:
            inner = json.loads(text)
            return str(inner["error"]["message"])
        except (ValueError, KeyError, TypeError):
            return text
