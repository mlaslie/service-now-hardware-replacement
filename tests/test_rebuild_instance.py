"""scripts/rebuild_instance.py: the pieces that don't need ServiceNow or Google Cloud."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rebuild_instance as rb  # noqa: E402

NEW = "https://dev99999.service-now.com"


def test_seed_line_is_parsed_and_checked_against_the_instance():
    assert rb.parse_seed_line(f"SEED_CLIENT={NEW}/|abc123|s3cret", NEW) == (NEW, "abc123", "s3cret")
    assert rb.parse_seed_line(f"  SEED_CLIENT={NEW}|abc123|s3cret  ", NEW) == (NEW, "abc123", "s3cret")
    assert rb.parse_seed_line(f"{NEW}|abc123|s3cret", NEW) == (NEW, "abc123", "s3cret")  # without the prefix
    with pytest.raises(ValueError, match="not https://dev99999"):
        rb.parse_seed_line("SEED_CLIENT=https://dev11111.service-now.com|abc|s", NEW)
    for bad in ["", "SEED_CLIENT=", f"SEED_CLIENT={NEW}|abc", f"SEED_CLIENT={NEW}||s", f"{NEW}|a|b|c"]:
        with pytest.raises(ValueError):
            rb.parse_seed_line(bad, NEW)


def test_instance_urls():
    assert rb.normalize_instance(" https://dev99999.service-now.com/ ") == NEW
    assert rb.instance_label(NEW) == "dev99999"
    for bad in ["dev99999.service-now.com", "http://dev99999.service-now.com", "https://x.com/path"]:
        with pytest.raises(SystemExit):
            rb.normalize_instance(bad)


def test_replace_instance_only_swaps_the_old_one():
    text = "SN_INSTANCE_URL=https://dev11111.service-now.com\nOTHER=https://dev22222.service-now.com\n"
    out = rb.replace_instance(text, "https://dev11111.service-now.com/", NEW)
    assert f"SN_INSTANCE_URL={NEW}\n" in out and "dev22222" in out
    assert rb.replace_instance(text, "", NEW) == text and rb.replace_instance(text, NEW, NEW) == text


def test_bootstrap_runs_the_role_first_and_ends_with_the_seed_line():
    script = rb.bootstrap_script()
    assert script.index("u_hardware_requester") < script.index("Hardware Seed Script")
    assert script.rstrip().endswith("})();") and "SEED_CLIENT=" in script.split("Hardware Seed Script", 1)[1]
    assert "localhost:8765/callback" in script  # the seed tool's redirect


def test_authorization_body_matches_what_gemini_enterprise_needs():
    body = rb.authorization_body(NEW, "cid", "csecret")["serverSideOauth2"]
    assert body["clientId"] == "cid" and body["clientSecret"] == "csecret" and body["scopes"] == ["useraccount"]
    assert body["authorizationUri"].startswith(f"{NEW}/oauth_auth.do?response_type=code&client_id=cid&")
    assert "redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect" in body["authorizationUri"]
    assert body["tokenUri"] == f"{NEW}/oauth_token.do"


def test_passwords_meet_common_policies():
    pw = {rb.new_password() for _ in range(20)}
    assert len(pw) == 20
    for p in pw:
        assert len(p) >= 16 and any(c.islower() for c in p) and any(c.isupper() for c in p)
        assert any(c.isdigit() for c in p) and any(not c.isalnum() for c in p)


def test_config_moves_an_old_manifest_aside_and_records_the_change(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "seed" / "state").mkdir(parents=True)
    (root / "docs").mkdir()
    old = "https://dev11111.service-now.com"
    (root / ".env").write_text(f"SN_INSTANCE_URL={old}\n")
    (root / ".pii-patterns").write_text("someone\n")
    (root / "docs" / "HANDOFF.local.md").write_text("# Handoff\n")
    (root / "seed" / "state" / "manifest.json").write_text(json.dumps({"instance": old}))
    monkeypatch.setattr(rb, "ROOT", root)
    monkeypatch.setattr(rb, "MCP_REPO", tmp_path / "absent")
    monkeypatch.setattr(rb, "awake", lambda url: True)
    monkeypatch.setenv("SN_INSTANCE_URL", old)
    rb.step_config(NEW, None)
    rb.step_config(NEW, None)  # repeatable
    assert (root / ".env").read_text() == f"SN_INSTANCE_URL={NEW}\n"
    assert (root / ".pii-patterns").read_text().split() == ["someone", "dev99999"]
    assert (root / "seed" / "state" / "manifest.dev11111.json").exists()
    assert not (root / "seed" / "state" / "manifest.json").exists()
    assert (root / "docs" / "HANDOFF.local.md").read_text().count("changed from https://dev11111") == 1


def test_config_stops_when_the_instance_is_asleep(monkeypatch):
    monkeypatch.setattr(rb, "awake", lambda url: False)
    with pytest.raises(SystemExit, match="isn't answering"):
        rb.step_config(NEW, None)
