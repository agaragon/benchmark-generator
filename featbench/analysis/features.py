"""Discover candidate features from git history.

Strategies, in priority order:
  1. merged branches / pull requests   ("Merge pull request #12 from org/feature/login")
  2. conventional-commit scopes         ("feat(login): ...", "fix(login): ...")
  3. ticket ids                          ("ABC-123 add login")
  4. unscoped `feat:` commits            (one feature per commit)
  5. module fallback                     (one feature per source module) when history has no conventions
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .. import gitutil, langs

CONV_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<desc>.+)")
FEAT_TYPES = {"feat", "feature"}
FIX_RE = re.compile(r"\b(fix|fixes|fixed|fixing|bug|bugfix|hotfix|patch|oops|regression|broken|crash|wip)\b", re.I)
REVERT_RE = re.compile(r"^revert\b", re.I)
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
MERGE_RES = [
    re.compile(r"Merge pull request #\d+ from [^/\s]+/(\S+)"),
    re.compile(r"Merge branch '([^']+)'"),
    re.compile(r"Merged in (\S+)"),
    re.compile(r"Merge remote-tracking branch '(?:[^/']+/)?([^']+)'"),
]
MAINLINE = re.compile(r"^(?:origin/|upstream/)?(main|master|develop|development|dev|trunk|next|stable|"
                      r"release.*|staging|production|gh-pages|hotfixes)$", re.I)
MAX_BRANCH_COMMITS = 150   # longer-lived branches are integration branches, not features
BRANCH_PREFIX = re.compile(r"^(?:feature|feat|features|story|topic|task)[/_-]", re.I)
GENERIC_ROOTS = {"src", "lib", "app", "pkg", "internal", "source", "main", "java", "kotlin",
                 "packages", "modules", "apps", "com", "org", "net", "io"}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:60] or "feature"


def is_fix(subject: str) -> bool:
    m = CONV_RE.match(subject)
    if m and m.group("type").lower() in {"fix", "hotfix", "bugfix", "revert"}:
        return True
    return bool(FIX_RE.search(subject) or REVERT_RE.search(subject))


@dataclass
class Feature:
    id: str
    repo: str
    name: str
    source: str
    commits: list[str]
    merge_commit: str | None = None
    base_commit: str | None = None
    reference_commit: str | None = None
    snapshot_strategy: str = "base_commit"
    module_paths: list[str] = field(default_factory=list)
    files: dict[str, dict] = field(default_factory=dict)
    src_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    perf_files: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    first_date: str = ""
    last_date: str = ""
    authors: list[str] = field(default_factory=list)
    rework_commits: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    suitability: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Feature":
        return cls(**d)


def _branch_name(subject: str) -> str | None:
    for rx in MERGE_RES:
        m = rx.search(subject)
        if m:
            branch = m.group(1).strip()
            if " into " in branch:
                branch = branch.split(" into ")[0]
            if MAINLINE.match(branch):
                return None
            return branch
    return None


def _group_commits(repo: str, commits: list[gitutil.Commit]) -> list[dict]:
    by_sha = {c.sha: c for c in commits}
    order = {c.sha: i for i, c in enumerate(commits)}
    assigned: set[str] = set()
    groups: list[dict] = []

    # 1. merged branches
    for c in commits:
        if len(c.parents) < 2:
            continue
        branch = _branch_name(c.subject)
        if not branch:
            continue
        shas = [s for s in gitutil.rev_list(repo, c.parents[1], "--not", c.parents[0])
                if s in by_sha and s not in assigned]
        if not shas or len(shas) > MAX_BRANCH_COMMITS:
            continue
        shas.sort(key=order.get)
        name = BRANCH_PREFIX.sub("", branch)
        assigned.update(shas)
        assigned.add(c.sha)
        if FIX_RE.search(name.replace("/", " ").replace("_", " ").replace("-", " ")):
            continue  # bugfix branches are not features
        groups.append({"key": f"merge:{slugify(name)}", "source": "merge", "name": name,
                       "commits": shas, "merge_commit": c.sha})

    # 2-4. scopes, tickets, unscoped feat commits
    keyed: dict[str, dict] = {}
    for c in commits:
        if c.sha in assigned or c.is_merge:
            continue
        m = CONV_RE.match(c.subject)
        ctype = m.group("type").lower() if m else ""
        ticket = TICKET_RE.search(c.subject)
        if m and m.group("scope"):
            key, source, name = f"scope:{slugify(m.group('scope'))}", "scope", m.group("scope")
        elif ticket:
            key, source, name = f"ticket:{ticket.group(1)}", "ticket", ticket.group(1)
        elif ctype in FEAT_TYPES:
            key, source, name = f"commit:{c.sha[:8]}", "commit", m.group("desc")
        else:
            continue
        g = keyed.setdefault(key, {"key": key, "source": source, "name": name, "commits": [],
                                   "merge_commit": None, "has_feat": False})
        g["commits"].append(c.sha)
        g["has_feat"] = g["has_feat"] or ctype in FEAT_TYPES or source == "ticket"
        assigned.add(c.sha)
    groups += [g for g in keyed.values() if g.pop("has_feat")]
    return groups


def _module_groups(repo: str, commits: list[gitutil.Commit]) -> list[dict]:
    """Fallback: treat each source module (directory) as a feature."""
    head_files = [f for f in gitutil.ls_files(repo) if langs.is_source_path(f)]
    modules: dict[str, list[str]] = defaultdict(list)
    for f in head_files:
        parts = f.split("/")[:-1]
        while parts and parts[0].lower() in GENERIC_ROOTS:
            parts = parts[1:]
        if not parts:
            continue
        prefix_len = f.split("/").index(parts[0]) + 1
        modules["/".join(f.split("/")[:prefix_len])].append(f)
    groups = []
    for mod, files in modules.items():
        if len(files) < 2:
            continue
        shas = [c.sha for c in commits if not c.is_merge and c.files and
                sum(1 for fs in c.files if fs.path.startswith(mod + "/")) * 2 >= len(c.files)]
        if shas:
            groups.append({"key": f"module:{slugify(mod)}", "source": "module", "name": mod,
                           "commits": shas, "merge_commit": None, "module": mod})
    return groups


def discover_features(repo: str, repo_name: str, min_lines: int = 5) -> list[Feature]:
    commits = gitutil.log_commits(repo)
    if not commits:
        return []
    by_sha = {c.sha: c for c in commits}
    order = {c.sha: i for i, c in enumerate(commits)}
    groups = _group_commits(repo, commits)
    if len(groups) < 2:
        groups += _module_groups(repo, commits)

    features: list[Feature] = []
    seen_ids: set[str] = set()
    for g in groups:
        cs = [by_sha[s] for s in g["commits"]]
        files: dict[str, dict] = {}
        for c in cs:
            for fs in c.files:
                d = files.setdefault(fs.path, {"added": 0, "deleted": 0})
                d["added"] += fs.added
                d["deleted"] += fs.deleted
        if sum(d["added"] for p, d in files.items() if not langs.is_noise(p)) < min_lines:
            continue
        slug = slugify(g["name"])
        fid = f"{repo_name}.{slug}"
        n = 2
        while fid in seen_ids:
            fid = f"{repo_name}.{slug}-{n}"
            n += 1
        seen_ids.add(fid)

        f = Feature(id=fid, repo=repo_name, name=g["name"], source=g["source"], commits=g["commits"],
                    merge_commit=g.get("merge_commit"), files=files)
        f.src_files = sorted(p for p in files if langs.is_source_path(p) and not langs.is_generated(p))
        f.test_files = sorted(p for p in files if langs.is_test_path(p) and langs.language_of(p))
        f.perf_files = sorted(p for p in f.test_files + f.src_files if langs.is_perf_path(p))
        f.subjects = [c.subject for c in cs]
        f.bodies = [c.body for c in cs if c.body]
        f.first_date, f.last_date = cs[0].date, cs[-1].date
        f.authors = sorted({c.author for c in cs})
        _decide_snapshot(repo, f, g, by_sha, order)
        features.append(f)

    _drop_generated(repo, features)
    _attach_rework(features, commits, order)
    return features


def _drop_generated(repo: str, features: list[Feature], big: int = 1000) -> None:
    """Large files are inspected at the reference commit; bundles/minified/generated ones are removed."""
    specs = sorted({(f.reference_commit, p) for f in features if f.reference_commit
                    for p, d in f.files.items() if d["added"] > big and langs.language_of(p)})
    blobs = gitutil.read_blobs(repo, specs)
    generated = {p for (_, p), text in blobs.items() if text and langs.is_generated(p, text)}
    if not generated:
        return
    for f in features:
        for p in generated & set(f.files):
            del f.files[p]
        f.src_files = [p for p in f.src_files if p not in generated]
        f.perf_files = [p for p in f.perf_files if p not in generated]


def _decide_snapshot(repo, f: Feature, g: dict, by_sha, order) -> None:
    first = by_sha[f.commits[0]]
    if g["source"] == "merge":
        merge = by_sha[g["merge_commit"]]
        f.base_commit, f.reference_commit = merge.parents[0], merge.sha
        f.snapshot_strategy = "base_commit"
        return
    if g["source"] == "module":
        f.base_commit, f.reference_commit = None, gitutil.rev_list(repo, "-1", "HEAD")[0]
        f.snapshot_strategy = "remove_paths"
        f.module_paths = [g["module"]]
        return
    last = max(f.commits, key=order.get)
    f.reference_commit = last
    if not first.parents:
        f.base_commit = None
        f.snapshot_strategy = "remove_paths"
        f.module_paths = [p for p in f.src_files]
        return
    f.base_commit = first.parents[0]
    between = set(gitutil.rev_list(repo, f"{f.base_commit}..{last}"))
    # Other people's commits in the middle -> rebuild the snapshot by reverting only ours.
    f.snapshot_strategy = "base_commit" if between <= set(f.commits) else "revert_commits"


REWORK_WINDOW_DAYS = 30
OWNED_MIN_LINES = 10


def _attach_rework(features: list[Feature], commits: list[gitutil.Commit], order) -> None:
    """Fix/revert commits shortly after the feature (REWORK_WINDOW_DAYS) that mostly touch files the
    feature substantially wrote count as rework. Shared hub files touched lightly are ignored."""
    for f in features:
        if f.source == "module":
            continue
        src = {p for p in f.src_files if f.files[p]["added"] >= OWNED_MIN_LINES}
        if not src:
            continue
        own = set(f.commits)
        last_idx = max(order[s] for s in f.commits)
        limit = dt.datetime.fromisoformat(commits[last_idx].date) + dt.timedelta(days=REWORK_WINDOW_DAYS)
        for c in commits[last_idx + 1:]:
            if dt.datetime.fromisoformat(c.date) > limit:
                continue
            if c.sha in own or c.is_merge or not is_fix(c.subject):
                continue
            c_src = {fs.path for fs in c.files if langs.is_source_path(fs.path)}
            if c_src and len(c_src & src) * 2 >= len(c_src):
                f.rework_commits.append(c.sha)
