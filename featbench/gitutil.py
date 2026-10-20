"""Thin wrappers around the git CLI."""

from __future__ import annotations

import io
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

REC = "\x1e"
FLD = "\x1f"


class GitError(RuntimeError):
    pass


def run_git(repo: str | Path, *args: str, input_bytes: bytes | None = None,
            check: bool = True, text: bool = True):
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"`git {' '.join(args)}` failed in {repo}: {err}")
    if text:
        return proc.stdout.decode("utf-8", errors="replace")
    return proc.stdout


def is_git_repo(path: str | Path) -> bool:
    try:
        run_git(path, "rev-parse", "--git-dir")
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


@dataclass
class FileStat:
    path: str
    added: int
    deleted: int


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: str
    date: str
    subject: str
    body: str
    files: list[FileStat] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def lines_changed(self) -> int:
        return sum(f.added + f.deleted for f in self.files)


def _parse_numstat(text: str) -> list[FileStat]:
    stats = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, p = parts
        stats.append(FileStat(p, int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0))
    return stats


def log_commits(repo: str | Path, rev: str = "HEAD") -> list[Commit]:
    """All commits reachable from `rev`, oldest first."""
    fmt = f"{REC}%H{FLD}%P{FLD}%an{FLD}%aI{FLD}%s{FLD}%b{FLD}"
    out = run_git(repo, "log", rev, f"--format={fmt}", "--numstat", "--no-renames")
    commits = []
    for rec in out.split(REC)[1:]:
        parts = rec.split(FLD, 6)
        if len(parts) < 7:
            continue
        sha, parents, author, date, subject, body, numstat = parts
        commits.append(Commit(sha, parents.split(), author, date, subject.strip(), body.strip(),
                              _parse_numstat(numstat)))
    commits.reverse()
    return commits


def rev_list(repo: str | Path, *args: str) -> list[str]:
    out = run_git(repo, "rev-list", *args)
    return [line for line in out.splitlines() if line]


def ls_files(repo: str | Path, rev: str | None = None) -> list[str]:
    if rev:
        out = run_git(repo, "ls-tree", "-r", "--name-only", rev)
    else:
        out = run_git(repo, "ls-files")
    return [line for line in out.splitlines() if line]


def read_blobs(repo: str | Path, specs: list[tuple[str, str]]) -> dict[tuple[str, str], str | None]:
    """Read many `rev:path` blobs at once with `git cat-file --batch`."""
    result: dict[tuple[str, str], str | None] = {}
    if not specs:
        return result
    payload = "".join(f"{rev}:{path}\n" for rev, path in specs).encode()
    raw = run_git(repo, "cat-file", "--batch", input_bytes=payload, text=False)
    pos = 0
    for spec in specs:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode(errors="replace")
        pos = nl + 1
        if header.endswith("missing") or header.endswith("ambiguous"):
            result[spec] = None
            continue
        parts = header.split()
        size = int(parts[2])
        kind = parts[1]
        content = raw[pos:pos + size]
        pos += size + 1  # trailing newline
        result[spec] = content.decode("utf-8", errors="replace") if kind == "blob" else None
    return result


def show_file(repo: str | Path, rev: str, path: str) -> str | None:
    return read_blobs(repo, [(rev, path)]).get((rev, path))


def extract_tree(repo: str | Path, rev: str, dest: str | Path) -> None:
    """Export the tree of `rev` into `dest` (no git history is copied)."""
    data = run_git(repo, "archive", "--format=tar", rev, text=False)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def commit_patch(repo: str | Path, sha: str) -> bytes:
    return run_git(repo, "show", "--format=", "--binary", "--no-renames", sha, text=False)


def diff_numstat(repo: str | Path, a: str, b: str) -> list[FileStat]:
    return _parse_numstat(run_git(repo, "diff", "--numstat", "--no-renames", a, b))


def added_lines(repo: str | Path, a: str, b: str, paths: list[str]) -> dict[str, set[int]]:
    """Line numbers (in `b`) added or modified between `a` and `b`, per path."""
    import re
    result: dict[str, set[int]] = {p: set() for p in paths}
    if not paths:
        return result
    out = run_git(repo, "diff", "-U0", "--no-renames", "--no-color", a, b, "--", *paths)
    current = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and current in result:
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                result[current].update(range(start, start + count))
    return result


def init_snapshot_repo(path: str | Path, message: str, tag: str) -> None:
    run_git(path, "init", "-q")
    run_git(path, "add", "-A")
    run_git(path, "-c", "user.name=featbench", "-c", "user.email=featbench@localhost",
            "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    run_git(path, "tag", "-f", tag)
