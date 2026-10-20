"""End-to-end: demo repo -> analyze -> generate -> calibrate -> prepare -> simulated agents -> evaluate -> report."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))

from make_demo_repo import build  # noqa: E402

from featbench.analysis import analyze, load_analysis  # noqa: E402
from featbench.benchmark import Benchmark  # noqa: E402
from featbench.cli import main  # noqa: E402
from featbench.generate import generate, select_features  # noqa: E402
from featbench.report import build_report, load_results  # noqa: E402
from featbench.runner import calibrate, evaluate  # noqa: E402
from featbench.workspace import prepare  # noqa: E402

AGENTS = ROOT / "examples" / "agents"
quiet = lambda *_: None  # noqa: E731


@pytest.fixture(scope="module")
def suite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    repo = build(tmp / "bankapp")
    analysis = analyze([str(repo)])
    (tmp / "analysis.json").write_text(json.dumps(analysis))
    repos, features = load_analysis(tmp / "analysis.json")
    generate(repos, select_features(features, None, False, None, True), tmp / "benchmarks")
    return tmp, analysis


def _feature(analysis, suffix):
    return next(f for f in analysis["features"] if f["id"].endswith(suffix))


def test_analysis_rates_features(suite):
    _, analysis = suite
    assert {f["id"] for f in analysis["features"]} == {
        "bankapp.signup", "bankapp.login", "bankapp.account-closure", "bankapp.account-reopening", "bankapp.statements"}
    assert _feature(analysis, "signup")["source"] == "merge"
    assert _feature(analysis, "login")["quality"]["label"] == "clean"
    closure = _feature(analysis, "account-closure")
    assert closure["quality"]["label"] == "chaotic"
    assert closure["snapshot_strategy"] == "revert_commits"      # a docs commit sits in the middle
    assert closure["rework_commits"]                              # the later hotfix
    statements = _feature(analysis, "statements")
    assert not statements["suitability"]["recommended"]          # no tests
    assert [l["name"] for l in analysis["repos"][0]["layers"]] == ["domain", "application", "infrastructure", "presentation"]


def test_generated_benchmark_is_valid_and_hides_tests(suite):
    tmp, _ = suite
    b = Benchmark.load(tmp / "benchmarks" / "bankapp.login")
    cats = {c["category"] for c in b.checks}
    assert cats == {"business_rules", "architecture", "performance"}
    assert (b.dir / "hidden" / "tests" / "test_login.py").exists()
    task = b.task_text()
    assert "account locks after three failed attempts" in task
    assert "test(login)" not in task


def test_snapshot_has_no_history_and_no_feature(suite, tmp_path):
    tmp, _ = suite
    b = Benchmark.load(tmp / "benchmarks" / "bankapp.account-closure")
    ws = prepare(b, tmp_path / "ws")
    log = subprocess.run(["git", "-C", str(ws), "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.strip().splitlines()) == 1
    assert not (ws / "bankapp/application/closure.py").exists()
    assert not (ws / "tests/test_closure.py").exists()
    assert (ws / "bankapp/application/login.py").exists()        # earlier features are present
    assert "API" in (ws / "README.md").read_text()                 # the unrelated docs commit is kept
    assert (ws / ".featbench" / "TASK.md").exists()


def test_calibration_and_agent_ranking(suite, tmp_path):
    tmp, _ = suite
    b = Benchmark.load(tmp / "benchmarks" / "bankapp.account-closure")
    cal = calibrate(b, log=quiet)
    assert cal["reference"]["scores"]["categories"]["business_rules"] == 1.0
    assert cal["snapshot"]["scores"]["categories"]["business_rules"] == 0.0
    assert "perf-test-suite" in cal["baselines"]
    # the reference is chaotic: it is flagged by its own architecture checks
    assert cal["reference"]["checks"]["arch-layers"]["status"] == "fail"

    b = Benchmark.load(b.dir)  # reload calibration
    results = tmp_path / "results"
    scores = {}
    for agent in ("clean", "sloppy", "lazy"):
        ws = prepare(b, tmp_path / f"ws-{agent}")
        subprocess.run([sys.executable, str(AGENTS / f"{agent}_closure.py"), str(ws)], check=True)
        r = evaluate(b, ws, agent=agent, meta={"cost_usd": 0.1}, out_dir=results, log=quiet)
        scores[agent] = r["scores"]

    assert scores["clean"]["acceptable"] and scores["clean"]["total"] == 1.0
    assert scores["sloppy"]["acceptable"] and scores["sloppy"]["categories"]["architecture"] < 0.8
    assert scores["lazy"]["acceptable"] is False
    assert scores["clean"]["total"] > scores["sloppy"]["total"] > scores["lazy"]["total"]

    report = build_report(load_results(results))
    assert report.index("| clean") < report.index("| sloppy") < report.index("| lazy")


def test_empty_change_scores_zero(suite, tmp_path):
    tmp, _ = suite
    b = Benchmark.load(tmp / "benchmarks" / "bankapp.signup")
    ws = prepare(b, tmp_path / "ws")
    r = evaluate(b, ws, agent="noop", log=quiet)
    assert r["scores"]["total"] == 0.0 and r["scores"]["acceptable"] is False


def test_plugins_and_custom_checks(suite, tmp_path):
    tmp, _ = suite
    bdir = tmp / "benchmarks" / "bankapp.signup"
    data = yaml.safe_load((bdir / "benchmark.yaml").read_text())
    data["id"] = "bankapp.signup-custom"
    data["checks"].append({"id": "arch-todos", "type": "max_todo_comments", "params": {"max": 0}})
    data["checks"].append({"id": "br-custom", "type": "command", "category": "business_rules",
                           "params": {"run": "{python} -c \"import bankapp.application.signup\""}})
    custom = tmp / "benchmarks" / "custom"
    custom.mkdir()
    (custom / "benchmark.yaml").write_text(yaml.safe_dump(data))
    (custom / "task.md").write_text("# task")
    (custom / "hidden").symlink_to(bdir / "hidden")
    b = Benchmark.load(custom)                     # loads ../plugins/example_plugin.py
    ws = prepare(b, tmp_path / "ws")
    (ws / "bankapp/application/signup.py").write_text(
        (Path(b.repo_path) / "bankapp/application/signup.py").read_text() + "# TODO: rate limit\n")
    r = evaluate(b, ws, agent="x", log=quiet)
    by_id = {c["id"]: c for c in r["checks"]}
    assert by_id["arch-todos"]["status"] == "fail"
    assert by_id["br-custom"]["status"] == "pass"


def test_cli_smoke(suite, capsys):
    tmp, _ = suite
    assert main(["checks"]) == 0
    assert "layer_dependencies" in capsys.readouterr().out
    assert main(["validate", str(tmp / "benchmarks")]) == 0
