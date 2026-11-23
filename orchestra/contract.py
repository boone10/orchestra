"""The stable, vendor-neutral contract every adapter returns (book 2.10).

The control plane only ever sees a Result. Vendor flags, JSON field names and
exit-code quirks stay inside the adapter that produced it. Later chapters widen
this dataclass (briefs, memory write-back, cost accounting) in one place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

OK = "ok"
ERROR = "error"
TIMEOUT = "timeout"
DENIED = "denied"  # a human (or the approval mode) refused the run; no agent was launched
ESCALATED = "escalated"  # the router found no agent that may do this; it is a human's to do

STAKES = ("low", "med", "high")
PRIVACY = ("P0", "P1", "P2", "P3", "P4")   # public .. must never leave the machine
CONTEXT = ("small", "large")
AUTONOMY = ("supervised", "unattended")

# What a run is allowed to do to the working tree. Each adapter maps these two
# words onto its own vendor's permission or sandbox flags.
READ = "read"   # look and answer; change nothing
EDIT = "edit"   # may change files inside the working directory
ACCESS_LEVELS = (READ, EDIT)


@dataclass
class Result:
    agent: str
    status: str  # OK | ERROR | TIMEOUT | DENIED | ESCALATED
    result: str = ""
    # None means "unknown", which is different from 0. Never silently absent.
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # What the run left behind in the working tree, measured by the control
    # plane from git (not self-reported by the agent). None = not a git repo.
    effects: Optional[dict] = None
    # Filled in by the control plane, not by adapters (book 5.4, 5.5):
    task_id: Optional[str] = None
    problems: Optional[list] = None   # validation failures; empty or None = valid
    gate: Optional[str] = None        # not_required | pending | applied | auto_applied |
                                      # nothing_to_review | blocked | direct
    artifacts: Optional[str] = None   # folder holding the brief, result and any diff
    route: Optional[dict] = None      # the router's decision and its reasons (book 6.10)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return asdict(self)
