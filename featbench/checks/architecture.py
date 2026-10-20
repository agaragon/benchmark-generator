"""Architecture fitness functions.

By default they look only at what the agent changed (`scope: changed`, and for function metrics
only functions touching added lines), so pre-existing debt in the repo is not blamed on the agent.
"""

from __future__ import annotations

import re

from .. import deps, gitutil, globs, langs
from ..analysis.profile import Layer, RepoProfile
from .base import Check, CheckContext, CheckResult, register

_SCOPE_DOC = {
    "scope": "'changed' (default: only files the agent touched) or 'all'",
    "include": "glob list of files to consider (default: all source files)",
    "exclude": "glob list of files to ignore",
    "include_tests": "also analyse test files (default false)",
}


class _FileCheck(Check):
    default_category = "architecture"

    def targets(self, ctx: CheckContext) -> list[str]:
        scope = self.param("scope", "changed")
        include = self.param("include") or ["**"]
        exclude = self.param("exclude") or []
        tests = self.param("include_tests", False)
        paths = ctx.changed_paths() if scope == "changed" else ctx.all_files()
        return [p for p in paths
                if langs.language_of(p) and not langs.is_ignored(p) and not langs.is_generated(p)
                and globs.match_any(p, include) and not globs.match_any(p, exclude)
                and (tests or not langs.is_test_path(p))]

    def new_code_only(self, ctx: CheckContext) -> bool:
        return self.param("scope", "changed") == "changed" and self.param("new_code_only", True)


def _ratio_result(check: Check, offenders: list, total: int, ok_msg: str, bad_msg: str, **details) -> CheckResult:
    if total == 0 and not offenders:
        return check.skipped("nothing to check in the changed files")
    if not offenders:
        return check.passed(ok_msg, checked=total, **details)
    score = 1 - len(offenders) / max(total, len(offenders))
    return check.failed(score, bad_msg.format(n=len(offenders), total=total),
                        offenders=offenders[:50], checked=total, **details)


@register
class LayerDependencies(_FileCheck):
    type_name = "layer_dependencies"
    description = ("Dependency rule between layers: an inner layer must not import an outer one. "
                   "Layers are listed inner -> outer; or give explicit `forbidden` pairs.")
    params_doc = {"layers": "list of {name, globs}, ordered from inner (domain) to outer (presentation)",
                  "forbidden": "optional list of {from, to} layer-name pairs (overrides the ordering rule)",
                  **_SCOPE_DOC}

    def run(self, ctx: CheckContext) -> CheckResult:
        layers = [Layer(l["name"], l.get("rank", i), l["globs"]) for i, l in enumerate(self.param("layers", []))]
        if len(layers) < 2:
            return self.skipped("fewer than two layers configured")
        profile = RepoProfile(name="", path="", languages={}, primary_language=None, stacks=[], layers=layers)
        explicit = {(f["from"], f["to"]) for f in self.param("forbidden", []) or []}

        def forbidden(a: Layer, b: Layer) -> bool:
            return (a.name, b.name) in explicit if explicit else b.rank > a.rank

        offenders, checked = [], 0
        for path in self.targets(ctx):
            src = profile.layer_of(path)
            text = ctx.read(path)
            if not src or text is None:
                continue
            checked += 1
            for imp in langs.extract_imports(path, text):
                dst = profile.layer_of(imp)
                if dst and dst.name != src.name and forbidden(src, dst):
                    offenders.append(f"{path} [{src.name}] imports {imp} [{dst.name}]")
        bad_files = sorted({o.split(" [")[0] for o in offenders})
        if checked == 0:
            return self.skipped("no changed file belongs to a layer")
        if not offenders:
            return self.passed(f"{checked} file(s) respect the layer rules", checked=checked)
        return self.failed(1 - len(bad_files) / max(checked, 1),
                           f"{len(offenders)} forbidden import(s) in {len(bad_files)} file(s)",
                           offenders=offenders[:50], checked=checked)


@register
class ForbiddenImports(_FileCheck):
    type_name = "forbidden_imports"
    description = "Files matching `files` must not import modules matching `forbid`."
    params_doc = {"rules": "list of {files: [globs], forbid: [globs], message}", **_SCOPE_DOC}

    def run(self, ctx: CheckContext) -> CheckResult:
        offenders, checked = [], 0
        for path in self.targets(ctx):
            rules = [r for r in self.param("rules", []) if globs.match_any(path, r.get("files", ["**"]))]
            if not rules:
                continue
            text = ctx.read(path) or ""
            checked += 1
            for imp in langs.extract_imports(path, text):
                for r in rules:
                    if globs.match_any(imp, r["forbid"]):
                        offenders.append(f"{path} imports {imp}" + (f" ({r['message']})" if r.get("message") else ""))
        return _ratio_result(self, offenders, checked, f"{checked} file(s) checked, no forbidden imports",
                             "{n} forbidden import(s)")


class _FunctionMetricCheck(_FileCheck):
    metric = ""
    params_doc = {"max": "maximum allowed value per function",
                  "new_code_only": "only functions that contain added lines (default true)", **_SCOPE_DOC}

    def value(self, fn: langs.FunctionMetric) -> int:
        return getattr(fn, self.metric)

    def run(self, ctx: CheckContext) -> CheckResult:
        limit = int(self.param("max", 10))
        new_only = self.new_code_only(ctx)
        offenders, total = [], 0
        for path in self.targets(ctx):
            text = ctx.read(path)
            if text is None:
                continue
            change = ctx.change(path)
            for fn in langs.function_metrics(path, text):
                if new_only and change and change.status != "A" and not any(
                        fn.start <= ln <= fn.end for ln in change.added_lines):
                    continue
                total += 1
                if self.value(fn) > limit:
                    offenders.append(f"{path}:{fn.start} {fn.name} = {self.value(fn)}")
        return _ratio_result(self, offenders, total, f"{total} function(s) within limit {limit}",
                             "{n} of {total} function(s) above " + str(limit), limit=limit)


