"""Score how clean or chaotic the reference implementation of a feature was.

Signals come from two sources:
  * history: fix commits inside the feature, rework after it, reverts, scatter, thrash, giant commits
  * code (at the reference commit): complexity, long functions, missing tests, layer violations

Every penalty is recorded as a human-readable reason so the score is explainable and tunable.
"""

from __future__ import annotations

from collections import Counter

from .. import gitutil, langs
from .features import REVERT_RE, Feature, is_fix
from .profile import RepoProfile

# Tunable knobs: (threshold, penalty per unit, max penalty)
PENALTIES = {
    "no_tests": 30,
    "fix_ratio": (0.3, 25),          # ratio above which we penalise, max penalty
    "rework": (8, 30),
    "revert": (10, 20),
    "scatter": (4, 5, 20),            # areas allowed, per extra area, max
    "thrash": (0.5, 10),
    "complexity": ((15, 8), (25, 15)),
    "long_functions": (80, 5, 15),
    "layer_violation": (8, 24),
    "giant_commit": (1500, 5),
}
LABELS = ((80, "clean"), (55, "mixed"), (0, "chaotic"))


def _area(path: str) -> str:
    return "/".join(path.split("/")[:2]) if "/" in path else "."


def compute_metrics(repo: str, f: Feature, profile: RepoProfile, commit_index: dict) -> dict:
    real = {p: d for p, d in f.files.items() if not langs.is_noise(p)}
    added = sum(d["added"] for d in real.values())
    deleted = sum(d["deleted"] for d in real.values())
    own = [commit_index[s] for s in f.commits if s in commit_index]
    fix_commits = [c for c in own[1:] if is_fix(c.subject)]
    reverts = [c for c in own if REVERT_RE.search(c.subject)]
    reverts += [commit_index[s] for s in f.rework_commits
                if s in commit_index and REVERT_RE.search(commit_index[s].subject)]

    ref, base = f.reference_commit, f.base_commit
    specs = [(ref, p) for p in f.src_files] + ([(base, p) for p in f.src_files] if base else [])
    blobs = gitutil.read_blobs(repo, specs) if ref else {}
    # Only judge what the feature wrote: functions overlapping changed lines, imports it introduced.
    touched = gitutil.added_lines(repo, base, ref, f.src_files) if (base and ref) else None
    complexities, long_funcs, violations = [], [], []
    for p in f.src_files:
        text = blobs.get((ref, p))
        if not text:
            continue
        lines = touched.get(p) if touched is not None else None
        for fn in langs.function_metrics(p, text):
            if lines is not None and not any(fn.start <= ln <= fn.end for ln in lines):
                continue
            complexities.append((fn.complexity, f"{p}:{fn.name}"))
            if fn.length > PENALTIES["long_functions"][0]:
                long_funcs.append(f"{p}:{fn.name} ({fn.length} lines)")
        src_layer = profile.layer_of(p)
        if src_layer:
            before = set(langs.extract_imports(p, blobs.get((base, p)) or "")) if base else set()
            for imp in set(langs.extract_imports(p, text)) - before:
                dst = profile.layer_of(imp)
                if dst and dst.rank > src_layer.rank:
                    violations.append(f"{p} ({src_layer.name}) -> {imp} ({dst.name})")

    complexities.sort(reverse=True)
    return {
        "commits": len(f.commits),
        "lines_added": added,
        "lines_deleted": deleted,
        "files": len(f.files),
        "src_files": len(f.src_files),
        "test_files": len(f.test_files),
        "fix_commits": len(fix_commits),
        "fix_ratio": round(len(fix_commits) / max(1, len(own)), 2),
        "rework_commits": len(f.rework_commits),
        "reverts": len(reverts),
        "areas": sorted({_area(p) for p in f.src_files}),
        "thrash_ratio": round(deleted / added, 2) if added else 0.0,
        "largest_commit_lines": max((sum(fs.added + fs.deleted for fs in c.files
                                         if fs.path in real)
                                     for c in own), default=0),
        "max_complexity": complexities[0][0] if complexities else 0,
        "most_complex": complexities[0][1] if complexities else None,
        "long_functions": long_funcs,
        "layer_violations": violations,
    }


