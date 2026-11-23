"""Skills: the capability registry (book chapter 8).

A skill is a folder under ~/.orchestra/skills/<name>/:

  skill.yaml   name, summary and the declarations the registry matches on
  SKILL.md     the body: the procedure, in prose (loaded only when chosen)
  scripts/     for native skills, what the control plane runs
  fixtures/    whatever the skill's own test needs

Three loading levels (8.3) keep a large library cheap: names() lists folders,
summaries() reads only each skill.yaml, body() reads SKILL.md for the one
skill that was chosen.

Three classes (8.5), by who does the work:
  native      the control plane runs the skill's script itself. This is what
              "act direct" means when no model is needed at all.
  delegation  wraps an agent: the body is added to the brief as the procedure.
  tool        wraps an external tool server (chapter 9). Declared and listed;
              not runnable until the coordinator has an MCP client.

Trust (8.7) is recorded outside the skill folder, in skills.lock.json, so a
skill cannot declare itself trusted. A skill is a draft until a person runs
`orchestra skill trust <name>`; that records a hash of every file in it. Edit
any file afterwards and the skill is "modified" and stops being used until it
is reviewed again. That is the same rule for your own skills, for ones an
agent drafted, and for ones you copied from someone else (8.10, 8.11): an
installed skill is an installed capability.

Matching (8.4, 8.9) is relevance filtered by admissibility, and every match
is logged: which skills were considered, which matched, which was chosen and
why the others were not.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import backends, config, memory, outcomes, state, stop_switch
from .contract import EDIT, ERROR, OK, PRIVACY, READ, TIMEOUT, Result

CATEGORIES = ("native", "delegation", "tool")
DRAFT, TRUSTED, MODIFIED, DEPRECATED = "draft", "trusted", "modified", "deprecated"


class SkillError(Exception):
    pass


def root() -> Path:
    return config.home() / "skills"


def _folder(name: str) -> Path:
    if not name or Path(name).name != name or name in (".", ".."):
        raise SkillError("use a skill name from the registry")
    path = root() / name
    if path.is_symlink() or root().is_symlink():
        raise SkillError("skill folders must be real directories inside the registry")
    return path


def _lock_path() -> Path:
    return config.home() / "skills.lock.json"


def _lock() -> dict:
    try:
        return json.loads(_lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_lock(lock: dict) -> None:
    tmp = _lock_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(lock, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _lock_path())


# --- loading level 1: names ----------------------------------------------------

def names() -> List[str]:
    if not root().is_dir():
        return []
    return sorted(p.name for p in root().iterdir() if p.is_dir() and (p / "skill.yaml").is_file())


# --- loading level 2: summaries ---------------------------------------------------

def content_hash(name: str) -> str:
    return content_hash_at(_folder(name))


def content_hash_at(base: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if "__pycache__" in path.relative_to(base).parts:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise SkillError("skills must contain only regular files and folders")
        if path.is_dir():
            continue
        h.update(path.relative_to(base).as_posix().encode() + b"\0")
        with path.open("rb") as data:
            for chunk in iter(lambda: data.read(1024**2), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def summary(name: str) -> dict:
    """A skill's declaration plus its trust status. Does not read the body."""
    import yaml
    path = _folder(name) / "skill.yaml"
    if path.is_symlink() or not path.is_file():
        raise SkillError(f"missing regular manifest: {path}")
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise SkillError(f"cannot read {path}: {e}") from None
    if not isinstance(spec, dict):
        raise SkillError(f"{path} must contain a mapping")
    spec.setdefault("name", name)
    if spec["name"] != name:
        raise SkillError(f"{path}: name '{spec['name']}' does not match its folder '{name}'")
    spec.setdefault("description", "")
    spec.setdefault("category", "native")
    spec.setdefault("version", "0")
    spec.setdefault("access", READ)
    spec.setdefault("unattended", False)
    spec.setdefault("triggers", [])
    spec.setdefault("required_environment_variables", [])
    if spec["category"] not in CATEGORIES:
        raise SkillError(f"{path}: category must be one of {CATEGORIES}")
    entry = _lock().get(name) or {}
    if entry.get("status") == DEPRECATED:
        spec["status"] = DEPRECATED
    elif entry.get("status") == TRUSTED:
        spec["status"] = TRUSTED if entry.get("hash") == content_hash(name) else MODIFIED
    else:
        spec["status"] = DRAFT
    spec["reviewed"] = entry.get("reviewed")
    spec["reviewed_hash"] = entry.get("hash") if spec["status"] == TRUSTED else None
    return spec


