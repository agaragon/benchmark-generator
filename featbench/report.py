"""Aggregate evaluation results into a leaderboard (per agent, per benchmark, per role)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def load_results(path: str | Path, latest_only: bool = True) -> list[dict]:
    files = [Path(path)] if Path(path).is_file() else sorted(Path(path).rglob("*.json"))
    results = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        if isinstance(d, dict) and "benchmark" in d and "scores" in d:
            d["_file"] = str(f)
            results.append(d)
    if latest_only:
        latest: dict[tuple, dict] = {}
        for r in sorted(results, key=lambda r: r["started_at"]):
            latest[(r["agent"], r["benchmark"])] = r
        results = list(latest.values())
    return results


def _fmt(x, digits=2, pct=False):
    if x is None:
        return "—"
    return f"{x * 100:.0f}%" if pct else f"{x:.{digits}f}"


def build_report(results: list[dict]) -> str:
    if not results:
        return "No results found."
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_agent[r["agent"]].append(r)
    cats = ["business_rules", "architecture", "performance"]
    extra = sorted({c for r in results for c in r["scores"]["categories"]} - set(cats))
    cats += extra

    lines = ["# featbench report", "", "## Leaderboard", "",
             "| agent | benchmarks | accepted | total | " + " | ".join(cats) + " | cost (USD) | cost / accepted | agent time (s) |",
             "|---|---|---|---|" + "---|" * len(cats) + "---|---|---|"]
    rows = []
    for agent, rs in by_agent.items():
        accepted = [r for r in rs if r["scores"].get("acceptable")]
        costs = [float(r["meta"]["cost_usd"]) for r in rs if r["meta"].get("cost_usd") is not None]
        times = [float(r["meta"]["agent_seconds"]) for r in rs if r["meta"].get("agent_seconds") is not None]
        cat_means = []
        for c in cats:
            vals = [r["scores"]["categories"][c] for r in rs if c in r["scores"]["categories"]]
            cat_means.append(_fmt(mean(vals)) if vals else "—")
        total_cost = sum(costs) if costs else None
        rows.append((mean(r["scores"]["total"] for r in rs),
                     f"| {agent} | {len(rs)} | {len(accepted)}/{len(rs)} | {_fmt(mean(r['scores']['total'] for r in rs))} | "
                     + " | ".join(cat_means)
                     + f" | {_fmt(total_cost)} | {_fmt(total_cost / len(accepted)) if (total_cost is not None and accepted) else '—'}"
                     + f" | {_fmt(mean(times), 0) if times else '—'} |"))
    lines += [row for _, row in sorted(rows, key=lambda t: -t[0])]

    benchmarks = sorted({r["benchmark"] for r in results})
    agents = sorted(by_agent)
    lines += ["", "## Per benchmark (total score, ✅ = passes the gate)", "",
              "| benchmark | role | " + " | ".join(agents) + " |", "|---|---|" + "---|" * len(agents)]
    index = {(r["agent"], r["benchmark"]): r for r in results}
    for b in benchmarks:
        role = next(r.get("role", "") for r in results if r["benchmark"] == b)
        cells = []
        for a in agents:
            r = index.get((a, b))
            cells.append("—" if not r else f"{_fmt(r['scores']['total'])} {'✅' if r['scores'].get('acceptable') else '❌'}")
        lines.append(f"| `{b}` | {role} | " + " | ".join(cells) + " |")

    roles = sorted({r.get("role", "?") for r in results})
    if len(roles) > 1:
        lines += ["", "## Per role (mean total)", "", "| agent | " + " | ".join(roles) + " |",
                  "|---|" + "---|" * len(roles)]
        for a in agents:
            cells = []
            for role in roles:
                vals = [r["scores"]["total"] for r in by_agent[a] if r.get("role") == role]
                cells.append(_fmt(mean(vals)) if vals else "—")
            lines.append(f"| {a} | " + " | ".join(cells) + " |")

    lines += ["", "## Failed checks", ""]
    for r in sorted(results, key=lambda r: (r["agent"], r["benchmark"])):
        bad = [c for c in r["checks"] if c["status"] in ("fail", "error")]
        if bad:
            lines.append(f"- **{r['agent']}** on `{r['benchmark']}`: " +
                         "; ".join(f"{c['id']} ({c['summary']})" for c in bad))
    return "\n".join(lines) + "\n"
