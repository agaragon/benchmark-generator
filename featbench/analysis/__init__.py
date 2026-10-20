"""Repository analysis: suggest features and rate how well each one was implemented."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .. import gitutil
from .features import Feature, discover_features
from .profile import RepoProfile, profile_repo
from .quality import evaluate_features, summarize

__all__ = ["analyze", "load_analysis", "render_markdown", "Feature", "RepoProfile"]


def analyze(repo_paths: list[str]) -> dict:
    repos, features = [], []
    used_names: set[str] = set()
    for raw in repo_paths:
        path = Path(raw).resolve()
        if not gitutil.is_git_repo(path):
            raise SystemExit(f"error: {path} is not a git repository (history is needed to build snapshots)")
        name = path.name
        n = 2
        while name in used_names:
            name = f"{path.name}{n}"
            n += 1
        used_names.add(name)
        profile = profile_repo(path, name)
        feats = discover_features(str(path), name)
        evaluate_features(str(path), feats, profile)
        repos.append(profile.to_dict())
        features += feats
    features.sort(key=lambda f: (-f.suitability["score"], -f.quality["score"], f.id))
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "repos": repos,
        "summary": summarize(features),
        "features": [f.to_dict() for f in features],
    }


def load_analysis(path: str | Path) -> tuple[list[RepoProfile], list[Feature]]:
    data = json.loads(Path(path).read_text())
    return ([RepoProfile.from_dict(r) for r in data["repos"]],
            [Feature.from_dict(f) for f in data["features"]])


_ICON = {"clean": "🟢 clean", "mixed": "🟡 mixed", "chaotic": "🔴 chaotic"}


def render_markdown(analysis: dict) -> str:
    lines = ["# featbench — feature analysis", "", f"Generated at {analysis['generated_at']}", ""]
    for r in analysis["repos"]:
        layers = ", ".join(f"{l['name']}({'/'.join(g.strip('*/') for g in l['globs'])})" for l in r["layers"]) or "none detected"
        lines += [f"## Repository `{r['name']}`", "",
                  f"- path: `{r['path']}`",
                  f"- languages: {', '.join(f'{k} ({v})' for k, v in r['languages'].items()) or '-'}",
                  f"- stacks: {', '.join(r['stacks']) or '-'}",
                  f"- architectural layers: {layers}",
                  f"- repo thresholds (p90/p95): {r['thresholds']}", ""]
    lines += ["## Suggested features", "",
              "| # | id | source | quality | suitability | commits | +lines | tests | recommended |",
              "|---|----|--------|---------|-------------|---------|--------|-------|-------------|"]
    for i, f in enumerate(analysis["features"], 1):
        q, s, m = f["quality"], f["suitability"], f["metrics"]
        lines.append(f"| {i} | `{f['id']}` | {f['source']} | {_ICON.get(q['label'], q['label'])} ({q['score']}) "
                     f"| {s['score']} | {m['commits']} | {m['lines_added']} | {m['test_files']} "
                     f"| {'✅' if s['recommended'] else '—'} |")
    lines.append("")
    for f in analysis["features"]:
        q, s = f["quality"], f["suitability"]
        lines += [f"### `{f['id']}` — {f['name']}", "",
                  f"Quality: **{q['label']}** ({q['score']}/100) · snapshot: `{f['snapshot_strategy']}` "
                  f"· base `{(f['base_commit'] or '-')[:10]}` → reference `{(f['reference_commit'] or '-')[:10]}`", ""]
        if q["strengths"]:
            lines += ["Strengths:"] + [f"- {x}" for x in q["strengths"]] + [""]
        if q["issues"]:
            lines += ["Issues:"] + [f"- {x}" for x in q["issues"]] + [""]
        if s["notes"]:
            lines += ["Benchmark notes:"] + [f"- {x}" for x in s["notes"]] + [""]
        lines += ["Commits:"] + [f"- {x}" for x in f["subjects"][:15]] + [""]
    return "\n".join(lines)