@register
class Complexity(_FunctionMetricCheck):
    type_name = "complexity"
    metric = "complexity"
    description = "Cyclomatic complexity per function (exact for Python, approximate elsewhere)."


@register
class FunctionLength(_FunctionMetricCheck):
    type_name = "function_length"
    metric = "length"
    description = "Number of lines per function."


@register
class FileLength(_FileCheck):
    type_name = "file_length"
    description = "Files must not grow beyond `max` lines (files already above it may not grow)."
    params_doc = {"max": "maximum lines per file", **_SCOPE_DOC}

    def run(self, ctx: CheckContext) -> CheckResult:
        limit = int(self.param("max", 400))
        offenders, files = [], self.targets(ctx)
        for path in files:
            text = ctx.read(path)
            if text is None:
                continue
            if langs.is_generated(path, text):
                continue
            n = text.count("\n") + 1
            before = ctx.read_snapshot(path)
            n_before = before.count("\n") + 1 if before is not None else 0
            if n > limit and n > n_before:
                offenders.append(f"{path} ({n} lines)")
        return _ratio_result(self, offenders, len(files), f"{len(files)} file(s) within {limit} lines",
                             "{n} file(s) above " + str(limit) + " lines", limit=limit)


@register
class DiffSize(Check):
    type_name = "diff_size"
    default_category = "architecture"
    description = ("Reviewability budget: total changed lines. Full score up to `max_lines`, "
                   "decreasing linearly to zero at twice the budget.")
    params_doc = {"max_lines": "line budget (added + deleted)", "exclude": "globs to ignore (e.g. lock files)",
                  "exclude_tests": "ignore test files (default false)"}

    def run(self, ctx: CheckContext) -> CheckResult:
        budget = int(self.param("max_lines", 500))
        exclude = self.param("exclude") or []
        lines = sum(c.added + c.deleted for c in ctx.changes
                    if not globs.match_any(c.path, exclude) and not langs.is_noise(c.path)
                    and not (self.param("exclude_tests", False) and langs.is_test_path(c.path)))
        details = {"changed_lines": lines, "budget": budget, "files": len(ctx.changes)}
        if lines <= budget:
            return self.passed(f"{lines} changed lines (budget {budget})", **details)
        return self.failed(1 - (lines - budget) / budget, f"{lines} changed lines exceed budget {budget}", **details)


@register
class FilePlacement(Check):
    type_name = "file_placement"
    default_category = "architecture"
    description = "New files must be created under the allowed locations."
    params_doc = {"allowed": "glob list of allowed locations for new files", "ignore": "globs never checked"}

    def run(self, ctx: CheckContext) -> CheckResult:
        allowed = self.param("allowed") or ["**"]
        ignore = self.param("ignore") or []
        new = [c.path for c in ctx.changes if c.status == "A" and not globs.match_any(c.path, ignore)]
        offenders = [p for p in new if not globs.match_any(p, allowed)]
        return _ratio_result(self, offenders, len(new), f"{len(new)} new file(s) in allowed locations",
                             "{n} of {total} new file(s) outside allowed locations")


@register
class NoNewDependencies(Check):
    type_name = "no_new_dependencies"
    default_category = "architecture"
    description = "No third-party dependency may be added to manifests unless listed in `allow`."
    params_doc = {"allow": "dependency names that may be added"}

    def run(self, ctx: CheckContext) -> CheckResult:
        allow = set(self.param("allow") or [])
        snap_files = gitutil.ls_files(ctx.workspace, ctx.snapshot_ref)
        before = deps.collect(snap_files, ctx.read_snapshot)
        after = deps.collect(ctx.all_files(), ctx.read)
        added = sorted(after - before - allow)
        if not added:
            return self.passed("no new dependencies")
        return self.failed(0.0, f"new dependencies: {', '.join(added)}", added=added)


@register
class ForbiddenPatterns(_FileCheck):
    type_name = "forbidden_patterns"
    description = "Regexes that must not appear in lines added by the agent (debug leftovers, secrets, ...)."
    params_doc = {"patterns": "list of {regex, message}", **_SCOPE_DOC}

    def run(self, ctx: CheckContext) -> CheckResult:
        rules = [(re.compile(p["regex"]), p.get("message", p["regex"])) for p in self.param("patterns", [])]
        hits = []
        files = self.targets(ctx)
        for path in files:
            text = ctx.read(path)
            change = ctx.change(path)
            if text is None or change is None:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if change.status != "A" and i not in change.added_lines:
                    continue
                for rx, msg in rules:
                    if rx.search(line):
                        hits.append(f"{path}:{i} {msg}: {line.strip()[:120]}")
        if not files:
            return self.skipped("no changed source files")
        if not hits:
            return self.passed(f"{len(files)} file(s) clean")
        return self.failed(max(0.0, 1 - 0.2 * len(hits)), f"{len(hits)} forbidden pattern hit(s)", hits=hits[:50])
