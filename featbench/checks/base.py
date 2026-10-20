"""Check framework: base class, context, registry and plugin loading.

A *check* is one measurable expectation about the agent's implementation. Each check has a
category (business_rules | architecture | performance | anything you invent), a weight and a
score in [0, 1]. To add a new kind of check, subclass `Check`, decorate it with `@register`, and
drop the file in a `plugins/` folder next to your benchmarks (see examples in the README).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar

from .. import gitutil

CATEGORIES = ("business_rules", "architecture", "performance")
SNAPSHOT_TAG = "featbench-snapshot"

REGISTRY: dict[str, type["Check"]] = {}


def register(cls: type["Check"]) -> type["Check"]:
    if not getattr(cls, "type_name", None):
        raise TypeError(f"{cls.__name__} must define `type_name`")
    REGISTRY[cls.type_name] = cls
    return cls


_loaded_plugins: set[str] = set()


def load_plugins(dirs: list[str | Path]) -> list[str]:
    """Import every `*.py` in the given directories (and $FEATBENCH_PLUGINS) so they can register checks."""
    env = os.environ.get("FEATBENCH_PLUGINS")
    all_dirs = list(dirs) + (env.split(os.pathsep) if env else [])
    loaded = []
    for d in all_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for py in sorted(d.glob("*.py")):
            key = str(py.resolve())
            if key in _loaded_plugins or py.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"featbench_plugin_{py.stem}", py)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _loaded_plugins.add(key)
            loaded.append(key)
    return loaded


# --------------------------------------------------------------------------- results

@dataclass
class CheckResult:
    id: str
    type: str
    category: str
    weight: float
    status: str               # pass | fail | error | skip
    score: float              # 0..1 (ignored when skipped)
    summary: str = ""
    details: dict = field(default_factory=dict)
    duration_s: float = 0.0
    calibration: dict | None = None   # data recorded during `featbench calibrate`

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["calibration"] is None:
            d.pop("calibration")
        return d


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, n: int = 40) -> str:
        text = (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()
        return "\n".join(text.splitlines()[-n:])


@dataclass
class Change:
    path: str
    status: str        # A, M, D
    added: int
    deleted: int
    added_lines: set[int] = field(default_factory=set)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def compute_changes(workspace: Path, ref: str = SNAPSHOT_TAG, exclude: set[str] | None = None) -> list[Change]:
    """What the agent changed relative to the snapshot (including untracked files)."""
    exclude = exclude or set()
    gitutil.run_git(workspace, "add", "-A")
    try:
        status = gitutil.run_git(workspace, "diff", "--cached", "--name-status", "--no-renames", ref)
        numstat = gitutil.run_git(workspace, "diff", "--cached", "--numstat", "--no-renames", ref)
        patch = gitutil.run_git(workspace, "diff", "--cached", "-U0", "--no-renames", "--no-color", ref)
    finally:
        gitutil.run_git(workspace, "reset", "-q", check=False)
    changes: dict[str, Change] = {}
    for line in status.splitlines():
        st, _, path = line.partition("\t")
        if path and path not in exclude and not path.startswith(".featbench/"):
            changes[path] = Change(path, st[:1], 0, 0)
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] in changes:
            changes[parts[2]].added = int(parts[0]) if parts[0].isdigit() else 0
            changes[parts[2]].deleted = int(parts[1]) if parts[1].isdigit() else 0
    current = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            p = line[4:]
            current = changes.get(p[2:]) if p.startswith("b/") else None
        elif line.startswith("@@") and current is not None:
            m = _HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                current.added_lines.update(range(start, start + count))
    return list(changes.values())


@dataclass
class CheckContext:
    workspace: Path
    benchmark_dir: Path
    changes: list[Change]
    calibration: dict = field(default_factory=dict)   # check_id -> data saved by `calibrate`
    calibrating: bool = False
    default_timeout: int = 600
    snapshot_ref: str = SNAPSHOT_TAG
    env: dict = field(default_factory=dict)

    def changed_paths(self, include_deleted: bool = False) -> list[str]:
        return [c.path for c in self.changes if include_deleted or c.status != "D"]

    def change(self, path: str) -> Change | None:
        return next((c for c in self.changes if c.path == path), None)

    def read(self, rel: str) -> str | None:
        try:
            return (self.workspace / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def read_snapshot(self, rel: str) -> str | None:
        return gitutil.show_file(self.workspace, self.snapshot_ref, rel)

    def all_files(self) -> list[str]:
        gitutil.run_git(self.workspace, "add", "-A")
        try:
            return gitutil.ls_files(self.workspace)
        finally:
            gitutil.run_git(self.workspace, "reset", "-q", check=False)

    def expand(self, text: str, extra: dict | None = None) -> str:
        values = {"workspace": str(self.workspace), "benchmark_dir": str(self.benchmark_dir),
                  "python": sys.executable, **(extra or {})}
        for k, v in values.items():
            text = text.replace("{" + k + "}", v)
        return text

    def run_command(self, cmd: str, timeout: int | None = None, extra: dict | None = None) -> CommandResult:
        cmd = self.expand(cmd, extra)
        env = {**os.environ, **{k: str(v) for k, v in self.env.items()}}
        start = time.perf_counter()
        try:
            p = subprocess.run(cmd, shell=True, cwd=self.workspace, capture_output=True, env=env,
                               timeout=timeout or self.default_timeout)
            return CommandResult(p.returncode, p.stdout.decode(errors="replace"),
                                 p.stderr.decode(errors="replace"), time.perf_counter() - start)
        except subprocess.TimeoutExpired as e:
            return CommandResult(-1, (e.stdout or b"").decode(errors="replace"),
                                 (e.stderr or b"").decode(errors="replace"),
                                 time.perf_counter() - start, timed_out=True)


# --------------------------------------------------------------------------- base check

class Check:
    """Subclass, set `type_name` / `default_category`, implement `run`, decorate with @register."""

    type_name: ClassVar[str] = ""
    default_category: ClassVar[str] = "business_rules"
    description: ClassVar[str] = ""
    params_doc: ClassVar[dict[str, str]] = {}

    def __init__(self, spec: dict):
        self.spec = spec
        self.id: str = spec["id"]
        self.category: str = spec.get("category", self.default_category)
        self.weight: float = float(spec.get("weight", 1.0))
        self.params: dict = spec.get("params", {}) or {}
        self.timeout: int | None = spec.get("timeout_seconds")

    def param(self, name: str, default=None):
        return self.params.get(name, default)

    def run(self, ctx: CheckContext) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # helpers -----------------------------------------------------------------
    def result(self, status: str, score: float, summary: str = "", **details) -> CheckResult:
        return CheckResult(self.id, self.type_name, self.category, self.weight, status,
                           max(0.0, min(1.0, float(score))), summary, details)

    def passed(self, summary: str = "", **details) -> CheckResult:
        return self.result("pass", 1.0, summary, **details)

    def failed(self, score: float, summary: str = "", **details) -> CheckResult:
        return self.result("fail", score, summary, **details)

    def skipped(self, summary: str) -> CheckResult:
        return self.result("skip", 0.0, summary)

    def errored(self, summary: str, **details) -> CheckResult:
        return self.result("error", 0.0, summary, **details)


def build_check(spec: dict) -> Check:
    ctype = spec.get("type")
    if ctype not in REGISTRY:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"check '{spec.get('id')}': unknown type '{ctype}'. Known types: {known}")
    return REGISTRY[ctype](spec)
