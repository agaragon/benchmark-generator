"""featbench command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__

ICON = {"clean": "🟢", "mixed": "🟡", "chaotic": "🔴"}


def _print_analysis(analysis: dict) -> None:
    for r in analysis["repos"]:
        layers = ", ".join(l["name"] for l in r["layers"]) or "none"
        print(f"repo {r['name']}: {', '.join(r['stacks']) or r['primary_language'] or '?'} · layers: {layers} "
              f"· thresholds {r['thresholds']}")
    feats = analysis["features"]
    print(f"\n{len(feats)} candidate feature(s) — quality: "
          + ", ".join(f"{ICON.get(k, '')} {k} {v}" for k, v in analysis["summary"].items()) + "\n")
    print(f"{'#':>3}  {'feature':40} {'source':7} {'quality':14} {'fit':>4}  {'rec':3}  main issue / note")
    print("-" * 110)
    for i, f in enumerate(feats, 1):
        q, s = f["quality"], f["suitability"]
        note = (q["issues"] or s["notes"] or q["strengths"] or [""])[0]
        print(f"{i:>3}  {f['id'][:40]:40} {f['source']:7} {ICON.get(q['label'], '')} {q['label']:8} {q['score']:>3} "
              f"{s['score']:>4}  {'✔' if s['recommended'] else ' ':3}  {note[:60]}")


def cmd_analyze(a) -> int:
    from .analysis import analyze, render_markdown
    analysis = analyze(a.repos)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(analysis, indent=2))
    md = out.with_suffix(".md")
    md.write_text(render_markdown(analysis))
    _print_analysis(analysis)
    print(f"\nwrote {out} and {md}")
    print("next: featbench generate", out, "--select <ids or #numbers> | --recommended | --top N")
    return 0


def cmd_generate(a) -> int:
    from .analysis import load_analysis
    from .generate import generate, select_features
    repos, features = load_analysis(a.analysis)
    chosen = select_features(features, a.select, a.recommended, a.top, a.all)
    if not chosen:
        print("no features selected (none recommended?) — use --select or --all", file=sys.stderr)
        return 1
    dirs, warnings = generate(repos, chosen, a.output, role=a.role, hints=not a.no_hints)
    for d in dirs:
        print(f"created {d}")
    for w in warnings:
        print(f"warning: {w}")
    print(f"\nnext: review task.md / benchmark.yaml, then `featbench calibrate {a.output}`")
    return 0


def _benchmarks(paths: list[str]):
    from .benchmark import Benchmark, find_benchmarks
    out = []
    for p in paths:
        found = find_benchmarks(p)
        if not found:
            raise SystemExit(f"error: no benchmark.yaml under {p}")
        out += [Benchmark.load(d) for d in found]
    return out


def cmd_validate(a) -> int:
    for b in _benchmarks(a.benchmarks):
        print(f"ok  {b.id}  ({len(b.checks)} checks)")
    return 0


def cmd_calibrate(a) -> int:
    from .runner import calibrate
    status = 0
    for b in _benchmarks(a.benchmarks):
        print(f"\n=== calibrating {b.id}")
        data = calibrate(b, repo=a.repo)
        ref = data["reference"]["scores"]
        print(f"reference (human baseline) total {ref['total']:.2f} {ref['categories']}  acceptable={ref['acceptable']}")
        print(f"bare snapshot business rules: {data['snapshot']['scores']['categories'].get('business_rules')}")
        for w in data["warnings"]:
            print(f"  warning: {w}")
            status = 2 if "cannot be passed" in w or "do not discriminate" in w else status
        print(f"wrote {b.calibration_file}")
    return status


def cmd_prepare(a) -> int:
    from .benchmark import Benchmark
    from .workspace import prepare
    bench = Benchmark.load(a.benchmark)
    ws = prepare(bench, a.workspace, repo=a.repo, force=a.force)
    print(f"workspace ready: {ws}")
    print(f"task for the agent: {ws / '.featbench' / 'TASK.md'}")
    print(f"when the agent is done: featbench evaluate -w {ws} --agent <name> -o results/")
    return 0


def cmd_evaluate(a) -> int:
    from .benchmark import Benchmark
    from .runner import evaluate
    from .workspace import detect_benchmark
    bdir = a.benchmark or detect_benchmark(a.workspace)
    if not bdir:
        raise SystemExit("error: benchmark not given and not detectable from the workspace")
    bench = Benchmark.load(bdir)
    meta = {}
    if a.cost is not None:
        meta["cost_usd"] = a.cost
    if a.tokens is not None:
        meta["tokens"] = a.tokens
    if a.agent_seconds is not None:
        meta["agent_seconds"] = a.agent_seconds
    for kv in a.meta or []:
        k, _, v = kv.partition("=")
        meta[k] = v
    print(f"evaluating {a.workspace} against {bench.id} (agent: {a.agent})")
    result = evaluate(bench, a.workspace, agent=a.agent, meta=meta, in_place=a.in_place, out_dir=a.output,
                      log=(lambda *_: None) if a.json else print)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        s = result["scores"]
        if s.get("note"):
            print(f"\nnote: {s['note']}")
        print(f"\nTOTAL {s['total']:.2f}  " + "  ".join(f"{k}={v:.2f}" for k, v in s["categories"].items())
              + f"  acceptable={s['acceptable']} ({s['gate']})")
        if result.get("result_file"):
            print(f"wrote {result['result_file']}")
    return 0 if result["scores"]["acceptable"] is not False else 3


def cmd_report(a) -> int:
    from .report import build_report, load_results
    text = build_report(load_results(a.results, latest_only=not a.all_runs))
    if a.output:
        Path(a.output).write_text(text)
        print(f"wrote {a.output}")
    else:
        print(text)
    return 0


def cmd_checks(a) -> int:
    from .checks import REGISTRY, load_plugins
    load_plugins(a.plugins or [])
    for name in sorted(REGISTRY):
        cls = REGISTRY[name]
        print(f"{name}  [{cls.default_category}]\n    {cls.description}")
        for p, doc in cls.params_doc.items():
            print(f"      - {p}: {doc}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="featbench", description="Build custom benchmarks for coding agents from your own repositories.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze", help="suggest features from one or more repositories and rate their implementation")
    s.add_argument("repos", nargs="+")
    s.add_argument("-o", "--output", default="analysis.json")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("generate", help="create benchmark folders for selected features")
    s.add_argument("analysis")
    s.add_argument("-o", "--output", default="benchmarks")
    s.add_argument("--select", help="comma-separated feature ids, short names or #numbers from `analyze`")
    s.add_argument("--recommended", action="store_true", help="all recommended features (default)")
    s.add_argument("--top", type=int)
    s.add_argument("--all", action="store_true")
    s.add_argument("--role", help="override role (backend, frontend, sre, ...)")
    s.add_argument("--no-hints", action="store_true", help="do not tell the agent where the code belongs")
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("validate", help="validate benchmark definitions")
    s.add_argument("benchmarks", nargs="+")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("calibrate", help="score the reference implementation and record baselines")
    s.add_argument("benchmarks", nargs="+")
    s.add_argument("--repo", help="override the source repository path")
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("prepare", help="create the snapshot workspace an agent will work in")
    s.add_argument("benchmark")
    s.add_argument("-w", "--workspace", required=True)
    s.add_argument("--repo")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_prepare)

    s = sub.add_parser("evaluate", help="evaluate a workspace after an agent worked in it")
    s.add_argument("benchmark", nargs="?", help="benchmark dir (auto-detected from the workspace if omitted)")
    s.add_argument("-w", "--workspace", required=True)
    s.add_argument("--agent", default="unknown")
    s.add_argument("--cost", type=float, help="cost of the agent run in USD")
    s.add_argument("--tokens", type=int)
    s.add_argument("--agent-seconds", type=float, help="wall time the agent took")
    s.add_argument("--meta", action="append", help="extra key=value metadata (repeatable)")
    s.add_argument("-o", "--output", help="results directory")
    s.add_argument("--in-place", action="store_true", help="evaluate in the workspace instead of a copy")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("report", help="leaderboard from a results directory")
    s.add_argument("results")
    s.add_argument("-o", "--output")
    s.add_argument("--all-runs", action="store_true", help="average all runs instead of the latest per agent/benchmark")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("checks", help="list available check types and their parameters")
    s.add_argument("--plugins", nargs="*", help="plugin directories to load")
    s.set_defaults(fn=cmd_checks)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
