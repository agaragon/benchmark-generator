"""Evaluate a workspace against a benchmark, and calibrate benchmarks against the reference implementation."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from .benchmark import Benchmark
from .checks import CheckContext, CheckResult, build_check, compute_changes
from .workspace import prepare

COPY_IGNORE = shutil.ignore_patterns("node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                                     ".venv", ".tox", ".gradle")


def score(bench: Benchmark, results: list[CheckResult]) -> dict:
    """Weighted average per category, then weighted total over categories that have checks."""
    sums: dict[str, list[float]] = {}
    for r in results:
        if r.status == "skip":
            continue
        s = sums.setdefault(r.category, [0.0, 0.0])
        s[0] += r.weight * r.score
        s[1] += r.weight
    categories = {c: round(v[0] / v[1], 4) for c, v in sums.items() if v[1] > 0}
    weights = {c: w for c, w in bench.weights.items() if c in categories and w > 0}
    wsum = sum(weights.values())
    total = round(sum(categories[c] * w for c, w in weights.items()) / wsum, 4) if wsum else 0.0
    gate = bench.gate
    gate_score = categories.get(gate["category"])
    acceptable = None if gate_score is None else gate_score >= float(gate.get("min_score", 1.0))
    return {"categories": categories, "weights": {c: round(w / wsum, 4) for c, w in weights.items()} if wsum else {},
            "total": total, "acceptable": acceptable,
            "gate": f"{gate['category']} >= {gate.get('min_score', 1.0)}"}


def evaluate(bench: Benchmark, workspace: str | Path, agent: str = "unknown", meta: dict | None = None,
             in_place: bool = False, calibrating: bool = False, categories: list[str] | None = None,
             out_dir: str | Path | None = None, log=print) -> dict:
    workspace = Path(workspace).resolve()
    if not (workspace / ".git").exists():
        raise SystemExit(f"error: {workspace} is not a featbench workspace (run `featbench prepare` first)")
    started = dt.datetime.now()
    t0 = time.perf_counter()
    tmp = None
    if in_place:
        ws = workspace
    else:
        tmp = tempfile.mkdtemp(prefix="featbench-eval-")
        ws = Path(tmp) / "ws"
        shutil.copytree(workspace, ws, symlinks=True, ignore=COPY_IGNORE)
    try:
        hidden_targets = {h["target"] for h in bench.hidden_files}
        changes = compute_changes(ws, exclude=hidden_targets)
        for h in bench.hidden_files:
            target = ws / h["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bench.dir / h["source"], target)

        ctx = CheckContext(workspace=ws, benchmark_dir=bench.dir, changes=changes,
                           calibration=bench.calibration().get("baselines", {}),
                           calibrating=calibrating, default_timeout=bench.timeout, env=bench.env)
        setup_log = []
        for cmd in bench.setup:
            log(f"  setup: {cmd}")
            res = ctx.run_command(cmd)
            setup_log.append({"run": cmd, "returncode": res.returncode, "duration_s": round(res.duration_s, 2),
                              "output_tail": res.tail(20)})
            if not res.ok:
                log(f"  ! setup command failed ({res.returncode})")

        results: list[CheckResult] = []
        for spec in bench.checks:
            if spec.get("enabled", True) is False:
                continue
            try:
                check = build_check(spec)
            except ValueError as e:
                results.append(CheckResult(spec.get("id", "?"), spec.get("type", "?"), spec.get("category", "?"),
                                           float(spec.get("weight", 1)), "error", 0.0, str(e)))
                continue
            if categories and check.category not in categories:
                continue
            c0 = time.perf_counter()
            try:
                r = check.run(ctx)
            except Exception as e:  # a broken check must not abort the whole evaluation
                r = check.errored(f"{type(e).__name__}: {e}")
            r.duration_s = round(time.perf_counter() - c0, 3)
            results.append(r)
            log(f"  [{r.status:5}] {r.category:15} {r.id:32} {r.score:5.2f}  {r.summary}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    result = {
        "benchmark": bench.id,
        "title": bench.title,
        "role": bench.role,
        "agent": agent,
        "meta": meta or {},
        "started_at": started.isoformat(timespec="seconds"),
        "evaluation_seconds": round(time.perf_counter() - t0, 2),
        "workspace": str(workspace),
        "changes": {"files": len(changes), "lines": sum(c.added + c.deleted for c in changes),
                    "paths": [f"{c.status} {c.path}" for c in changes][:200]},
        "setup": setup_log,
        "checks": [r.to_dict() for r in results],
        "scores": score(bench, results),
    }
    if not changes and not calibrating:
        # Doing nothing must not be rewarded by checks that trivially pass on an empty diff.
        result["scores"].update(total=0.0, acceptable=False, note="no changes were made in the workspace")
    if out_dir:
        safe_agent = re.sub(r"[^\w.-]+", "_", agent)
        target = Path(out_dir) / bench.id / f"{safe_agent}__{started.strftime('%Y%m%dT%H%M%S')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2))
        result["result_file"] = str(target)
    return result


def calibrate(bench: Benchmark, repo: str | None = None, log=print) -> dict:
    """1) score the reference implementation (the human baseline) and record performance baselines;
    2) check that the business-rule checks FAIL on the bare snapshot (otherwise they measure nothing)."""
    warnings = []
    with tempfile.TemporaryDirectory(prefix="featbench-calib-") as tmp:
        log("» reference implementation")
        ref_ws = prepare(bench, Path(tmp) / "reference", repo=repo, apply_reference=True)
        ref = evaluate(bench, ref_ws, agent="reference", in_place=True, calibrating=True, log=log)
        baselines = {c["id"]: c["calibration"] for c in ref["checks"] if c.get("calibration")}
        log("» bare snapshot (business rules only)")
        snap_ws = prepare(bench, Path(tmp) / "snapshot", repo=repo)
        snap = evaluate(bench, snap_ws, agent="snapshot", in_place=True,
                        categories=["business_rules"], log=log)

    ref_br = ref["scores"]["categories"].get("business_rules")
    snap_br = snap["scores"]["categories"].get("business_rules")
    if ref_br is None:
        warnings.append("no enabled business-rule checks: the benchmark cannot tell whether the feature works "
                        "(write tests and add a test_command/command check)")
    if ref_br is not None and ref_br < 1.0:
        warnings.append(f"the reference implementation scores {ref_br:.2f} on business rules: "
                        "check the test commands / setup, the benchmark cannot be passed as is")
    if snap_br is not None and snap_br >= 1.0:
        warnings.append("business-rule checks already pass on the bare snapshot: they do not "
                        "discriminate (add tests that exercise the new behaviour)")
    for c in ref["checks"]:
        if c["category"] != "business_rules" and c["status"] in ("fail", "error"):
            warnings.append(f"reference fails '{c['id']}' ({c['summary']}): relax the threshold or "
                            "keep it if the reference is known to be chaotic")
    data = {
        "calibrated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "baselines": baselines,
        "reference": {"scores": ref["scores"],
                      "checks": {c["id"]: {"status": c["status"], "score": c["score"]} for c in ref["checks"]}},
        "snapshot": {"scores": snap["scores"]},
        "warnings": warnings,
    }
    bench.calibration_file.write_text(json.dumps(data, indent=2))
    return data
