"""Chapter 3: second adapter, same brief through two harnesses, context files.

Stand-in executables play both agents, so these tests cost nothing. The
stand-ins mimic the documented output shapes; they do not prove the real
tools behave that way.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestra import cli, control, outcomes, workspace
from orchestra.contract import EDIT, ERROR, OK

# Edits a file only when given the edit-level flag; never commits.
FAKE_CLAUDE = """#!/bin/sh
for last; do :; done
edit=no; for a; do [ "$a" = "acceptEdits" ] && edit=yes; done
if [ "$last" = "add docstring" ] && [ "$edit" = yes ]; then echo '# doc' >> app.py; fi
echo '{"type":"result","subtype":"success","is_error":false,"result":"claude says hi","total_cost_usd":0.1,"duration_ms":7,"usage":{"input_tokens":10,"cache_read_input_tokens":90,"output_tokens":5}}'
"""

# Emits Codex-style JSON lines. With workspace-write it edits AND commits.
FAKE_CODEX = """#!/bin/sh
for last; do :; done
write=no; for a; do [ "$a" = "workspace-write" ] && write=yes; done
echo '{"type":"thread.started","thread_id":"t1"}'
echo '{"type":"turn.started"}'
if [ "$last" = "explode" ]; then
  echo '{"type":"turn.failed","error":{"message":"model unavailable"}}'; exit 1
fi
if [ "$last" = "add docstring" ] && [ "$write" = yes ]; then
  echo '# doc' >> app.py
  git -c user.name=t -c user.email=t@t commit -qam "docstring" >/dev/null
fi
echo '{"type":"item.completed","item":{"id":"i0","type":"reasoning","text":"thinking"}}'
echo '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"codex says hi"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":800,"cached_input_tokens":700,"output_tokens":50}}'
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class Chapter3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["ORCHESTRA_CLAUDE_BIN"] = _exe(root / "fake-claude", FAKE_CLAUDE)
        os.environ["ORCHESTRA_CODEX_BIN"] = _exe(root / "fake-codex", FAKE_CODEX)
        os.environ["ORCHESTRA_HOME"] = str(root / "home")
        self.home = Path(os.environ["ORCHESTRA_HOME"])
        # Everything in these tests is ordinary internal work. Without a declared
        # privacy class the router would (rightly) keep it away from cloud agents.
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "projects.yaml").write_text("/: {privacy: P1}\n")

        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def top():\n    return 1\n")
        for cmd in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", *cmd], cwd=self.repo, check=True, capture_output=True)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_codex_events_are_normalised(self):
        r = control.delegate("hello", agent="codex", cwd=str(self.repo))
        self.assertEqual((r.status, r.result), (OK, "codex says hi"))
        self.assertIsNone(r.cost_usd)  # tokens, not dollars: unknown, not zero
        self.assertEqual((r.input_tokens, r.output_tokens), (800, 50))

    def test_codex_nested_duplicate_errors_are_made_readable(self):
        fake = Path(os.environ["ORCHESTRA_CODEX_BIN"])
        inner = r'{\"type\":\"error\",\"status\":400,\"error\":{\"message\":\"model not supported\"}}'
        fake.write_text("#!/bin/sh\n"
                        "echo '{\"type\":\"error\",\"message\":\"" + inner + "\"}'\n"
                        "echo '{\"type\":\"turn.failed\",\"error\":{\"message\":\"" + inner + "\"}}'\n"
                        "exit 1\n")
        r = control.delegate("hello", agent="codex", cwd=str(self.repo))
        self.assertEqual((r.status, r.error), (ERROR, "model not supported"))

    def test_codex_failure(self):
        r = control.delegate("explode", agent="codex", cwd=str(self.repo))
        self.assertEqual(r.status, ERROR)
        self.assertIn("model unavailable", r.error)

    def test_claude_token_counts(self):
        r = control.delegate("hello", agent="claude", cwd=str(self.repo))
        self.assertEqual((r.input_tokens, r.output_tokens), (100, 5))

    def test_read_access_leaves_tree_untouched(self):
        for agent in ("claude", "codex"):
            r = control.delegate("add docstring", agent=agent, cwd=str(self.repo))
            self.assertEqual(r.effects, {"commits_added": 0, "committed_files": [],
                                         "uncommitted_files": []})

    def test_effects_distinguish_uncommitted_from_committed(self):
        r = control.delegate("add docstring", agent="claude", cwd=str(self.repo), access=EDIT)
        self.assertEqual(r.effects["uncommitted_files"], ["app.py"])
        self.assertEqual(r.effects["commits_added"], 0)
        subprocess.run(["git", "checkout", "-q", "app.py"], cwd=self.repo, check=True)
        r = control.delegate("add docstring", agent="codex", cwd=str(self.repo), access=EDIT)
        self.assertEqual((r.effects["commits_added"], r.effects["committed_files"]),
                         (1, ["app.py"]))
        self.assertEqual(outcomes.read_all()[-1]["effects"]["commits_added"], 1)

    def test_compare_isolates_agents_and_original(self):
        original = (self.repo / "app.py").read_text()
        runs = control.compare("add docstring", ["claude", "codex"], cwd=str(self.repo),
                               access=EDIT)
        (claude, claude_dir), (codex, codex_dir) = runs
        # Same starting tree for both, different postures, original untouched.
        self.assertEqual(claude.effects["uncommitted_files"], ["app.py"])
        self.assertEqual(claude.effects["commits_added"], 0)
        self.assertEqual(codex.effects["commits_added"], 1)
        self.assertEqual(codex.effects["uncommitted_files"], [])
        self.assertEqual((self.repo / "app.py").read_text(), original)
        self.assertNotEqual(claude_dir, codex_dir)
        entries = outcomes.read_all()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["group"], entries[1]["group"])
        self.assertIn("revert", workspace.describe(codex.effects))
        self.assertIn("you gate the commit", workspace.describe(claude.effects))

    def test_compare_rejects_unknown_agent_before_running_any(self):
        self.assertEqual(cli.main(["compare", "x", "--agents", "claude,nope",
                                   "--cwd", str(self.repo)]), 2)
        self.assertEqual(outcomes.read_all(), [])

    def test_non_git_directory(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        r = control.delegate("hello", agent="claude", cwd=str(plain))
        self.assertTrue(r.ok)
        self.assertIsNone(r.effects)

    def test_context_files(self):
        (self.repo / "CLAUDE.md").write_text("use tabs\n")
        rows = {r["file"]: r for r in workspace.context_files(str(self.repo))}
        self.assertTrue(rows["CLAUDE.md"]["present"])
        self.assertFalse(rows["AGENTS.md"]["present"])
        self.assertTrue(rows["CLAUDE.md"]["consistent"])
        (self.repo / "AGENTS.md").write_text("use spaces\n")
        rows = workspace.context_files(str(self.repo))
        self.assertFalse(rows[0]["consistent"])
        self.assertEqual(cli.main(["context", "--cwd", str(self.repo)]), 0)

    def test_cli_compare_and_agents(self):
        self.assertEqual(cli.main(["compare", "add docstring", "--cwd", str(self.repo),
                                   "--access", "edit"]), 0)
        self.assertEqual(cli.main(["agents", "-v"]), 0)


if __name__ == "__main__":
    unittest.main()
