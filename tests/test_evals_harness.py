"""The eval checker itself (no model): it must catch every kind of failure."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.run import CASES, check_turn  # noqa: E402

CALLS = [{"name": "find_device", "args": {"description": "the MRI in room 104"}},
         {"name": "set_issue", "args": {"category": "error_alarm", "urgency": "high"}}]


def test_a_matching_turn_passes():
    assert check_turn({"calls": ["find_device", "set_issue"], "args": {"set_issue": {"category": "error_alarm"}},
                       "not_calls": ["submit_ticket"], "reply_contains": ["right device"]},
                      CALLS, "Is this the right device?") == []


def test_each_kind_of_failure_is_reported():
    problems = check_turn({"calls": ["set_issue", "find_device"],          # wrong order
                           "args": {"find_device": {"description": "pump"}},  # wrong argument
                           "not_calls": ["set_issue"],                     # forbidden call made
                           "reply_contains": ["submitted"],                # missing text
                           "reply_not_contains": ["device"]},              # forbidden text
                          CALLS, "Is this the right device?")
    assert len(problems) == 5, problems


def test_no_calls_expectation():
    assert check_turn({"no_calls": True}, CALLS, "") == ["expected no tool calls, got ['find_device', 'set_issue']"]


def test_cases_file_is_well_formed():
    cases = yaml.safe_load(CASES.read_text())
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and all(c["turns"] for c in cases)
    allowed = {"calls", "not_calls", "args", "no_calls", "reply_contains", "reply_not_contains"}
    for c in cases:
        for t in c["turns"]:
            assert set(t.get("expect") or {}) <= allowed, (c["id"], t)