def summaries() -> List[dict]:
    out = []
    for name in names():
        try:
            out.append(summary(name))
        except SkillError as e:
            out.append({"name": name, "status": "broken", "description": str(e),
                        "category": "?", "version": "?", "triggers": [], "access": READ,
                        "unattended": False})
    return out


# --- loading level 3: the body --------------------------------------------------------

def body(name: str) -> str:
    path = root() / name / "SKILL.md"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


# --- matching: relevance, filtered by admissibility --------------------------------------

def _admissible(spec: dict, axes: dict, access: str) -> Optional[str]:
    """None if the skill may be used for this task; otherwise why not."""
    if spec["status"] != TRUSTED:
        return {DRAFT: "a draft; review it and run `orchestra skill trust`",
                MODIFIED: "changed since it was trusted; review it again",
                DEPRECATED: "deprecated"}.get(spec["status"], "not usable")
    if spec["category"] == "tool":
        return "tool skills are not runnable yet (no MCP client in the coordinator)"
    if spec["access"] == EDIT and access == READ:
        return "it writes, and this task is read-only"
    if spec["category"] == "native" and access == EDIT:
        return "native skills only read; this task needs edits"
    if axes["autonomy"]["value"] == "unattended" and not spec.get("unattended"):
        return "not declared safe to run unattended"
    if axes["stakes"]["value"] == "high" and spec["access"] == EDIT:
        return "it writes, and the task is high-stakes"
    limit = spec.get("max_privacy")
    if limit in PRIVACY and PRIVACY.index(axes["privacy"]["value"]) > PRIVACY.index(limit):
        return f"declared for data up to {limit} only"
    return None


def match(task: str, axes: dict, access: str, cfg: dict, forced: Optional[str] = None) -> dict:
    """Find the skill for a task. Returns the full match log (8.4)."""
    threshold = int(cfg["skills"].get("match_threshold", 2))
    want = memory.tokens(task)
    candidates = []
    for spec in summaries():
        about = memory.tokens(" ".join([spec["name"].replace("-", " "), str(spec["description"])]
                                       + [str(t) for t in spec.get("triggers") or []]))
        score = memory.overlap(want, about)
        if forced:
            relevant = spec["name"] == forced
        else:
            relevant = score >= threshold
        if not relevant:
            continue
        why_not = "broken: " + spec["description"] if spec["status"] == "broken" \
            else _admissible(spec, axes, access)
        candidates.append({"name": spec["name"], "version": str(spec["version"]),
                           "category": spec["category"], "score": score,
                           "status": spec["status"], "rejected": why_not})
    candidates.sort(key=lambda c: c["score"], reverse=True)
    usable = [c for c in candidates if not c["rejected"]]
    log = {"considered": len(names()), "candidates": candidates,
           "chosen": usable[0]["name"] if usable else None}
    if forced and not candidates:
        raise SkillError(f"no skill named '{forced}'")
    if forced and not usable:
        raise SkillError(f"skill '{forced}' cannot be used here: {candidates[0]['rejected']}")
    return log


# --- lifecycle ------------------------------------------------------------------------------

def run_test(name: str, timeout: int = 120, expected_hash: Optional[str] = None) -> dict:
    spec = summary(name)
    if not spec.get("test"):
        return {"declared": False, "passed": None, "output": ""}
    try:
        if spec["required_environment_variables"]:
            raise SkillError("offline skill tests cannot use host environment variables")
        folder = _folder(name)
        p = backends.run_skill(spec["test"], skill_dir=folder, project=None, cwd=folder,
                               timeout=timeout, expected_hash=expected_hash or content_hash(name))
        return {"declared": True, "passed": p.returncode == 0,
                "output": (p.stdout + p.stderr).strip()[-2000:]}
    except (OSError, SkillError, subprocess.TimeoutExpired, backends.HardlineRefusal) as e:
        return {"declared": True, "passed": False, "output": f"{type(e).__name__}: {e}"}


