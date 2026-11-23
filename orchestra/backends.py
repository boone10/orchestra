"""The terminal backend: where dispatched commands actually run (book 4.5).

Every command the coordinator launches goes through a backend, chosen by
terminal.backend in config. Only "local" exists today, but because adapters
never call subprocess themselves, a container or remote backend can be added
here without touching them.

Under every backend sits the hardline floor (book 4.6): patterns refused no
matter what the configuration says. It is a backstop against catastrophe, not
the safety strategy. The approval mode and (later) the sandbox do that work.
"""

from __future__ import annotations

import re
import subprocess
from typing import List, Optional

HARDLINE = [
    # Launching an agent with its own safety switched off.
    (r"--dangerously-skip-permissions", "agent launched with permissions disabled"),
    (r"--dangerously-bypass-approvals-and-sandbox", "agent launched with sandbox disabled"),
    (r"danger-full-access", "agent launched with full-access sandbox"),
    (r"bypassPermissions", "agent launched with permissions bypassed"),
    # Classic catastrophes. These matter once the coordinator runs commands a
    # model chose; today it only launches agent CLIs.
    (r"\brm\s+-[a-zA-Z]*([rR][a-zA-Z]*f|f[a-zA-Z]*[rR])[a-zA-Z]*\s+(/|~|\$HOME)/?(\s|$)",
     "recursive delete of root or home"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b.*\bof=/dev/(sd|disk|nvme|hd)", "raw write to a disk device"),
]
_HARDLINE = [(re.compile(p), why) for p, why in HARDLINE]


class HardlineRefusal(Exception):
    pass


def check_floor(cmd: List[str]) -> None:
    line = " ".join(cmd)
    for pattern, why in _HARDLINE:
        if pattern.search(line):
            raise HardlineRefusal(why)


class LocalBackend:
    """Run on this machine's shell: no isolation. A bad command reaches
    everything your user can reach. Acceptable while a human starts each run."""

    name = "local"
    isolated = False

    def run(self, cmd: List[str], cwd: Optional[str], timeout: int,
            prompt: Optional[str] = None, env: Optional[dict] = None
            ) -> subprocess.CompletedProcess:
        # The floor inspects the command, not the prompt: a brief that merely
        # mentions a dangerous command is data, not something we execute.
        check_floor(cmd)
        argv = cmd + ([prompt] if prompt is not None else [])
        # env=None would inherit everything the coordinator can see. Adapters
        # always pass the stripped environment they built (book 5.6).
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def get(cfg: dict):
    # config.load() has already rejected backends that are not built.
    return LocalBackend()
