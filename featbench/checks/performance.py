"""Performance fitness functions. Thresholds may be absolute or `auto` (baseline from calibration x tolerance)."""

from __future__ import annotations

import json
import re
import statistics

from .base import Check, CheckContext, CheckResult, register


def _limit_score(value: float, limit: float, lower_is_better: bool = True) -> float:
    if lower_is_better:
        return 1.0 if value <= limit else max(0.0, 1 - (value - limit) / limit)
    return 1.0 if value >= limit else max(0.0, value / limit)


class _ThresholdCheck(Check):
    default_category = "performance"

    def threshold(self, ctx: CheckContext, key: str, lower_is_better: bool = True) -> float | None:
        value = self.param(key, "auto")
        if value != "auto":
            return float(value)
        base = (ctx.calibration.get(self.id) or {}).get("baseline")
        if base is None:
            return None
        tol = float(self.param("tolerance", 1.5))
        return base * tol if lower_is_better else base / tol


@register
class CommandDuration(_ThresholdCheck):
    type_name = "command_duration"
    description = "Median wall time of a command over `repeat` runs must stay under `max_seconds`."
    params_doc = {"run": "shell command", "repeat": "runs measured (default 3)", "warmup": "unmeasured runs (default 0)",
                  "max_seconds": "number, or 'auto' = calibrated baseline x tolerance",
                  "tolerance": "multiplier for auto thresholds (default 1.5)",
                  "must_succeed": "fail if the command exits non-zero (default true)"}

    def run(self, ctx: CheckContext) -> CheckResult:
        cmd = self.param("run")
        for _ in range(int(self.param("warmup", 0))):
            ctx.run_command(cmd, self.timeout)
        times = []
        for _ in range(max(1, int(self.param("repeat", 3)))):
            res = ctx.run_command(cmd, self.timeout)
            if res.timed_out:
                return self.errored("command timed out", output_tail=res.tail())
            if self.param("must_succeed", True) and not res.ok:
                return self.failed(0.0, "command failed, cannot measure performance", output_tail=res.tail())
            times.append(res.duration_s)
        median = statistics.median(times)
        details = {"median_seconds": round(median, 4), "runs": [round(t, 4) for t in times]}
        if ctx.calibrating:
            r = self.passed(f"baseline recorded: {median:.3f}s", **details)
            r.calibration = {"baseline": median}
            return r
        limit = self.threshold(ctx, "max_seconds")
        if limit is None:
            return self.skipped("max_seconds is 'auto' but no calibration found (run `featbench calibrate`)")
        details["limit_seconds"] = round(limit, 4)
        score = _limit_score(median, limit)
        if score >= 1:
            return self.passed(f"median {median:.3f}s <= {limit:.3f}s", **details)
        return self.failed(score, f"median {median:.3f}s > {limit:.3f}s", **details)


@register
class CommandMetric(_ThresholdCheck):
    type_name = "command_metric"
    description = ("Runs a command (load test, profiler, k6, locust, ...) and compares one metric it prints "
                   "against a threshold.")
    params_doc = {"run": "shell command",
                  "json_path": "dot path of the metric in JSON stdout, e.g. 'metrics.p95_ms'",
                  "regex": "alternative to json_path: regex whose first group is the number",
                  "threshold": "number, or 'auto' = calibrated baseline x tolerance",
                  "direction": "'lower' (default, e.g. latency) or 'higher' (e.g. throughput)",
                  "tolerance": "multiplier for auto thresholds (default 1.5)"}

    def _extract(self, out: str) -> float | None:
        if self.param("json_path"):
            try:
                data = json.loads(out.strip().splitlines()[-1] if out.strip() else "{}")
            except ValueError:
                try:
                    data = json.loads(out)
                except ValueError:
                    return None
            for key in self.param("json_path").split("."):
                if not isinstance(data, dict) or key not in data:
                    return None
                data = data[key]
            return float(data)
        m = re.search(self.param("regex", r"([-+]?\d+(?:\.\d+)?)"), out)
        return float(m.group(1)) if m else None

    def run(self, ctx: CheckContext) -> CheckResult:
        res = ctx.run_command(self.param("run"), self.timeout)
        if res.timed_out:
            return self.errored("command timed out", output_tail=res.tail())
        if not res.ok:
            return self.failed(0.0, "command failed", output_tail=res.tail())
        value = self._extract(res.stdout)
        if value is None:
            return self.errored("metric not found in output", output_tail=res.tail())
        lower = self.param("direction", "lower") == "lower"
        if ctx.calibrating:
            r = self.passed(f"baseline recorded: {value}", value=value)
            r.calibration = {"baseline": value}
            return r
        limit = self.threshold(ctx, "threshold", lower)
        if limit is None:
            return self.skipped("threshold is 'auto' but no calibration found (run `featbench calibrate`)")
        score = _limit_score(value, limit, lower)
        op = "<=" if lower else ">="
        if score >= 1:
            return self.passed(f"{value} {op} {limit:.4g}", value=value, limit=limit)
        return self.failed(score, f"{value} violates {op} {limit:.4g}", value=value, limit=limit)
