"""The organization profile and deployment settings: load, validate, explain mistakes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app import config, profile

ROOT = Path(__file__).resolve().parents[1]
HOSPITAL = ROOT / "config" / "organization.yaml"
OFFICE = ROOT / "config" / "examples" / "office.yaml"


def _write(tmp_path, data) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _hospital() -> dict:
    return yaml.safe_load(HOSPITAL.read_text())


def test_shipped_profiles_are_valid():
    for path in (HOSPITAL, OFFICE):
        p = profile.load(path)
        assert p.keys("personal") and p.keys("equipment")


def test_hospital_profile_matches_the_documented_behaviour():
    p = profile.load(HOSPITAL)
    assert p.branding.primary_color == "#22873B"
    assert p.min_urgency("safety_concern") == "critical" and p.is_safety("safety_concern")
    assert p.photo_policy("cracked_screen")[0] == "required"
    assert p.photo_policy("safety_concern") == (None, "")  # never slow down an unsafe-equipment report
    assert p.issue("performance").recommendation.startswith("Remote diagnostics")


@pytest.mark.parametrize("change, message", [
    (lambda d: d["branding"].update(primary_color="green"), "branding.primary_color"),
    (lambda d: d["issues"]["personal"].append(dict(d["issues"]["personal"][0])), "duplicate keys"),
    (lambda d: d["issues"]["equipment"][0].update(photo="required"), "photo_of is empty"),
    (lambda d: d["service"]["equipment_response_targets"].pop("1"), "priorities ['1']"),
    (lambda d: d["servicenow"].update(ticket_categroy="x"), "ticket_categroy"),  # misspelled key
    (lambda d: d["issues"]["personal"][0].update(key="Cracked Screen"), "issues.personal.0.key"),
    (lambda d: d["issues"]["equipment"][-1].update(label="Other"), "different labels"),
])
def test_mistakes_are_named(tmp_path, change, message):
    data = _hospital()
    change(data)
    with pytest.raises(profile.ProfileError) as err:
        profile.load(_write(tmp_path, data))
    assert message in str(err.value), str(err.value)


def test_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(profile.ProfileError, match="config/examples/office.yaml"):
        profile.load(tmp_path / "nope.yaml")


def test_check_command(capsys):
    assert profile.main(["x", str(OFFICE)]) == 0
    assert "Example Corp" in capsys.readouterr().out


def test_the_agent_runs_on_another_profile():
    """Swap the profile (a fresh interpreter, since it is loaded once) and the cards, agent card
    and instructions follow it."""
    code = """
import json
from app import cards, agent, server, tools
print(json.dumps({
    "color": cards.PRIMARY_COLOR,
    "personal": [k for k, _ in cards.ISSUE_CATEGORIES],
    "card": server.build_agent_card().name,
    "persona": agent.INSTRUCTION.startswith("You are the Hardware Help assistant"),
    "set_issue_doc": "printer" not in tools.set_issue.__doc__ and "not_working" in tools.set_issue.__doc__,
}))
"""
    env = dict(os.environ, ORGANIZATION_PROFILE=str(OFFICE), GOOGLE_CLOUD_PROJECT="p", AGENT_ENGINE_ID="1",
               SN_INSTANCE_URL="https://example.service-now.com")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"color": "#1A56DB", "personal": ["cracked_screen", "wont_power_on", "battery",
                                                       "keyboard_trackpad", "performance", "other"],
                      "card": "Hardware Help", "persona": True, "set_issue_doc": True}


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED", {"GOOGLE_CLOUD_PROJECT": "p", "AGENT_ENGINE_ID": "", "SN_INSTANCE_URL": ""})
    monkeypatch.setattr(config, "SN_INSTANCE_URL", "")
    with pytest.raises(config.ConfigError) as err:
        config.require()
    assert "AGENT_ENGINE_ID is not set" in str(err.value) and "SN_INSTANCE_URL is not set" in str(err.value)


def test_instance_url_must_be_https(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED", {"X": "set"})
    monkeypatch.setattr(config, "SN_INSTANCE_URL", "acme.service-now.com")
    with pytest.raises(config.ConfigError, match="https://"):
        config.require()


def test_env_file_never_overrides(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nHW_TEST_A=from-file\nHW_TEST_B = 'quoted'\n")
    monkeypatch.setenv("HW_TEST_A", "already-set")
    monkeypatch.delenv("HW_TEST_B", raising=False)
    config.load_env_file(env)
    assert os.environ["HW_TEST_A"] == "already-set" and os.environ["HW_TEST_B"] == "quoted"