def score_quality(m: dict) -> dict:
    score, reasons, positives = 100.0, [], []
    P = PENALTIES

    if m["test_files"] == 0:
        score -= P["no_tests"]
        reasons.append("no test files were added/changed with the feature")
    else:
        positives.append(f"{m['test_files']} test file(s) shipped with the feature")

    thr, maxp = P["fix_ratio"]
    if m["fix_ratio"] > thr:
        pen = min(maxp, (m["fix_ratio"] - thr) / (1 - thr) * maxp + 5)
        score -= pen
        reasons.append(f"{m['fix_commits']} of {m['commits']} commits inside the feature were fixes")

    per, maxp = P["rework"]
    if m["rework_commits"]:
        score -= min(maxp, per * m["rework_commits"])
        reasons.append(f"{m['rework_commits']} later fix commit(s) reworked the feature's files")

    per, maxp = P["revert"]
    if m["reverts"]:
        score -= min(maxp, per * m["reverts"])
        reasons.append(f"{m['reverts']} revert(s) related to the feature")

    allowed, per, maxp = P["scatter"]
    if len(m["areas"]) > allowed:
        score -= min(maxp, per * (len(m["areas"]) - allowed))
        reasons.append(f"changes scattered over {len(m['areas'])} areas (shotgun surgery)")

    thr, pen = P["thrash"]
    if m["commits"] > 1 and m["thrash_ratio"] > thr:
        score -= pen
        reasons.append(f"high thrash: {m['lines_deleted']} lines deleted for {m['lines_added']} added")

    for limit, pen in sorted(P["complexity"], reverse=True):
        if m["max_complexity"] > limit:
            score -= pen
            reasons.append(f"cyclomatic complexity {m['max_complexity']} in {m['most_complex']}")
            break

    _, per, maxp = P["long_functions"]
    if m["long_functions"]:
        score -= min(maxp, per * len(m["long_functions"]))
        reasons.append(f"{len(m['long_functions'])} very long function(s)")

    per, maxp = P["layer_violation"]
    if m["layer_violations"]:
        score -= min(maxp, per * len(m["layer_violations"]))
        reasons.append(f"{len(m['layer_violations'])} import(s) break the layer dependency rule")
    elif m["src_files"]:
        positives.append("no layer-rule violations detected")

    limit, pen = P["giant_commit"]
    if m["largest_commit_lines"] > limit:
        score -= pen
        reasons.append(f"a single commit changed {m['largest_commit_lines']} lines (hard to review)")

    score = max(0, round(score))
    label = next(lbl for floor, lbl in LABELS if score >= floor)
    return {"score": score, "label": label, "issues": reasons, "strengths": positives}


def score_suitability(f: Feature) -> dict:
    """How useful this feature is as a benchmark task (independent from its quality)."""
    m, q = f.metrics, f.quality
    score, notes = 0, []
    has_tests = m["test_files"] > 0
    if has_tests:
        score += 40
    else:
        notes.append("no automated tests: business-rule checks must be written by hand")
    if m["src_files"] == 0:
        notes.append("no source files changed (config/docs only)")
    size = m["lines_added"]
    if 20 <= size <= 3000:
        score += 20
    elif size < 20:
        notes.append("very small change: low signal")
    else:
        notes.append("very large change: consider splitting into smaller benchmarks")
    if f.snapshot_strategy == "revert_commits":
        notes.append("feature commits are interleaved with others: snapshot is built by reverting them")
    if q["label"] == "clean":
        score += 30
        notes.append("clean reference: its metrics are a good baseline for fitness functions")
    elif q["label"] == "mixed":
        score += 15
    else:
        notes.append("chaotic reference: trust its tests for business rules, but do not use its "
                     "architecture/performance as a baseline (thresholds come from the repo instead)")
    if f.perf_files:
        score += 10
        notes.append("has performance/benchmark files")
    if m["src_files"] == 0:
        score = min(score, 10)
    recommended = has_tests and m["src_files"] > 0 and 20 <= size <= 3000
    return {"score": score, "recommended": recommended, "notes": notes}


def evaluate_features(repo: str, features: list[Feature], profile: RepoProfile) -> None:
    index = {c.sha: c for c in gitutil.log_commits(repo)}
    for f in features:
        f.metrics = compute_metrics(repo, f, profile, index)
        f.quality = score_quality(f.metrics)
        f.suitability = score_suitability(f)


def summarize(features: list[Feature]) -> dict:
    return dict(Counter(f.quality.get("label", "?") for f in features))
