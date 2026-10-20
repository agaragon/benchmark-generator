"""Generate benchmark folders from analysed features."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

import yaml

from . import deps, gitutil, langs
from .analysis import Feature, RepoProfile
from .analysis.features import CONV_RE, FIX_RE

HEADER = """\
# featbench benchmark — generated, meant to be edited by hand.
#
# How it works
#   snapshot      the state of the repo *before* the feature; the agent starts from it.
#   task.md       what the agent is asked to do (edit freely; it is the spec the agent sees).
#   hidden_files  tests copied into the workspace only at evaluation time (the agent never sees them).
#   checks        what is measured. category: business_rules | architecture | performance (or your own,
#                 as long as it has a weight in scoring.weights). Each check scores 0..1 and has a weight.
#
# Extending
#   * any check type can be added/removed/tuned here; `featbench checks` lists types and parameters
#   * `type: command` turns any script (any language) into a check: exit code 0 = pass
#   * new check types: drop a Python file in ../plugins/ (see ../plugins/example_plugin.py)
#   * thresholds set to `auto` are filled by `featbench calibrate` (reference baseline x tolerance)
#
# Examples
#   - id: br-http-contract
#     category: business_rules
#     type: command
#     weight: 2
#     params: {run: "bash {benchmark_dir}/scripts/contract_test.sh"}
#   - id: perf-p95-latency
#     type: command_metric
#     params: {run: "k6 run --summary-export=/dev/stdout load.js", json_path: "metrics.http_req_duration.p(95)",
#              threshold: 250, direction: lower}
#   - id: arch-no-orm-in-domain
#     type: forbidden_imports
#     params: {rules: [{files: ["**/domain/**"], forbid: ["sqlalchemy/**", "django/**"]}]}
"""

class _PlainDumper(yaml.SafeDumper):
    """No YAML anchors/aliases: generated files must be easy to edit by hand."""

    def ignore_aliases(self, data):
        return True


DEBUG_PATTERNS = {
    "python": [{"regex": r"\bbreakpoint\(\)|\bpdb\.set_trace\(", "message": "debugger left in code"}],
    "javascript": [{"regex": r"\bdebugger\s*;", "message": "debugger left in code"},
                   {"regex": r"console\.log\(", "message": "console.log left in code"}],
    "go": [{"regex": r"\bfmt\.Println\(\"DEBUG", "message": "debug print"}],
    "java": [{"regex": r"System\.out\.println|printStackTrace\(\)", "message": "debug print"}],
}
DEBUG_PATTERNS["typescript"] = DEBUG_PATTERNS["javascript"]
DEBUG_PATTERNS["kotlin"] = DEBUG_PATTERNS["java"]
COMMON_PATTERNS = [
    {"regex": r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]", "message": "hard-coded secret"},
]


def select_features(features: list[Feature], select: str | None, recommended: bool, top: int | None,
                    all_: bool) -> list[Feature]:
    if all_:
        return features
    if select:
        chosen = []
        for token in [t.strip() for t in select.split(",") if t.strip()]:
            if token.isdigit() and 1 <= int(token) <= len(features):
                chosen.append(features[int(token) - 1])
                continue
            match = [f for f in features if f.id == token or f.id.endswith("." + token) or f.name == token]
            if not match:
                raise SystemExit(f"error: unknown feature '{token}'")
            chosen += match
        return _dedupe(chosen)
    recommended_pool = [f for f in features if f.suitability.get("recommended")]
    if top:
        return (recommended_pool if recommended else features)[:top]
    return recommended_pool


def _dedupe(items: list[Feature]) -> list[Feature]:
    seen, out = set(), []
    for f in items:
        if f.id not in seen:
            seen.add(f.id)
            out.append(f)
    return out


def _infer_role(f: Feature) -> str:
    exts = {posixpath.splitext(p)[1].lower() for p in f.files}
    if exts & langs.FRONTEND_EXTS:
        return "frontend"
    if any(p.endswith((".tf", ".yaml", ".yml", "Dockerfile")) or "/k8s/" in p or "helm" in p for p in f.files) \
            and not f.src_files:
        return "sre"
    return "backend"


def _dir_glob(path: str, depth: int = 2) -> str:
    parts = path.split("/")[:-1][:depth]
    return "/".join(parts) + "/**" if parts else "*"


def _ref_threshold(repo: str, f: Feature, profile: RepoProfile) -> dict:
    """Thresholds: repo-wide percentiles, relaxed to the reference only when the reference is clean."""
    th = dict(profile.thresholds)
    if f.quality.get("label") != "clean" or not f.reference_commit:
        return th
    blobs = gitutil.read_blobs(repo, [(f.reference_commit, p) for p in f.src_files])
    touched = (gitutil.added_lines(repo, f.base_commit, f.reference_commit, f.src_files)
               if f.base_commit else {})
    for p in f.src_files:
        text = blobs.get((f.reference_commit, p))
        if not text:
            continue
        th["file_lines"] = max(th["file_lines"], text.count("\n") + 1)
        lines = touched.get(p)
        for fn in langs.function_metrics(p, text):
            if lines is not None and f.base_commit and not any(fn.start <= ln <= fn.end for ln in lines):
                continue
            th["complexity"] = max(th["complexity"], fn.complexity)
            th["function_length"] = max(th["function_length"], fn.length)
    return th


NOISE_TYPES = {"fix", "hotfix", "bugfix", "test", "tests", "revert", "chore", "docs", "style", "ci", "build", "refactor"}


def _spec_subjects(subjects: list[str]) -> list[str]:
    """Commit subjects that describe *what* was built (no fixes/tests/merges), without the type prefix."""
    out = []
    for s in subjects:
        if s.lower().startswith(("merge", "revert")):
            continue
        m = CONV_RE.match(s)
        if m and m.group("type").lower() in NOISE_TYPES:
            continue
        if not m and (FIX_RE.search(s) or re.search(r"\b(tests?|specs?)\b", s, re.I)):
            continue
        out.append(m.group("desc") if m else s)
    return out or [CONV_RE.sub(lambda m: m.group("desc"), s) for s in subjects[:5]]


def _net_diff_lines(repo: str, f: Feature) -> int:
    """Size of the feature as a reviewer would see it (net diff), not the churn of its commits."""
    if f.base_commit and f.reference_commit:
        stats = gitutil.diff_numstat(repo, f.base_commit, f.reference_commit)
        return sum(s.added + s.deleted for s in stats if s.path in f.files and not langs.is_noise(s.path))
    return sum(d["added"] for p, d in f.files.items() if not langs.is_noise(p))


def _task_markdown(f: Feature, profile: RepoProfile, behaviours: dict[str, list[str]], checks: list[dict],
                   hints: bool) -> str:
    subjects = _spec_subjects(f.subjects)
    lines = [f"# Task: {f.name}", "",
             "> Generated by featbench from the repository history. Review and rewrite this file so it reads "
             "like the ticket a developer would receive. It is the only spec the agent gets.", "",
             "## Context", "",
             f"You are working on the `{profile.name}` repository ({', '.join(profile.stacks) or profile.primary_language}). "
             "Implement the feature below in a way that fits the existing code base. "
             "If anything is unclear, ask the operator before guessing (see CLARIFICATIONS.md if present).", "",
             "## What to build", ""]
    lines += [f"- {s}" for s in subjects] or ["- (describe the feature here)"]
    bodies = [b for b in f.bodies if b and not b.startswith("Co-Authored-By")]
    if bodies:
        lines += ["", "Notes from the original change:", ""] + [f"> {b.splitlines()[0]}" for b in bodies[:10]]
    if any(behaviours.values()):
        lines += ["", "## Expected behaviour (business rules)", "",
                  "Acceptance is verified by automated tests describing these behaviours:", ""]
        for _, items in behaviours.items():
            lines += [f"- {b}" for b in items]
    if hints:
        areas = sorted({_dir_glob(p).removesuffix("/**") for p in f.src_files})
        if areas:
            lines += ["", "## Where it belongs", ""] + [f"- `{a}/`" for a in areas]
    lines += ["", "## How you will be evaluated", ""]
    for c in checks:
        if c.get("enabled", True):
            lines.append(f"- **{c['category']}** · {c.get('description', c['type'])}")
    lines += ["", "Do not modify unrelated code. Keep the change easy to review.", ""]
    return "\n".join(lines)


def build_benchmark(repo: str, f: Feature, profile: RepoProfile, out: Path, role: str | None = None,
                    hints: bool = True) -> tuple[Path, list[str]]:
    warnings: list[str] = []
    bdir = out / f.id
    hidden_dir = bdir / "hidden"
    bdir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- hidden tests
    ref = f.reference_commit
    blobs = gitutil.read_blobs(repo, [(ref, p) for p in f.test_files]) if ref else {}
    hidden, behaviours = [], {}
    for p in f.test_files:
        text = blobs.get((ref, p))
        if text is None:
            continue
        dest = hidden_dir / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        hidden.append({"source": f"hidden/{p}", "target": p})
        behaviours[p] = langs.test_descriptions(p, text)
    runnable_tests = [h["target"] for h in hidden if posixpath.basename(h["target"]) != "conftest.py"]

    checks: list[dict] = []
    # ---------------------------------------------------------------- business rules
    if runnable_tests:
        per_file = len(runnable_tests) <= 8
        groups = [[t] for t in runnable_tests] if per_file else [runnable_tests]
        for group in groups:
            stem = posixpath.splitext(posixpath.basename(group[0]))[0] if per_file else "all"
            n = max(1, len(behaviours.get(group[0], []))) if per_file else 1
            checks.append({"id": f"br-{stem}", "category": "business_rules", "type": "test_command",
                           "weight": n, "description": f"tests in {', '.join(group)}",
                           "params": {"run": profile.test_command(group)}})
    else:
        warnings.append(f"{f.id}: no tests found — added a disabled placeholder business-rule check")
        checks.append({"id": "br-todo", "category": "business_rules", "type": "command", "enabled": False,
                       "description": "TODO: write tests for the business rules of this feature",
                       "params": {"run": "echo 'write business-rule tests' && exit 1"}})

    # ---------------------------------------------------------------- architecture
    th = _ref_threshold(repo, f, profile)
    if profile.layers:
        checks.append({"id": "arch-layers", "category": "architecture", "type": "layer_dependencies", "weight": 2,
                       "description": "inner layers must not import outer layers ("
                                      + " <- ".join(l.name for l in profile.layers) + ")",
                       "params": {"layers": [{"name": l.name, "globs": l.globs} for l in profile.layers]}})
    checks += [
        {"id": "arch-complexity", "category": "architecture", "type": "complexity", "weight": 1,
         "description": f"cyclomatic complexity per new/changed function <= {th['complexity']}",
         "params": {"max": th["complexity"]}},
        {"id": "arch-function-length", "category": "architecture", "type": "function_length", "weight": 0.5,
         "description": f"new/changed functions <= {th['function_length']} lines",
         "params": {"max": th["function_length"]}},
        {"id": "arch-file-length", "category": "architecture", "type": "file_length", "weight": 0.5,
         "description": f"files <= {th['file_lines']} lines", "params": {"max": th["file_lines"]}},
    ]
    ref_lines = _net_diff_lines(repo, f)
    checks.append({"id": "arch-diff-size", "category": "architecture", "type": "diff_size", "weight": 0.5,
                   "description": f"reviewable change: <= {max(100, ref_lines * 2)} changed lines "
                                  f"(reference changed {ref_lines})",
                   "params": {"max_lines": max(100, ref_lines * 2)}})
    allowed_dirs = sorted({_dir_glob(p) for p in f.files})
    checks.append({"id": "arch-file-placement", "category": "architecture", "type": "file_placement", "weight": 0.5,
                   "description": "new files only in the areas the feature belongs to",
                   "params": {"allowed": allowed_dirs + ["**/test/**", "**/tests/**", "**/__tests__/**"]}})
    allowed_deps: list[str] = []
    manifests = [p for p in f.files if deps.is_manifest(p)]
    if manifests and ref:
        before = deps.collect(manifests, lambda p: gitutil.show_file(repo, f.base_commit, p)) if f.base_commit else set()
        after = deps.collect(manifests, lambda p: gitutil.show_file(repo, ref, p))
        allowed_deps = sorted(after - before)
    checks.append({"id": "arch-no-new-deps", "category": "architecture", "type": "no_new_dependencies", "weight": 1,
                   "description": "no new third-party dependencies"
                                  + (f" (except {', '.join(allowed_deps)})" if allowed_deps else ""),
                   "params": {"allow": allowed_deps}})
    patterns = DEBUG_PATTERNS.get(profile.primary_language or "", []) + COMMON_PATTERNS
    checks.append({"id": "arch-no-leftovers", "category": "architecture", "type": "forbidden_patterns", "weight": 0.5,
                   "description": "no debug leftovers or hard-coded secrets in added lines",
                   "params": {"patterns": patterns}})

    # ---------------------------------------------------------------- performance
    if runnable_tests:
        checks.append({"id": "perf-test-suite", "category": "performance", "type": "command_duration", "weight": 1,
                       "description": "feature tests run no slower than the reference x1.5 "
                                      "(proxy — replace with a real load test when you have one)",
                       "params": {"run": profile.test_command(runnable_tests, junit=False),
                                  "repeat": 3, "max_seconds": "auto", "tolerance": 1.5}})
    perf_tests = [p for p in f.perf_files if p in runnable_tests]
    if perf_tests:
        checks.append({"id": "perf-benchmarks", "category": "performance", "type": "command_duration", "weight": 2,
                       "description": "the repository's own performance tests stay within baseline x1.25",
                       "params": {"run": profile.test_command(perf_tests, junit=False),
                                  "repeat": 3, "warmup": 1, "max_seconds": "auto", "tolerance": 1.25}})

    # ---------------------------------------------------------------- snapshot
    snapshot = {"strategy": f.snapshot_strategy, "base_commit": f.base_commit, "reference_commit": ref}
    if f.snapshot_strategy == "revert_commits":
        snapshot["revert_commits"] = f.commits
    if f.snapshot_strategy == "remove_paths":
        snapshot["remove_paths"] = f.module_paths or f.src_files

    (bdir / "task.md").write_text(_task_markdown(f, profile, behaviours, checks, hints))
    (bdir / "clarifications.md").write_text(
        f"# Clarifications — {f.name}\n\n"
        "Questions an agent (or developer) may ask about this task, with the official answers.\n"
        "Agents are allowed — and expected — to ask before guessing; add the answers here so every\n"
        "agent evaluated on this benchmark gets the same information.\n\n"
        "<!-- Q: ...\n     A: ... -->\n")

    data = {
        "schema_version": 1,
        "id": f.id,
        "title": f.name,
        "role": role or _infer_role(f),
        "repo": {"name": profile.name, "path": profile.path},
        "reference": {"source": f.source, "commits": f.commits, "quality": f.quality,
                      "suitability_notes": f.suitability.get("notes", [])},
        "snapshot": snapshot,
        "task": {"prompt": "task.md", "clarifications": "clarifications.md"},
        "hidden_files": hidden,
        "setup": profile.setup_commands(),
        "env": {},
        "timeout_seconds": 600,
        "scoring": {"weights": {"business_rules": 0.5, "architecture": 0.3, "performance": 0.2},
                    "gate": {"category": "business_rules", "min_score": 1.0}},
        "checks": checks,
    }
    body = yaml.dump(data, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=110)
    (bdir / "benchmark.yaml").write_text(HEADER + "\n" + body)
    return bdir, warnings


EXAMPLE_PLUGIN = '''\
"""Example featbench plugin: every *.py in this folder is loaded automatically.

