"""Fuzz harness setup: Hypothesis profiles, the `fuzz` marker, the network guard, and the result hook
that writes fuzz/results/deterministic.json (plus one violations.jsonl line per failing invariant).

    uv run --group dev pytest fuzz/deterministic -m fuzz -p no:cacheprovider
    HYPOTHESIS_PROFILE=deep uv run --group dev pytest fuzz/deterministic -m fuzz -p no:cacheprovider
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUZZ = ROOT / "fuzz"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fuzz import fakes  # noqa: E402,F401  (network guard and fake ServiceNow URL, before any app import)
from fuzz import invariants  # noqa: E402

import pytest  # noqa: E402
from hypothesis import HealthCheck, Phase, settings  # noqa: E402
from hypothesis.configuration import set_hypothesis_home_dir  # noqa: E402

set_hypothesis_home_dir(str(FUZZ / ".hypothesis"))  # not ./.hypothesis at the repo root
from hypothesis.database import DirectoryBasedExampleDatabase  # noqa: E402

PROFILE = os.environ.setdefault("HYPOTHESIS_PROFILE", "ci")
_DB = DirectoryBasedExampleDatabase(str(FUZZ / ".hypothesis" / "examples"))
_COMMON = dict(deadline=None, database=_DB, print_blob=True, report_multiple_bugs=False,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much,
                                      HealthCheck.function_scoped_fixture, HealthCheck.large_base_example])
# ci is derandomized: the same examples on every run, so a red run is reproducible and a green one is
# comparable. deep draws new examples every run and keeps failures in fuzz/.hypothesis for replay.
settings.register_profile("ci", max_examples=100, stateful_step_count=30, derandomize=True,
                          **{k: v for k, v in _COMMON.items() if k != "database"})
settings.register_profile("deep", max_examples=1000, stateful_step_count=50, **_COMMON)
# Replays only what the example database has, e.g. to re-check a fix quickly.
settings.register_profile("replay", max_examples=100, phases=[Phase.explicit, Phase.reuse], **_COMMON)
settings.load_profile(PROFILE)

RESULTS = FUZZ / "results"
_started = time.time()
_reports: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _no_real_servicenow():
    """Belt and braces: whatever a test forgets to fake, the ServiceNow client can't reach a network."""
    from app import config, servicenow

    old_http, old_url = servicenow._http, config.SN_INSTANCE_URL
    servicenow._http, config.SN_INSTANCE_URL = fakes.refusing_http, fakes.FAKE_SN_URL
    yield
    servicenow._http, config.SN_INSTANCE_URL = old_http, old_url


def pytest_configure(config):
    config.addinivalue_line("markers", "fuzz: property-based fuzz tests (fuzz/deterministic)")
    config.addinivalue_line("markers", "invariants(*ids): the invariants a fuzz test checks")
    config.addinivalue_line("filterwarnings", "ignore::hypothesis.errors.HypothesisWarning")


def pytest_sessionstart(session):
    if hasattr(session.config, "workerinput"):
        return
    RESULTS.mkdir(parents=True, exist_ok=True)
    # Deterministic violations are rewritten per run; the model runner's are kept.
    if invariants.VIOLATIONS.exists():
        kept = [v for v in invariants.read_violations() if v.get("source") != "deterministic"]
        invariants.VIOLATIONS.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in kept),
                                         encoding="utf-8")


def _category(item) -> str:
    name = Path(str(item.fspath)).stem
    m = re.match(r"test_(\w+?)_(fuzz|stateful)$", name)
    return m.group(1) if m else name


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/fuzz/" not in str(item.fspath).replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.fuzz)
        cat = _category(item)
        marker = item.get_closest_marker("invariants")
        ids = list(marker.args) if marker else invariants.CATEGORIES.get(cat, [])
        item.user_properties.append(("category", cat))
        item.user_properties.append(("invariants", ids))


# Hypothesis prints "Falsifying example:" (older) or "Failing test case:" (newer), then the arguments.
_FALSIFYING = re.compile(r"^((?:Falsifying (?:explicit )?example|Failing test case):.*?)"
                         r"(?=^(?:Explanation:|You can reproduce|Captured|state = )|\Z)", re.S | re.M)
_BLOB = re.compile(r"@reproduce_failure\([^)]*\)")


def reproducer_parts(text: str) -> dict:
    clean = re.sub(r"^E {0,11}", "", text, flags=re.M)  # pytest's "E   " prefix on exception lines
    falsifying = [m.group(1).rstrip() for m in _FALSIFYING.finditer(clean)]
    # Stateful tests print the steps as code: "state = Machine()" ... "state.teardown()".
    steps = re.search(r"(state = \w+\(\).*?state\.teardown\(\))", clean, re.S)
    steps_text = steps.group(1) if steps else ""
    falsifying = [f for f in falsifying if f.strip() not in ("Falsifying example:", "Failing test case:")]
    return {"falsifying": "\n\n".join(falsifying) or steps_text, "steps": steps_text,
            "reproduce_failure": "\n".join(dict.fromkeys(_BLOB.findall(clean)))}


def _rerun(nodeid: str) -> str:
    return f'HYPOTHESIS_PROFILE={PROFILE} uv run --group dev pytest "{nodeid}" -m fuzz -p no:cacheprovider'


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is None or "/fuzz/" not in str(item.fspath).replace("\\", "/"):
        return
    found = invariants.violation_ids(call.excinfo.value)
    report.user_properties.append(("violations", [
        {"id": v.inv_id, "detail": v.detail, "data": v.data} for v in found]))


def pytest_runtest_logreport(report):
    props = dict(report.user_properties)
    if "category" not in props:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        text = report.longreprtext if report.failed else ""
        parts = reproducer_parts(text) if text else {"falsifying": "", "steps": "", "reproduce_failure": ""}
        entry = {
            "nodeid": report.nodeid, "category": props["category"], "outcome": report.outcome,
            "duration": round(report.duration, 3), "invariants": props.get("invariants", []),
            "violations": [v["id"] for v in props.get("violations", [])],
            "reproducer": {**parts, "traceback": text[-6000:], "rerun": _rerun(report.nodeid)} if report.failed else None,
        }
        _reports[report.nodeid] = entry
        if report.failed:
            found = props.get("violations") or []
            if not found:
                # A failure that isn't a stated invariant (e.g. an exception from the harness itself).
                found = [{"id": "", "detail": text.strip().splitlines()[-1][:300] if text.strip() else "failed", "data": {}}]
            for v in found:
                invariants.record(v["id"] or "HARNESS", v["detail"], source="deterministic", nodeid=report.nodeid,
                                  reproducer="\n\n".join(filter(None, [parts["falsifying"], parts["reproduce_failure"]])),
                                  rerun=_rerun(report.nodeid), data=v.get("data"))


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput") or not _reports:
        return
    import hypothesis

    RESULTS.mkdir(parents=True, exist_ok=True)
    tests = sorted(_reports.values(), key=lambda r: r["nodeid"])
    out = {
        "kind": "deterministic",
        "started": datetime.datetime.fromtimestamp(_started).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - _started, 1),
        "profile": PROFILE,
        "hypothesis": hypothesis.__version__,
        "python": platform.python_version(),
        "commit": _git("rev-parse", "--short", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git("status", "--porcelain", "--", "app")),
        "seed": os.environ.get("HYPOTHESIS_SEED", ""),
        "exit_status": int(exitstatus),
        "summary": {o: sum(1 for t in tests if t["outcome"] == o) for o in ("passed", "failed", "skipped")},
        "tests": tests,
    }
    (RESULTS / "deterministic.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str),
                                                encoding="utf-8")
