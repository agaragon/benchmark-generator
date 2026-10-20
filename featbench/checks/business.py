"""Business-rule checks: behaviour verified through tests or arbitrary commands."""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import Check, CheckContext, CheckResult, register


def _parse_junit(path: Path) -> dict | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
    total = failed = skipped = 0
    failing: list[str] = []
    for s in suites:
        for case in s.iter("testcase"):
            total += 1
            if case.find("skipped") is not None:
                skipped += 1
            elif case.find("failure") is not None or case.find("error") is not None:
                failed += 1
                failing.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return {"total": total - skipped, "failed": failed, "skipped": skipped, "failing": failing[:50]}


_PYTEST = re.compile(r"(\d+) (passed|failed|error|errors)\b")
_JEST = re.compile(r"Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(?:(\d+) passed,\s*)?(\d+) total")
_GO = re.compile(r"^\s*--- (PASS|FAIL):", re.M)


def _parse_output(text: str) -> dict | None:
    m = _JEST.search(text)
    if m:
        failed, skipped, _, total = (int(x or 0) for x in m.groups())
        return {"total": total - skipped, "failed": failed, "skipped": skipped}
    counts = {"passed": 0, "failed": 0}
    found = False
    for n, kind in _PYTEST.findall(text):
        found = True
        counts["passed" if kind == "passed" else "failed"] += int(n)
    if found:
        return {"total": counts["passed"] + counts["failed"], "failed": counts["failed"], "skipped": 0}
    go = _GO.findall(text)
    if go:
        return {"total": len(go), "failed": go.count("FAIL"), "skipped": 0}
    return None


@register
class TestCommand(Check):
    type_name = "test_command"
    default_category = "business_rules"
    description = ("Runs a test command. Partial credit = passed/total, read from a JUnit XML report "
                   "(use the {junit_xml} placeholder) or from pytest/jest/go output.")
    params_doc = {
        "run": "shell command; placeholders {python} {workspace} {benchmark_dir} {junit_xml}",
        "min_tests": "minimum number of tests that must run (default 1) — guards against 0 collected",
    }

    def run(self, ctx: CheckContext) -> CheckResult:
        with tempfile.TemporaryDirectory() as tmp:
            junit = Path(tmp) / "junit.xml"
            res = ctx.run_command(self.param("run"), self.timeout, {"junit_xml": str(junit)})
            stats = _parse_junit(junit) if junit.exists() else None
        stats = stats or _parse_output(res.stdout + "\n" + res.stderr)
        details = {"returncode": res.returncode, "output_tail": res.tail(), "duration_s": round(res.duration_s, 3)}
        if res.timed_out:
            return self.errored("test command timed out", **details)
        if stats is None:
            return (self.passed("command succeeded", **details) if res.ok
                    else self.failed(0.0, "command failed (no test report could be parsed)", **details))
        details.update(stats)
        total, failed = stats["total"], stats["failed"]
        if total < int(self.param("min_tests", 1)):
            return self.failed(0.0, f"only {total} test(s) ran", **details)
        score = (total - failed) / total
        if failed == 0 and res.ok:
            return self.passed(f"{total}/{total} tests passed", **details)
        return self.failed(score, f"{total - failed}/{total} tests passed", **details)


@register
class CommandCheck(Check):
    type_name = "command"
    default_category = "business_rules"
    description = "Generic check: passes when the command exits with code 0 (works for any category)."
    params_doc = {"run": "shell command", "expect_output": "optional regex that stdout must match"}

    def run(self, ctx: CheckContext) -> CheckResult:
        res = ctx.run_command(self.param("run"), self.timeout)
        details = {"returncode": res.returncode, "output_tail": res.tail()}
        if res.timed_out:
            return self.errored("command timed out", **details)
        pattern = self.param("expect_output")
        if res.ok and (not pattern or re.search(pattern, res.stdout, re.M)):
            return self.passed("command succeeded", **details)
        return self.failed(0.0, "command failed" if not res.ok else "output did not match", **details)
