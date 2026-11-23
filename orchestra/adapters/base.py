"""Adapter interface: one subclass per execution agent (book 2.10, 3.10)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import List, Optional, Tuple

from .. import backends, config
from ..contract import ERROR, READ, TIMEOUT, Result


# What any command-line program needs to start, find its own login, and reach
# the network. Deliberately short. No API keys live here.
BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "ORCHESTRA_HOME",
)


def depth() -> int:
    try:
        return max(0, int(os.environ.get("ORCHESTRA_DEPTH", "0")))
    except ValueError:
        return 0


class Adapter:
    """Translate the control plane's one contract into one agent's dialect.

    Per-agent policy (timeout, validation of the returned structure, cost
    normalisation) lives here, not in the coordinator.
    """

    name = "base"
    default_binary = ""
    binary_env = ""  # env var that overrides the binary path (cron, tests)

    # --- what the router needs to know (book 3.7, 6.5, 10.2) ----------------
    location = "cloud"       # "cloud": the brief leaves this machine. "local": it does not.
    can_edit = True          # can it change files, or only answer?
    sandboxed = False        # does it contain its own commands (suits unattended runs)?
    needs_inline_files = False  # True if it has no tools and must be handed file contents
    fit = ()                 # routing keys from the capability matrix, best first

    version = "0"            # bump when this adapter's flags or parsing change
    context_file = ""        # the guidance file this agent is named for
    auto_reads_context = False  # True if the agent loads that file on its own
    # The only credentials and settings this agent may see (book 5.6). Names,
    # not values. config.yaml can add more per agent under agents.<name>.env.
    env: tuple = ()

    # The agent read on the axes of book 3.1 / table 3.7. `claim` follows the
    # book's three classes: official (docs), observed (we ran it), inferred.
    profile: dict = {}

    @property
    def binary(self) -> str:
        return os.environ.get(self.binary_env, self.default_binary)

    def available(self) -> bool:
        """True if the agent can be invoked on this machine."""
        return shutil.which(self.binary) is not None

    def delegate(self, brief: str, cwd: Optional[str] = None, timeout: int = 600,
                 access: str = READ) -> Result:
        """Run one unit of work headlessly. Must never raise; return a Result."""
        raise NotImplementedError

    def forwarded(self, cfg: Optional[dict] = None) -> List[str]:
        """Names of the agent-specific variables this adapter may forward."""
        extra = []
        if cfg:
            extra = ((cfg.get("agents") or {}).get(self.name) or {}).get("env") or []
        return list(dict.fromkeys(list(self.env) + [str(n) for n in extra]))

    def build_env(self, cfg: Optional[dict] = None) -> dict:
        """The stripped environment an agent process starts with: the basics
        any program needs, plus the variables this agent declares. Everything
        else the coordinator can see stays behind."""
        names = list(BASE_ENV) + self.forwarded(cfg)
        env = {n: os.environ[n] for n in names if n in os.environ}
        env.update({k: v for k, v in os.environ.items() if k.startswith("LC_")})
        # Recursion guard (book 5.11): the agent knows how deep it is, and
        # orchestra refuses to delegate past delegation.max_depth.
        env["ORCHESTRA_DEPTH"] = str(depth() + 1)
        return env

    def _run(self, cmd: List[str], cwd: Optional[str], timeout: int, prompt: Optional[str] = None
             ) -> Tuple[Optional[subprocess.CompletedProcess], Optional[Result], int]:
        """Run a CLI through the configured terminal backend. `prompt` is the
        brief, appended as the final argument. Returns (process, failure
        Result, elapsed ms); one of the first two is None. Launch problems
        become Results, never exceptions."""
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        def fail(status: str, msg: str):
            return None, Result(self.name, status, error=msg, duration_ms=elapsed()), elapsed()

        try:
            cfg = config.load()
            backend = backends.get(cfg)
            proc = backend.run(cmd, cwd, timeout, prompt=prompt, env=self.build_env(cfg))
        except config.ConfigError as e:
            return fail(ERROR, f"configuration: {e}")
        except backends.HardlineRefusal as e:
            return fail(ERROR, f"refused by the hardline floor: {e}")
        except FileNotFoundError:
            if cwd and not os.path.isdir(cwd):
                return fail(ERROR, f"working directory not found: {cwd}")
            return fail(ERROR, f"'{cmd[0]}' not found on PATH")
        except NotADirectoryError:
            return fail(ERROR, f"working directory not found: {cwd}")
        except subprocess.TimeoutExpired:
            return fail(TIMEOUT, f"no reply within {timeout}s")
        return proc, None, elapsed()
