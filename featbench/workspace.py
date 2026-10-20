"""Build the snapshot workspace the agent works in.

The workspace is a *fresh* git repository containing only the snapshot tree (no history), so the
agent cannot peek at the reference implementation through `git log`. The snapshot is committed and
tagged `featbench-snapshot`, which is what evaluation diffs against.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import gitutil
from .benchmark import Benchmark
from .checks import SNAPSHOT_TAG

META_DIR = ".featbench"


def _clear(dest: Path, keep: set[str]) -> None:
    for child in dest.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _remove(dest: Path, rel: str) -> None:
    target = dest / rel
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def prepare(bench: Benchmark, dest: str | Path, repo: str | None = None,
            apply_reference: bool = False, force: bool = False) -> Path:
    """Create the workspace. With `apply_reference`, the reference implementation is laid on top of the
    snapshot as uncommitted changes (used by `calibrate` to score the human baseline)."""
    dest = Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise SystemExit(f"error: workspace {dest} is not empty (use --force to overwrite)")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    repo = repo or bench.repo_path
    if not gitutil.is_git_repo(repo):
        raise SystemExit(f"error: source repository not found: {repo} (use --repo to point to it)")
    snap = bench.snapshot
    strategy = snap["strategy"]
    reference = snap.get("reference_commit")

    if strategy == "base_commit":
        gitutil.extract_tree(repo, snap["base_commit"], dest)
    elif strategy == "remove_paths":
        gitutil.extract_tree(repo, reference, dest)
        for rel in snap.get("remove_paths", []):
            _remove(dest, rel)
    elif strategy == "revert_commits":
        gitutil.extract_tree(repo, reference, dest)
        gitutil.run_git(dest, "init", "-q")
        for sha in reversed(snap.get("revert_commits", [])):
            patch = gitutil.commit_patch(repo, sha)
            if not patch.strip():
                continue
            try:
                gitutil.run_git(dest, "apply", "-R", "--whitespace=nowarn", "-", input_bytes=patch)
            except gitutil.GitError as e:
                raise SystemExit(
                    f"error: could not revert {sha[:10]} on top of {reference[:10]}: {e}\n"
                    "hint: switch snapshot.strategy to 'base_commit' or 'remove_paths' in benchmark.yaml")
        shutil.rmtree(dest / ".git")

    for h in bench.hidden_files:
        _remove(dest, h["target"])

    meta = dest / META_DIR
    meta.mkdir(exist_ok=True)
    (meta / "TASK.md").write_text(bench.task_text())
    clar = bench.clarifications_text()
    if clar:
        (meta / "CLARIFICATIONS.md").write_text(clar)
    (meta / "benchmark.json").write_text(json.dumps(
        {"id": bench.id, "benchmark_dir": str(bench.dir), "reference_applied": apply_reference}, indent=2))

    gitutil.run_git(dest, "init", "-q")
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(exclude.read_text() + f"\n{META_DIR}/\n" if exclude.exists() else f"{META_DIR}/\n")
    gitutil.init_snapshot_repo(dest, f"featbench snapshot for {bench.id}", SNAPSHOT_TAG)

    if apply_reference:
        _clear(dest, {".git", META_DIR})
        gitutil.extract_tree(repo, reference or snap["base_commit"], dest)
    return dest


def detect_benchmark(workspace: str | Path) -> str | None:
    f = Path(workspace) / META_DIR / "benchmark.json"
    if f.exists():
        return json.loads(f.read_text()).get("benchmark_dir")
    return None