def trust(name: str, without_test: bool = False, by: str = "operator") -> dict:
    """The review step. A person has read the skill; this records that, and
    pins exactly what they read."""
    spec = summary(name)
    if not str(spec["description"]).strip():
        raise SkillError("write a description first: it is what tasks are matched against")
    if spec["category"] == "native" and not spec.get("run"):
        raise SkillError("a native skill needs a `run:` command")
    if spec["category"] == "native" and spec["required_environment_variables"]:
        raise SkillError("offline native skills cannot use host environment variables")
    fingerprint = content_hash(name)
    tested = run_test(name, expected_hash=fingerprint)
    if tested["declared"] and not tested["passed"]:
        raise SkillError(f"its own test fails, so it is not trusted:\n{tested['output']}")
    if not tested["declared"] and not without_test:
        raise SkillError("this skill declares no `test:`. Writing one is part of the review. "
                         "To trust it untested anyway, pass --without-test.")
    if content_hash(name) != fingerprint:
        raise SkillError("skill changed during review; review it again")
    lock = _lock()
    lock[name] = {"status": TRUSTED, "version": str(spec["version"]), "hash": fingerprint,
                  "reviewed": state.now(), "reviewed_by": by,
                  "test": "passed" if tested["declared"] else "none"}
    _save_lock(lock)
    state.audit("skill.trusted", skill=name, version=str(spec["version"]), by=by,
                test=lock[name]["test"])
    return summary(name)


def deprecate(name: str) -> dict:
    summary(name)
    lock = _lock()
    lock[name] = dict(lock.get(name) or {}, status=DEPRECATED, deprecated=state.now())
    _save_lock(lock)
    state.audit("skill.deprecated", skill=name)
    return summary(name)


def usage() -> dict:
    """How each skill has actually done (8.7, step four: observation)."""
    seen = {}
    for e in outcomes.read_all():
        if e.get("skill"):
            u = seen.setdefault(str(e["skill"]).split("@")[0], {"used": 0, "failed": 0})
            u["used"] += 1
            u["failed"] += e.get("status") != OK
    return seen


def new(name: str, category: str = "delegation") -> Path:
    if category not in CATEGORIES:
        raise SkillError(f"category must be one of {CATEGORIES}")
    if not name.replace("-", "").replace("_", "").isalnum() or name != name.lower():
        raise SkillError("use a lowercase name with dashes, e.g. audit-wiki-links")
    path = root() / name
    if path.exists():
        raise SkillError(f"{path} already exists")
    (path / "fixtures").mkdir(parents=True)
    extra = {"native": "run: [python3, scripts/run.py]\ntest: [python3, scripts/run.py, --self-test]\n",
             "delegation": "agent: claude            # default agent; the router's rules still apply\n",
             "tool": "server: name-of-mcp-server\ntool: name_of_tool\ngated: true\n"}[category]
    (path / "skill.yaml").write_text(
        f"name: {name}\n"
        "description: >\n"
        "  TODO. Name the trigger, the action, the scope and the safety posture, e.g.\n"
        "  \"Crawl a documentation wiki, check links, and report broken URLs; read-only;\n"
        "  safe to run unattended.\" This sentence is what tasks are matched against.\n"
        f"category: {category}\n"
        "version: \"0.1\"\n"
        "owner: TODO\n"
        "triggers: []              # extra words a matching task would contain\n"
        "task_class: general\n"
        "access: read              # read | edit\n"
        "unattended: false         # may it run with nobody watching?\n"
        "required_environment_variables: []   # offline skills receive only task metadata\n"
        + extra, encoding="utf-8")
    (path / "SKILL.md").write_text(
        f"# {name}\n\nTODO: when to use this, the inputs it expects, the steps, what it must "
        "never do, and how to read its output.\n", encoding="utf-8")
    if category == "native":
        (path / "scripts").mkdir()
        (path / "scripts" / "run.py").write_text(
            "#!/usr/bin/env python3\n"
            "\"\"\"Runs in the project directory. Reads ORCHESTRA_TASK and ORCHESTRA_FILES\n"
            "(one path per line). Print the result; exit non-zero only if the skill itself failed.\"\"\"\n"
            "import sys\n\n"
            "if \"--self-test\" in sys.argv:\n"
            "    sys.exit(\"TODO: check the skill against fixtures/ and exit 0 only if it behaves\")\n"
            "print(\"TODO\")\n", encoding="utf-8")
    return path


