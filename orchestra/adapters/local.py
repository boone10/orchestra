"""Local model adapter: the "act direct" muscle (book 3.6, 6.2, 10.1).

Talks to a model server on this machine through the OpenAI-compatible chat
endpoint that Ollama provides:

    POST {base_url}/v1/chat/completions   {"model", "messages", "stream": false}
    GET  {base_url}/v1/models             (to see whether it is up, and what it has)

What makes this adapter different from the others:
  - location is "local": the brief never leaves the machine, which is the only
    reason private (P3/P4) work may be given to it.
  - it has no tools. It cannot open files or run commands, so orchestra hands
    it the contents of the files named with --file, and it can only answer.
    It cannot edit.
  - the marginal cost is zero, recorded as 0.0 (known), not unknown.

"Local" is enforced, not assumed: the base URL must point at this machine
unless local.allow_remote is set, because the privacy wall trusts this label.

Written from Ollama's documented OpenAI-compatible API. The project handoff
records a successful read run on the personal Mac.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import List, Optional
from urllib.parse import urlparse

from .. import config
from ..contract import EDIT, ERROR, OK, READ, TIMEOUT, Result
from .base import Adapter

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Never route a call to the local model through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalAdapter(Adapter):
    name = "local"
    location = "local"
    can_edit = False
    needs_inline_files = True
    fit = ("routine", "private")
    version = "2026.11.20-1"
    profile = {
        "backend": "a model server on this machine (Ollama)",
        "local_models": "yes",
        "safety_default": "cannot act: no tools, no file access, answers only",
        "context_file": "none; orchestra supplies the files you name",
        "edit_model": "cannot edit",
        "best_for": "cheap, private, routine work: summarise, classify, extract",
        "claim": "observed 2026-11-22: a queued read with llama3.2:latest returned the value from a named file",
    }

    def _settings(self) -> dict:
        try:
            return config.load()["local"]
        except config.ConfigError:
            return dict(config.DEFAULTS["local"])

    def _base(self) -> Optional[str]:
        s = self._settings()
        base = str(s.get("base_url") or "").rstrip("/")
        host = urlparse(base).hostname
        if not base or (host not in _LOOPBACK and not s.get("allow_remote")):
            return None
        return base

    def models(self, timeout: float = 1.5) -> List[str]:
        base = self._base()
        if not base:
            return []
        try:
            with _opener.open(base + "/v1/models", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def model(self) -> Optional[str]:
        chosen = self._settings().get("model")
        if chosen:
            return str(chosen)
        # Ollama lists embedding models alongside chat models; those cannot answer.
        found = [m for m in self.models() if "embed" not in m.lower()]
        return found[0] if found else None

    def available(self) -> bool:
        return bool(self.models())

    def delegate(self, brief: str, cwd: Optional[str] = None, timeout: int = 600,
                 access: str = READ) -> Result:
        started = time.monotonic()
        elapsed = lambda: int((time.monotonic() - started) * 1000)
        if access == EDIT:
            return Result(self.name, ERROR, error="the local model cannot edit files; it only answers")
        base = self._base()
        if not base:
            return Result(self.name, ERROR, error="local.base_url must point at this machine "
                                                  "(127.0.0.1 or localhost) for work to count as local")
        model = self.model()
        if not model:
            return Result(self.name, ERROR, duration_ms=elapsed(),
                          error=f"no local model reachable at {base}. Is Ollama running, "
                                "and has a model been pulled (`ollama pull <model>`)?")
        body = json.dumps({"model": model, "stream": False,
                           "messages": [{"role": "user", "content": brief}]}).encode("utf-8")
        request = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with _opener.open(request, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            text = data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            return Result(self.name, ERROR, duration_ms=elapsed(),
                          error=f"local model returned HTTP {e.code}: {detail}")
        except (TimeoutError, urllib.error.URLError) as e:
            timed_out = isinstance(e, TimeoutError) or "timed out" in str(e).lower()
            return Result(self.name, TIMEOUT if timed_out else ERROR, duration_ms=elapsed(),
                          error=f"no reply within {timeout}s" if timed_out
                          else f"could not reach the local model: {e}")
        except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
            return Result(self.name, ERROR, duration_ms=elapsed(),
                          error=f"unparseable reply from the local model: {type(e).__name__}")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        return Result(self.name, OK, result=text, cost_usd=0.0,
                      input_tokens=tin if isinstance(tin, int) else None,
                      output_tokens=tout if isinstance(tout, int) else None,
                      duration_ms=elapsed())
