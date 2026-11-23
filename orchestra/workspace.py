"""What did a run do to the working tree? Measured from git, not self-reported.

Book 3.1: a routable tool must leave evidence the coordinator can read. Agents
describe their own work loosely; git does not.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

# The "trio convention" (book 3.8, listing 3.2): which file each family reads.
CONTEXT_FILES = {
    "CLAUDE.md": "Claude Code",
    "AGENTS.md": "Codex / OpenCode",
    "GEMINI.md": "Gemini / Antigravity",
}


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def project_root(cwd: Optional[str]) -> Optional[str]:
    """The git repository containing cwd, or None if there is none."""
    top = _git(cwd or ".", "rev-parse", "--show-toplevel")
    return top.strip() if top and top.strip() else None


def snapshot(cwd: Optional[str]) -> Optional[dict]:
    cwd = cwd or "."
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return None
    head = _git(cwd, "rev-parse", "HEAD")  # None in a repo with no commits
    status = _git(cwd, "status", "--porcelain") or ""
    return {"head": head.strip() if head else None,
            "dirty": {line for line in status.splitlines() if line}}


def effects(cwd: Optional[str], before: Optional[dict]) -> Optional[dict]:
    """Compare the tree now with a snapshot taken before the run."""
    if before is None:
        return None
    cwd = cwd or "."
    after = snapshot(cwd)
    if after is None:
        return None
    commits, committed = 0, []
    if after["head"] and after["head"] != before["head"]:
        rng = f"{before['head']}..{after['head']}" if before["head"] else after["head"]
        commits = int((_git(cwd, "rev-list", "--count", rng) or "0").strip() or 0)
        if before["head"]:
            names = _git(cwd, "diff", "--name-only", before["head"], after["head"]) or ""
            committed = sorted(n for n in names.splitlines() if n)
    uncommitted = sorted(line[3:] for line in after["dirty"] - before["dirty"])
    return {"commits_added": commits, "committed_files": committed,
            "uncommitted_files": uncommitted}


def describe(eff: Optional[dict]) -> str:
    """One line on the posture a run showed: who gates the change, and how."""
    if eff is None:
        return "not a git repository, so changes were not measured"
    parts = []
    if eff["commits_added"]:
        parts.append(f"{eff['commits_added']} commit(s) added touching "
                     f"{len(eff['committed_files'])} file(s): already committed, "
                     f"you gate with git revert")
    if eff["uncommitted_files"]:
        parts.append(f"{len(eff['uncommitted_files'])} file(s) changed, uncommitted "
                     f"({', '.join(eff['uncommitted_files'][:5])}): you gate the commit")
    return "; ".join(parts) or "tree untouched: the agent answered or proposed only"


def isolated_copy(src: Optional[str], dest: Path) -> Path:
    """Give one agent its own copy of the project so runs cannot see or
    disturb each other, and the original is never touched."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src or ".", dest, symlinks=True)
    return dest


def context_files(cwd: Optional[str]) -> list:
    root = Path(cwd or ".")
    rows, texts = [], {}
    for name, reader in CONTEXT_FILES.items():
        path = root / name
        present = path.is_file()
        if present:
            texts[name] = path.read_text(encoding="utf-8", errors="replace")
        rows.append({"file": name, "reader": reader, "present": present})
    consistent = len(set(texts.values())) <= 1
    return [dict(r, consistent=consistent) for r in rows]