# --- running a native skill -------------------------------------------------------------------

class SkillRunner:
    """Lets the delegation primitive drive a native skill exactly as it drives
    an agent: same contract, same validation, same outcome record."""

    location = "local"
    can_edit = False
    sandboxed = True
    needs_inline_files = False
    auto_reads_context = True
    context_file = ""
    fit = ()

    def __init__(self, name: str, project: str, files: List[str]):
        self.spec = summary(name)
        self.skill = name
        self.name = f"skill:{name}"
        self.version = str(self.spec["version"])
        self.project, self.files = project, files

    def available(self) -> bool:
        return self.spec["status"] == TRUSTED

    def delegate(self, brief: str, cwd: Optional[str] = None, timeout: int = 600,
                 access: str = READ) -> Result:
        started = time.monotonic()
        elapsed = lambda: int((time.monotonic() - started) * 1000)
        try:
            entry = _lock().get(self.skill) or {}
            fingerprint = self.spec["reviewed_hash"]
            if not fingerprint or entry.get("status") != TRUSTED or entry.get("hash") != fingerprint:
                raise SkillError("skill needs a current trust record")
            if self.spec["required_environment_variables"] or access != READ:
                raise SkillError("offline native skills require read access and no host environment")
            proc = backends.run_skill(self.spec.get("run"), skill_dir=_folder(self.skill),
                                      project=Path(self.project), cwd=Path(cwd or self.project),
                                      timeout=timeout, task=brief, files=self.files, expected_hash=fingerprint)
        except SkillError as e:
            return Result(self.name, ERROR, error=str(e), duration_ms=elapsed())
        except backends.HardlineRefusal as e:
            return Result(self.name, ERROR, error=f"refused by the hardline floor: {e}",
                          duration_ms=elapsed())
        except subprocess.TimeoutExpired:
            return Result(self.name, TIMEOUT, error=f"no reply within {timeout}s", duration_ms=elapsed())
        except stop_switch.Stopped:
            raise  # the delegation lifecycle records an operator denial
        except OSError as e:
            return Result(self.name, ERROR, error=f"could not start the skill: {e}", duration_ms=elapsed())
        text = proc.stdout.strip()[:20000]
        try:  # a skill may answer in the contract's own shape
            data = json.loads(text)
            if isinstance(data, dict) and "result" in data:
                text = str(data["result"])
        except ValueError:
            pass
        failed = proc.returncode != 0
        return Result(self.name, ERROR if failed else OK, result=text, cost_usd=0.0,
                      duration_ms=elapsed(), exit_code=proc.returncode,
                      error=(proc.stderr.strip()[-500:] or f"exit code {proc.returncode}") if failed else None)


# --- examples: installed as drafts, for you to read and trust ---------------------------------

