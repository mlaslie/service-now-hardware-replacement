"""scripts/sn_profile.py against an in-memory ServiceNow: check and import-issues."""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sn_profile  # noqa: E402

from app import profile  # noqa: E402

CHOICES = {
    ("category", ""): ["inquiry", "software", "hardware"],
    ("subcategory", "hardware"): ["cpu", "keyboard", "memory", "mouse", "disk", "monitor"],
    ("contact_type", ""): ["phone", "self-service"],
    ("impact", ""): ["1", "2", "3"],
    ("urgency", ""): ["1", "2", "3"],
}


class FakeSN:
    def __init__(self, fields=("subcategory", "contact_type", "category", "impact", "urgency")):
        self.fields = set(fields)

    def query(self, table, q, fields="", limit=1000):
        conds = dict(c.split("=", 1) for c in q.split("^") if "=" in c and "IN" not in c.split("=")[0])
        if table == "sys_choice":
            element, dep = conds.get("element"), conds.get("dependent_value", "")
            values = CHOICES.get((element, dep), [])
            return [{"value": v, "label": v.title(), "sequence": str(i)} for i, v in enumerate(values)]
        if table == "sys_dictionary":
            return [{"element": conds["element"]}] if conds.get("element") in self.fields else []
        if table == "sys_db_object":
            return [{"name": "alm_hardware"}] if conds.get("name") == "alm_hardware" else []
        if table == "cmdb_model_category":
            return [{"name": "Imaging Equipment"}, {"name": "Patient Care Equipment"}]
        return []


def test_shipped_profile_passes():
    findings = sn_profile.check(FakeSN(), profile.load(ROOT / "config" / "organization.yaml"))
    assert not [f for f in findings if f.level == "fail"], findings


def _fails(tmp_path, change):
    data = yaml.safe_load((ROOT / "config" / "organization.yaml").read_text())
    change(data["servicenow"])
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    return [f.what for f in sn_profile.check(FakeSN(), profile.load(path)) if f.level == "fail"]


def test_unknown_category_fails(tmp_path):
    fails = _fails(tmp_path, lambda s: s.update(ticket_category="hw"))
    assert any("ticket_category 'hw' is not an incident category" in f for f in fails)


def test_value_that_is_not_a_choice_fails(tmp_path):
    fails = _fails(tmp_path, lambda s: s["device_values"].update(laptop="notebook"))
    assert any("device_values.laptop -> 'notebook' is not a choice" in f for f in fails)


def test_missing_field_fails(tmp_path):
    fails = _fails(tmp_path, lambda s: s["ticket_fields"].update(u_hardware_type="{device_value}"))
    assert any("ticket_fields.u_hardware_type: no such incident field" in f for f in fails)


def test_import_keeps_rules_and_maps_values():
    data = yaml.safe_load((ROOT / "config" / "organization.yaml").read_text())
    data["issues"]["personal"].append({"key": "keyboard", "label": "Keys", "photo": "required", "photo_of": "the keys"})
    p = profile.Profile.model_validate(data)
    out = yaml.safe_load(sn_profile.import_issues(FakeSN(), p, "subcategory", "hardware", "personal"))
    entries = out["issues"]["personal"]
    assert [e["key"] for e in entries] == ["cpu", "keyboard", "memory", "mouse", "disk", "monitor"]
    keyboard = entries[1]
    assert keyboard["label"] == "Keyboard" and keyboard["photo"] == "required"  # label from ServiceNow, rules kept
    profile.Issues.model_validate({"personal": entries, "equipment": data["issues"]["equipment"]})  # valid YAML
