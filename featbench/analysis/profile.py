"""Repository profile: languages, stack, test runner, architectural layers and code-metric baselines."""

from __future__ import annotations

import json
import posixpath
import shlex
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import gitutil, globs, langs

# Canonical layers, from the innermost (rank 0) to the outermost. An inner layer must not
# depend on an outer one (the dependency rule of Clean/Hexagonal/Onion architectures).
LAYER_ALIASES: dict[str, tuple[int, set[str]]] = {
    "domain": (0, {"domain", "entities", "entity", "model", "models", "core"}),
    "application": (1, {"application", "app_services", "usecases", "use_cases", "usecase",
                        "services", "service", "interactors"}),
    "infrastructure": (2, {"infrastructure", "infra", "adapters", "adapter", "repositories",
                           "repository", "persistence", "db", "database", "gateways", "clients"}),
    "presentation": (3, {"api", "controllers", "controller", "handlers", "routes", "web", "http",
                         "views", "ui", "presentation", "cli", "endpoints", "resources", "pages",
                         "components"}),
}


@dataclass
class Layer:
    name: str
    rank: int
    globs: list[str]


@dataclass
class RepoProfile:
    name: str
    path: str
    languages: dict[str, int]
    primary_language: str | None
    stacks: list[str]
    js_test_runner: str | None = None
    layers: list[Layer] = field(default_factory=list)
    thresholds: dict[str, int] = field(default_factory=dict)
    has_git: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RepoProfile":
        d = dict(d)
        d["layers"] = [Layer(**layer) for layer in d.get("layers", [])]
        return cls(**d)

    # ------------------------------------------------------------------ helpers

    def layer_of(self, path: str) -> Layer | None:
        # Most specific (longest) match wins, e.g. src/api/domain/... -> domain
        best, best_pos = None, -1
        parts = path.lower().split("/")
        for layer in self.layers:
            for g in layer.globs:
                if globs.match(path, g):
                    seg = g.strip("*/").lower()
                    pos = max((i for i, p in enumerate(parts) if p == seg), default=0)
                    if pos > best_pos:
                        best, best_pos = layer, pos
        return best

    def test_command(self, test_files: list[str], junit: bool = True) -> str:
        files = " ".join(shlex.quote(f) for f in test_files)
        lang = self.primary_language
        if "python" in self.stacks or lang == "python":
            junit_arg = " --junitxml={junit_xml}" if junit else ""
            return f"{{python}} -m pytest -q -p no:cacheprovider{junit_arg} {files}".strip()
        if "node" in self.stacks:
            runner = self.js_test_runner
            if runner == "vitest":
                return f"npx vitest run {files}"
            if runner == "jest":
                return f"npx jest {files}"
            if runner == "mocha":
                return f"npx mocha {files}"
            return "npm test"
        if "go" in self.stacks:
            pkgs = sorted({"./" + (posixpath.dirname(f) or ".") for f in test_files})
            return "go test " + " ".join(pkgs)
        classes = sorted({posixpath.splitext(posixpath.basename(f))[0] for f in test_files})
        if "maven" in self.stacks:
            return "mvn -q test -Dtest=" + ",".join(classes)
        if "gradle" in self.stacks:
            return "./gradlew test " + " ".join(f"--tests '*{c}'" for c in classes)
        if "cargo" in self.stacks:
            return "cargo test"
        if "dotnet" in self.stacks:
            return "dotnet test"
        if "ruby" in self.stacks:
            return f"bundle exec rspec {files}"
        if "php" in self.stacks:
            return f"vendor/bin/phpunit {files}"
        return "echo 'TODO: configure the test command for this repository' && exit 1"

    def setup_commands(self) -> list[str]:
        cmds = []
        if "node" in self.stacks:
            cmds.append("npm install --no-audit --no-fund")
        if "go" in self.stacks:
            cmds.append("go mod download")
        return cmds


def _percentile(values: list[int], pct: float, default: int) -> int:
    if not values:
        return default
    values = sorted(values)
    idx = min(len(values) - 1, int(round(pct * (len(values) - 1))))
    return values[idx]


def _detect_stacks(files: list[str], read) -> tuple[list[str], str | None]:
    names = {posixpath.basename(f) for f in files if f.count("/") <= 1}
    stacks = []
    js_runner = None
    if names & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "pytest.ini", "conftest.py"}:
        stacks.append("python")
    if "package.json" in names:
        stacks.append("node")
        text = read("package.json") or "{}"
        try:
            pkg = json.loads(text)
        except ValueError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        js_runner = next((r for r in ("vitest", "jest", "mocha") if r in deps), None)
    if "go.mod" in names:
        stacks.append("go")
    if "pom.xml" in names:
        stacks.append("maven")
    if names & {"build.gradle", "build.gradle.kts"}:
        stacks.append("gradle")
    if "Cargo.toml" in names:
        stacks.append("cargo")
    if any(f.endswith((".csproj", ".sln")) for f in files):
        stacks.append("dotnet")
    if "Gemfile" in names:
        stacks.append("ruby")
    if "composer.json" in names:
        stacks.append("php")
    return stacks, js_runner


def _detect_layers(source_files: list[str]) -> list[Layer]:
    found: dict[str, set[str]] = {}
    for f in source_files:
        for part in f.split("/")[:-1]:
            low = part.lower()
            for layer, (_, aliases) in LAYER_ALIASES.items():
                if low in aliases:
                    found.setdefault(layer, set()).add(part)
    if len(found) < 2:
        return []
    return [Layer(name, LAYER_ALIASES[name][0], sorted(f"**/{d}/**" for d in dirs))
            for name, dirs in sorted(found.items(), key=lambda kv: LAYER_ALIASES[kv[0]][0])]


def profile_repo(path: str | Path, name: str | None = None, max_files: int = 1500) -> RepoProfile:
    path = Path(path).resolve()
    name = name or path.name
    has_git = gitutil.is_git_repo(path)
    if has_git:
        files = [f for f in gitutil.ls_files(path) if not langs.is_ignored(f)]
    else:
        files = [str(p.relative_to(path)).replace("\\", "/") for p in path.rglob("*")
                 if p.is_file() and not langs.is_ignored(str(p.relative_to(path)))]

    def read(rel: str) -> str | None:
        try:
            return (path / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    source = [f for f in files if langs.is_source_path(f) and not langs.is_generated(f)]
    lang_counts = Counter(l for f in source if (l := langs.language_of(f)))
    stacks, js_runner = _detect_stacks(files, read)

    complexities, lengths, file_lines = [], [], []
    for f in source[:max_files]:
        text = read(f)
        if text is None or langs.is_generated(f, text):
            continue
        file_lines.append(text.count("\n") + 1)
        for fn in langs.function_metrics(f, text):
            complexities.append(fn.complexity)
            lengths.append(fn.length)

    thresholds = {
        "complexity": max(10, _percentile(complexities, 0.90, 10)),
        "function_length": max(50, _percentile(lengths, 0.90, 50)),
        "file_lines": max(400, _percentile(file_lines, 0.95, 400)),
    }
    return RepoProfile(
        name=name, path=str(path), languages=dict(lang_counts.most_common()),
        primary_language=lang_counts.most_common(1)[0][0] if lang_counts else None,
        stacks=stacks, js_test_runner=js_runner, layers=_detect_layers(source),
        thresholds=thresholds, has_git=has_git,
    )