_CHECK_LINKS = r'''#!/usr/bin/env python3
"""Report relative Markdown links that point at files which do not exist.

Read-only: opens files, writes nothing, touches no network. Runs in the
project directory. If ORCHESTRA_FILES names files, only those are checked.
Exit code is 0 when the check ran (even if it found broken links) and
non-zero only if the check itself could not run.
"""
import json
import os
import re
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
EXTERNAL = ("http://", "https://", "mailto:", "tel:", "#", "data:")


def check(root: Path, only=None):
    files = [root / f for f in only] if only else [
        p for p in root.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(root).parts)]
    broken, checked = [], 0
    for md in sorted(f for f in files if f.is_file() and f.suffix.lower() == ".md"):
        for number, line in enumerate(md.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for target in LINK.findall(line):
                if target.startswith(EXTERNAL):
                    continue
                checked += 1
                path = target.split("#")[0].split("?")[0]
                where = (root / path.lstrip("/")) if path.startswith("/") else (md.parent / path)
                if path and not where.exists():
                    broken.append(f"{md.relative_to(root)}:{number} -> {target}")
    return checked, broken


if "--self-test" in sys.argv:
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "wiki"
    before = sorted(p.name for p in fixture.iterdir())
    checked, broken = check(fixture)
    assert checked == 3, f"expected 3 local links, saw {checked}"
    assert broken == ["index.md:4 -> missing.md"], broken
    assert sorted(p.name for p in fixture.iterdir()) == before, "the check must not write"
    print("self-test passed: found the one broken link, ignored the web link, wrote nothing")
    sys.exit(0)

only = [f for f in os.environ.get("ORCHESTRA_FILES", "").splitlines() if f.strip()]
checked, broken = check(Path.cwd(), only)
report = f"Checked {checked} local link(s) in Markdown files; {len(broken)} broken."
if broken:
    report += "\n" + "\n".join(f"- {b}" for b in broken)
print(json.dumps({"result": report, "status": "ok", "broken": len(broken)}))
'''

EXAMPLES = {
    "check-doc-links": {
        "skill.yaml": '''name: check-doc-links
description: >
  Check the relative links in a project's Markdown files and report the ones
  that point at files which do not exist. Read-only, no network, no secrets.
  Safe to run unattended.
category: native
version: "1.0"
owner: orchestra examples
triggers: [links, broken, markdown, docs, documentation, readme, audit]
task_class: review
access: read
unattended: true
max_privacy: P4              # nothing leaves the machine, so any data may be checked
required_environment_variables: []
run: [python3, scripts/check_links.py]
test: [python3, scripts/check_links.py, --self-test]
''',
        "SKILL.md": '''# check-doc-links

Use for: "check the docs for broken links", "audit the links in the README".

What it does: reads every `.md` file in the project (or only the files named
with `--file`), finds links of the form `[text](path)`, and reports those
whose target file does not exist. Web links are ignored: it makes no network
calls.

It never writes. It needs no credentials. A non-zero exit means the check
itself failed, not that links are broken.

Output: a one-line count, then one line per broken link as `file:line -> target`.
''',
        "scripts/check_links.py": _CHECK_LINKS,
        "fixtures/wiki/index.md": "# Wiki\n\nSee [the guide](guide.md) and [its setup section](guide.md#setup).\n"
                                  "This one is broken: [gone](missing.md).\nAnd the web: [site](https://example.com).\n",
        "fixtures/wiki/guide.md": "# Guide\n\n## Setup\n\nNothing to see.\n",
    },
    "careful-code-change": {
        "skill.yaml": '''name: careful-code-change
description: >
  Make a supervised change to existing code: a bug fix, a small feature or a
  refactor in named files. Works in a private copy and returns a diff for
  human review. Not for unattended use.
category: delegation
version: "1.0"
owner: orchestra examples
triggers: [fix, bug, implement, feature, refactor, rename, change, modify, update]
task_class: feature
access: edit
unattended: false
agent: claude
required_environment_variables: []
''',
        "SKILL.md": '''Follow these steps in order.

1. Read the files in scope before changing anything. If no files were named,
   find the smallest set the task needs and say which you chose.
2. Make the smallest change that does the task. Match the surrounding style.
   Do not reformat, rename or tidy anything the task did not ask for.
3. If the task cannot be done without touching a file outside the scope, stop
   and say which file and why. Do not touch it.
4. Do not add dependencies, change configuration, or edit tests to make them
   pass unless the task asks for exactly that.
5. Finish with: what you changed and why, anything you were unsure about, and
   the commands a person should run to check it.
''',
    },
}


def install_examples() -> List[str]:
    """Copy the example skills in as drafts. Never overwrites."""
    created = []
    for name, files in EXAMPLES.items():
        base = root() / name
        if base.exists():
            continue
        for rel, text in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        created.append(name)
    return created