Use it in a benchmark.yaml:
    - id: arch-max-todos
      type: max_todo_comments
      category: architecture
      params: {max: 0}
"""

from featbench.checks import Check, CheckContext, CheckResult, register


@register
class MaxTodoComments(Check):
    type_name = "max_todo_comments"
    default_category = "architecture"
    description = "Limit TODO/FIXME comments added by the agent."
    params_doc = {"max": "maximum number of TODO/FIXME lines added (default 0)"}

    def run(self, ctx: CheckContext) -> CheckResult:
        limit = int(self.param("max", 0))
        hits = []
        for change in ctx.changes:
            text = ctx.read(change.path) or ""
            for n, line in enumerate(text.splitlines(), 1):
                if (change.status == "A" or n in change.added_lines) and ("TODO" in line or "FIXME" in line):
                    hits.append(f"{change.path}:{n}")
        if len(hits) <= limit:
            return self.passed(f"{len(hits)} TODO/FIXME added")
        return self.failed(limit / len(hits), f"{len(hits)} TODO/FIXME added (max {limit})", hits=hits)
'''


def generate(analysis_repos: list[RepoProfile], features: list[Feature], out: str | Path,
             role: str | None = None, hints: bool = True) -> tuple[list[Path], list[str]]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    plugins = out / "plugins"
    plugins.mkdir(exist_ok=True)
    if not (plugins / "example_plugin.py").exists():
        (plugins / "example_plugin.py").write_text(EXAMPLE_PLUGIN)
    profiles = {p.name: p for p in analysis_repos}
    dirs, warnings = [], []
    for f in features:
        profile = profiles[f.repo]
        d, w = build_benchmark(profile.path, f, profile, out, role, hints)
        dirs.append(d)
        warnings += w
    _write_index(out)
    return dirs, warnings


def _write_index(out: Path) -> None:
    rows = []
    for y in sorted(out.glob("*/benchmark.yaml")):
        d = yaml.safe_load(y.read_text())
        q = d.get("reference", {}).get("quality", {})
        rows.append(f"| `{d['id']}` | {d.get('title', '')} | {d.get('role', '')} | {q.get('label', '-')} "
                    f"({q.get('score', '-')}) | {len(d.get('checks', []))} |")
    (out / "README.md").write_text(
        "# Benchmark suite\n\nGenerated by featbench. Edit any `benchmark.yaml` / `task.md` freely.\n\n"
        "| benchmark | title | role | reference quality | checks |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        "## Usage\n\n```bash\n"
        "featbench calibrate <benchmark-dir>             # once: baselines + sanity checks\n"
        "featbench prepare   <benchmark-dir> -w /tmp/ws   # snapshot for the agent\n"
        "# ... run ANY agent inside /tmp/ws with /tmp/ws/.featbench/TASK.md as the prompt ...\n"
        "featbench evaluate  -w /tmp/ws --agent my-agent --cost 0.42 -o results/\n"
        "featbench report results/\n```\n")
