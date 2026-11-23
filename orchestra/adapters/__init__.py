"""The capability registry, in embryo: the agents this control plane can run.

Onboarding a new agent (book 3.10) = read it on the axes, write one Adapter
subclass with a profile, add it here, bench it on your own work.
"""

from __future__ import annotations

from .base import Adapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .local import LocalAdapter

REGISTRY = {a.name: a for a in (ClaudeAdapter(), CodexAdapter(), LocalAdapter())}
DEFAULT_AGENT = "claude"


def get(name: str) -> Adapter:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown agent '{name}' (known: {known})") from None
