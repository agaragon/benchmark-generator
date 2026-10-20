"""Benchmark definition (benchmark.yaml) loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .checks import CATEGORIES, REGISTRY, load_plugins

DEFAULT_WEIGHTS = {"business_rules": 0.5, "architecture": 0.3, "performance": 0.2}
STRATEGIES = ("base_commit", "revert_commits", "remove_paths")


class BenchmarkError(ValueError):
    pass


@dataclass
class Benchmark:
    dir: Path
    data: dict

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "Benchmark":
        p = Path(path).resolve()
        file = p / "benchmark.yaml" if p.is_dir() else p
        if not file.exists():
            raise BenchmarkError(f"benchmark file not found: {file}")
        data = yaml.safe_load(file.read_text()) or {}
        bench = cls(file.parent, data)
        load_plugins(bench.plugin_dirs)
        bench.validate()
        return bench

    @property
    def plugin_dirs(self) -> list[Path]:
        return [self.dir / "plugins", self.dir.parent / "plugins"]

    # ------------------------------------------------------------------ accessors
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def title(self) -> str:
        return self.data.get("title", self.id)

    @property
    def role(self) -> str:
        return self.data.get("role", "backend")

    @property
    def repo_path(self) -> str:
        return self.data["repo"]["path"]

    @property
    def snapshot(self) -> dict:
        return self.data["snapshot"]

    @property
    def checks(self) -> list[dict]:
        return self.data.get("checks", [])

    @property
    def setup(self) -> list[str]:
        return self.data.get("setup", []) or []

    @property
    def env(self) -> dict:
        return self.data.get("env", {}) or {}

    @property
    def timeout(self) -> int:
        return int(self.data.get("timeout_seconds", 600))

    @property
    def hidden_files(self) -> list[dict]:
        return self.data.get("hidden_files", []) or []

    @property
    def weights(self) -> dict[str, float]:
        return {**DEFAULT_WEIGHTS, **(self.data.get("scoring", {}).get("weights") or {})}

    @property
    def gate(self) -> dict:
        return self.data.get("scoring", {}).get("gate") or {"category": "business_rules", "min_score": 1.0}

    def task_text(self) -> str:
        f = self.dir / self.data.get("task", {}).get("prompt", "task.md")
        return f.read_text() if f.exists() else f"# {self.title}\n"

    def clarifications_text(self) -> str | None:
        name = self.data.get("task", {}).get("clarifications")
        f = self.dir / name if name else None
        return f.read_text() if f and f.exists() else None

    @property
    def calibration_file(self) -> Path:
        return self.dir / "calibration.json"

    def calibration(self) -> dict:
        if self.calibration_file.exists():
            return json.loads(self.calibration_file.read_text())
        return {}

    # ------------------------------------------------------------------ validation
    def validate(self) -> None:
        errors = []
        for key in ("id", "repo", "snapshot", "checks"):
            if key not in self.data:
                errors.append(f"missing key '{key}'")
        snap = self.data.get("snapshot", {})
        strategy = snap.get("strategy")
        if strategy not in STRATEGIES:
            errors.append(f"snapshot.strategy must be one of {STRATEGIES}")
        if strategy == "base_commit" and not snap.get("base_commit"):
            errors.append("snapshot.base_commit is required for strategy base_commit")
        if strategy in ("revert_commits", "remove_paths") and not snap.get("reference_commit"):
            errors.append(f"snapshot.reference_commit is required for strategy {strategy}")
        ids = set()
        for c in self.data.get("checks", []):
            if "id" not in c or "type" not in c:
                errors.append(f"check without id/type: {c}")
                continue
            if c["id"] in ids:
                errors.append(f"duplicate check id '{c['id']}'")
            ids.add(c["id"])
            if c["type"] not in REGISTRY:
                errors.append(f"check '{c['id']}': unknown type '{c['type']}' (known: {', '.join(sorted(REGISTRY))})")
                continue
            cat = c.get("category") or REGISTRY[c["type"]].default_category
            if cat not in CATEGORIES and cat not in self.weights:
                errors.append(f"check '{c['id']}': category '{cat}' has no weight in scoring.weights")
        for h in self.hidden_files:
            if not (self.dir / h["source"]).exists():
                errors.append(f"hidden file not found: {h['source']}")
        if errors:
            raise BenchmarkError(f"invalid benchmark {self.dir}:\n  - " + "\n  - ".join(errors))


def find_benchmarks(path: str | Path) -> list[Path]:
    p = Path(path)
    if (p / "benchmark.yaml").exists():
        return [p]
    return sorted(f.parent for f in p.rglob("benchmark.yaml"))
