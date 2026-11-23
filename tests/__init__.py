"""Fixture setup for suites that register individual stand-in CLIs."""

import os
from pathlib import Path
from unittest.mock import patch

from orchestra.adapters.local import LocalAdapter


def disable_live_providers(test):
    """Each test opts back into the specific fake executables it created."""
    missing = str(Path(test.tmp.name) / "uninstalled-agent")
    binaries = patch.dict(os.environ, ORCHESTRA_CLAUDE_BIN=missing, ORCHESTRA_CODEX_BIN=missing,
                          ORCHESTRA_DEPTH="0")
    models = patch.object(LocalAdapter, "models", return_value=[])
    for stub in (binaries, models):
        stub.start()
        test.addCleanup(stub.stop)
