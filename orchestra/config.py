"""Configuration and secrets (book 4.8).

Two files, deliberately separate:
  config.yaml  non-secret settings you can read, review and version
  .env         secrets (KEY=VALUE), chmod 600, never placed in a brief

config.yaml may refer to a secret by name with ${NAME}; the value is
substituted at load time and never written back.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from .contract import ACCESS_LEVELS, PRIVACY

APPROVAL_MODES = ("manual", "smart", "off")
BACKENDS_BUILT = ("local",)
BACKENDS_PLANNED = ("docker", "ssh", "sandbox")

DEFAULTS = {
    "model": {"provider": None, "name": None},
    "auxiliary": {},
    "terminal": {"backend": "local"},
    "approvals": {"mode": "manual"},
    "defaults": {"agent": "claude", "access": "read", "timeout": 600},
    "memory": {"max_chars": 4000},
    "brief": {"max_chars": 12000, "guidance_max_chars": 6000, "memory_items": 3},
    "delegation": {"retries": 1, "max_depth": 1},
    "agents": {},
    "routing": {"untagged_privacy": "P3", "approved_providers": [], "p3_exceptions": [],
                "min_evidence": 5},
    "local": {"base_url": "http://127.0.0.1:11434", "model": None, "allow_remote": False},
    "budgets": {"global_monthly_usd": None, "classes": []},
    "skills": {"match_threshold": 2},
    "mcp": {"servers": []},
    "daemon": {"poll_seconds": 2},
    "gateways": {"cli": True},
    "scheduler": {"enabled": False},
}

CONFIG_TEMPLATE = """\
# orchestra control-plane configuration. Non-secret only.
# Secrets live in the .env file next to this one; refer to them as ${NAME}.

# Reserved. The coordinator is rule-based and makes no model calls of its own
# yet; chapter 6 (routing) is where these start to matter.
model:
  provider: null
  name: null
auxiliary: {}

# Where the coordinator runs the commands it dispatches. Only "local" (this
# machine's shell) is built. docker / ssh / sandbox arrive with chapter 11.
terminal:
  backend: local

# Edit-level runs never touch your project directly. The agent works in a
# private copy and produces a diff.
# manual: the diff waits for you: `orchestra review`, then `orchestra apply <id>`.
# off:    a diff that passes validation is applied automatically. Use only for
#         low-stakes, reversible work.
# smart:  needs an auxiliary model to classify risk; treated as manual for now.
approvals:
  mode: manual

defaults:
  access: read      # read | edit
  timeout: 600      # seconds per run

# SOUL.md + MEMORY.md + USER.md are sent with every brief, so keep them small.
memory:
  max_chars: 4000

# What goes into a brief, and how much (chapter 5).
brief:
  max_chars: 12000           # warn above this; every character is paid for
  guidance_max_chars: 6000   # cap on the project guidance file
  memory_items: 3            # past outcomes from the same project to include

delegation:
  retries: 1      # read-only runs are retried this many times on a timeout or
                  # unreadable reply. Edit runs are never retried automatically.
  max_depth: 1    # an agent started by orchestra may not start further agents

# Agents get a stripped environment: basics plus the few variables their
# adapter declares. To forward one more to a specific agent, name it here.
# Anything forwarded can leave the machine.
# agents:
#   claude:
#     env: [AWS_PROFILE]
agents: {}

# How the router decides (chapters 6 and 10). Per-project privacy and stakes
# are declared with `orchestra project set`, not here.
routing:
  untagged_privacy: P3      # a task with no declared privacy class is treated as
                            # sensitive: it may not go to a cloud agent
  approved_providers: []    # cloud agents allowed to receive P2 (confidential)
                            # work, e.g. [claude]. Empty = P2 stays local
  p3_exceptions: []         # deliberate, named exceptions letting P3 work reach a
                            # cloud agent, e.g.
                            #   - {task_class: support-triage, agent: claude,
                            #      approved_by: mike, reason: "DPA signed 2026-11"}
                            # P4 has no exceptions.
  min_evidence: 5           # runs of a task class before its record sways the choice of agent

# The local model used for "act direct" work. Must be on this machine.
local:
  base_url: http://127.0.0.1:11434   # Ollama's default
  model: null                        # null = use the first model Ollama lists

# Spending caps by task class, counted from the outcome log for the calendar
# month. Only runs that report a dollar cost are counted (Codex does not).
budgets:
  global_monthly_usd: null
  classes: []
  #  - {task_class: research, monthly_usd: 40, on_exhaustion: refuse}
  #  - {task_class: refactor, monthly_usd: 25, on_exhaustion: downgrade_to_local}

skills:
  match_threshold: 2        # how strongly a task must match a skill's summary

# External tool servers this control plane knows about (chapter 9). A record,
# not a connection: what it is, how it starts, what it may read and write.
mcp:
  servers: []

daemon:
  poll_seconds: 2

gateways:
  cli: true         # the requests/ spool, written by `orchestra submit`

# Not built. Unattended scheduling is the last capability to earn (chapter 12).
scheduler:
  enabled: false
"""


class ConfigError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("ORCHESTRA_HOME", "~/.orchestra")).expanduser()


def load_secrets() -> dict:
    path = home() / ".env"
    secrets = {}
    if not path.is_file():
        return secrets
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        secrets[key] = value.strip().strip("'\"")
    return secrets


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute(value, secrets: dict):
    if isinstance(value, dict):
        return {k: _substitute(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, secrets) for v in value]
    if isinstance(value, str):
        def repl(match):
            name = match.group(1)
            if name not in secrets:
                raise ConfigError(f"config.yaml refers to ${{{name}}} but .env does not define it")
            return secrets[name]
        return _VAR.sub(repl, value)
    return value


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = home() / "config.yaml"
    if path.is_file():
        try:
            import yaml
        except ImportError:
            raise ConfigError("reading config.yaml needs PyYAML: pip install pyyaml") from None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path} is not valid YAML: {e}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a mapping of settings")
        _merge(cfg, data)
    cfg = _substitute(cfg, load_secrets())

    mode = cfg["approvals"].get("mode")
    if mode is False:
        # YAML reads a bare `off` as the boolean false. Accept what was meant.
        mode = cfg["approvals"]["mode"] = "off"
    if mode not in APPROVAL_MODES:
        raise ConfigError(f"approvals.mode must be one of {APPROVAL_MODES}, not {mode!r}")
    backend = cfg["terminal"].get("backend")
    if backend in BACKENDS_PLANNED:
        raise ConfigError(f"terminal.backend '{backend}' is not built yet (chapter 11); use 'local'")
    if backend not in BACKENDS_BUILT:
        raise ConfigError(f"terminal.backend must be one of {BACKENDS_BUILT}, not {backend!r}")
    if cfg["routing"].get("untagged_privacy") not in PRIVACY:
        raise ConfigError(f"routing.untagged_privacy must be one of {PRIVACY}")
    for b in cfg["budgets"].get("classes") or []:
        if b.get("on_exhaustion", "refuse") not in ("refuse", "downgrade_to_local"):
            raise ConfigError("budgets.classes[].on_exhaustion must be refuse or downgrade_to_local")
    if cfg["defaults"].get("access") not in ACCESS_LEVELS:
        raise ConfigError(f"defaults.access must be one of {ACCESS_LEVELS}")
    return cfg
