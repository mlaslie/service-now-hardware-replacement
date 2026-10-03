"""set -> report -> reset round trip against an in-memory ServiceNow."""

import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "seed"))
import sn_seed  # noqa: E402

pytest.importorskip("reportlab")


class FakeSN:
    """Just enough of the Table API for the encoded queries sn_seed uses."""

    def __init__(self):
        self.client = sn_seed.OAuthClient("https://fake.service-now.com", "id", "secret")
        self.tables: dict[str, dict[str, dict]] = {
            "sys_db_object": {"t1": {"sys_id": "t1", "name": "alm_license"}},
            "sys_user_role": {f"r{i}": {"sys_id": f"r{i}", "name": n} for i, n in enumerate(
                ["itil", "sn_incident_write", "asset", "u_hardware_requester"])},
            "cmdb_model_category": {f"c{i}": {"sys_id": f"c{i}", "name": n} for i, n in enumerate(
                ["Computer", "Computer Monitor", "Computer Peripheral", "IP Phone", "Mobile Device", "Printer"])},
            "sys_user": {"u0": {"sys_id": "u0", "user_name": "abel.tuter", "first_name": "Abel"},
                         "u1": {"sys_id": "u1", "user_name": "john.doe", "first_name": "Old", "email": "old@x",
                                "title": "", "phone": "", "department": "", "cost_center": "", "manager": "",
                                "location": "", "active": "true"}},
        }
        self.ids = itertools.count(100)
        self.deleted: list[tuple[str, str]] = []

    @staticmethod
    def _match(row: dict, q: str) -> bool:
        groups = [[]]
        for cond in q.replace("^^", "\0").split("^"):
            cond = cond.replace("\0", "^")
            if not cond or cond.startswith("ORDERBY"):
                continue
            if cond.startswith("OR"):
                groups.append([cond[2:]])
            else:
                groups[-1].append(cond)
        def ok(c):
            for op in ("IN", "LIKE", "="):
                if op in c:
                    k, v = c.split(op, 1)
                    val = str(row.get(k, ""))
                    return val in v.split(",") if op == "IN" else (v in val if op == "LIKE" else val == v)
            return False
        return any(all(ok(c) for c in g) for g in groups if g)

    def rows(self, table):
        if table == "alm_asset":
            return list(self.tables.get("alm_hardware", {}).values()) + list(self.tables.get("alm_license", {}).values())
        return list(self.tables.get(table, {}).values())

    def query(self, table, q, fields="sys_id", limit=1000):
        return [dict(r) for r in self.rows(table) if self._match(r, q)][:limit]

    def one(self, table, q, fields="sys_id"):
        rows = self.query(table, q, fields, 1)
        return rows[0] if rows else None

    def get(self, table, sys_id, fields):
        return dict(self.tables.get(table, {}).get(sys_id, {}))

    def create(self, table, data):
        sys_id = f"s{next(self.ids)}"
        row = {"sys_id": sys_id, **data}
        if table in ("alm_hardware", "alm_license"):
            row["sys_class_name"] = table
        self.tables.setdefault(table, {})[sys_id] = row
        return dict(row)

    def update(self, table, sys_id, data):
        self.tables[table][sys_id].update(data)
        return dict(self.tables[table][sys_id])

    def delete(self, table, sys_id):
        if table == "cmdb_ci":  # the base table: the record lives in its class table
            table = next(t for t, rows in self.tables.items() if t.startswith("cmdb_ci") and sys_id in rows)
        self.tables[table].pop(sys_id)
        self.deleted.append((table, sys_id))

    def table_exists(self, table):
        return self.one("sys_db_object", f"name={table}") is not None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sn_seed, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sn_seed, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(sn_seed, "log", lambda msg: None)
    users = tmp_path / "users.json"
    users.write_text((sn_seed.HERE / "users.example.json").read_text())
    return FakeSN(), users, tmp_path


def test_set_is_idempotent_and_issues_full_kit(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    hw, lic = sn.tables["alm_hardware"], sn.tables["alm_license"]
    john = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "john.doe")
    assert john["sys_id"] == "u1" and john["first_name"] == "John"  # existing user updated, not duplicated
    assert sum(r["assigned_to"] == "u1" for r in hw.values()) == 8
    assert sum(r["assigned_to"] == "u1" for r in lic.values()) == 4
    jane = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "jane.doe")
    assert sum(r["assigned_to"] == jane["sys_id"] for r in list(hw.values()) + list(lic.values())) == 12
    assert all(r["comments"].startswith(sn_seed.SEED_MARK) for r in hw.values())
    assert len({r["asset_tag"] for r in list(hw.values()) + list(lic.values())}) == len(hw) + len(lic)

    before = len(hw) + len(lic)
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")  # second run adds nothing
    assert len(sn.tables["alm_hardware"]) + len(sn.tables["alm_license"]) == before


def test_report_builds_a_pdf(env):
    sn, users, tmp = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    out = tmp / "report.pdf"
    sn_seed.cmd_report(sn, out)
    assert out.read_bytes()[:4] == b"%PDF" and out.stat().st_size > 3000


def test_reset_undoes_everything(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    jane = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "jane.doe")
    sn.tables["incident"] = {
        "i1": {"sys_id": "i1", "number": "INC1", "caller_id": "u1", "correlation_display": sn_seed.AGENT_MARK},
        "i2": {"sys_id": "i2", "number": "INC2", "opened_by": jane["sys_id"]},
        "i3": {"sys_id": "i3", "number": "INC3", "caller_id": "u0"},  # someone else's: kept
        # john.doe existed before the seed: his own (non-agent) work is kept.
        "i4": {"sys_id": "i4", "number": "INC4", "caller_id": "u1", "correlation_display": ""},
    }
    sn_seed.cmd_reset(sn, yes=False, memory=False)  # dry run changes nothing
    assert len(sn.tables["incident"]) == 4

    sn_seed.cmd_reset(sn, yes=True, memory=False)
    assert sorted(sn.tables["incident"]) == ["i3", "i4"]
    assert not sn.tables["alm_hardware"] and not sn.tables["alm_license"]
    assert set(sn.tables["sys_user"]) == {"u0", "u1"}  # created user gone, pre-existing kept
    assert sn.tables["sys_user"]["u1"]["first_name"] == "Old" and sn.tables["sys_user"]["u1"]["email"] == "old@x"
    assert not sn.tables.get("cmn_location") and not sn.tables.get("core_company")
    assert not sn_seed.MANIFEST.exists()


def test_reset_one_user_leaves_the_rest(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    jane = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "jane.doe")
    sn.tables["incident"] = {"i1": {"sys_id": "i1", "number": "INC1", "caller_id": jane["sys_id"]},
                             "i2": {"sys_id": "i2", "number": "INC2", "caller_id": "u1"}}
    john_items = sum(r["assigned_to"] == "u1" for r in sn.tables["alm_hardware"].values())

    sn_seed.cmd_reset(sn, yes=True, memory=False, only={"jane.doe"})
    assert list(sn.tables["incident"]) == ["i2"]  # only Jane's ticket
    assert jane["sys_id"] not in sn.tables["sys_user"]
    assert not any(r["assigned_to"] == jane["sys_id"] for r in sn.tables["alm_hardware"].values())
    assert sum(r["assigned_to"] == "u1" for r in sn.tables["alm_hardware"].values()) == john_items
    assert sn.tables["cmn_location"]  # shared records kept
    m = sn_seed.Manifest.load()
    assert set(m.users) == {"john.doe"}


def test_manager_is_resolved(env):
    sn, users, _ = env
    sn.tables["sys_user"]["u0"]["user_name"] = "abel.tuter"
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    assert sn.tables["sys_user"]["u1"]["manager"] == "u0"  # john.doe's manager


def _equipment(sn):
    return {r["asset_tag"]: r for r in sn.tables["alm_hardware"].values() if r.get("comments") == f"{sn_seed.SEED_MARK} equipment"}


def test_hospital_equipment_belongs_to_departments(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    eq = _equipment(sn)
    assert len(eq) == 8
    mri = eq["CE-10421"]
    names = {t: {sid: r.get("name") for sid, r in sn.tables[t].items()}
             for t in ("cmn_department", "sys_user_group", "cmn_location", "cmdb_model_category")}
    assert mri["assigned_to"] == "" and names["cmn_department"][mri["department"]] == "Radiology"
    assert names["sys_user_group"][mri["support_group"]] == "Imaging Engineering"
    assert names["cmn_location"][mri["location"]] == "Riverside Medical Center - Radiology - MRI 1 (Room 104)"
    assert names["cmdb_model_category"][mri["model_category"]] == "Imaging Equipment"
    assert names["cmdb_model_category"][eq["610204"]["model_category"]] == "Computer"  # existing category reused
    ci = sn.tables["cmdb_ci_hardware"][mri["ci"]]  # tickets link to it
    assert ci["asset"] == mri["sys_id"] and ci["asset_tag"] == "CE-10421" and ci["department"] == mri["department"]
    jane = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "jane.doe")
    assert names["cmn_department"][jane["department"]] == "Radiology"
    service_desk = next(sid for sid, n in names["sys_user_group"].items() if n == "Service Desk")
    assert [m["group"] for m in sn.tables["sys_user_grmember"].values() if m["user"] == "u1"] == [service_desk]

    assert set(sn_seed.Manifest.load().equipment) == set(eq)  # listed for the report
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")  # idempotent
    assert len(_equipment(sn)) == 8 and len(sn.tables["sys_user_grmember"]) == 1

    manifest = sn_seed.Manifest.load()
    manifest.equipment = {}
    manifest.save()
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")  # a lost list is rebuilt from the seed mark
    assert set(sn_seed.Manifest.load().equipment) == set(eq) and len(_equipment(sn)) == 8


def test_reset_removes_equipment_but_one_user_reset_keeps_it(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    sn_seed.cmd_reset(sn, yes=True, memory=False, only={"john.doe"})
    assert len(_equipment(sn)) == 8 and not sn.tables["sys_user_grmember"]
    sn_seed.cmd_reset(sn, yes=True, memory=False)
    assert not _equipment(sn) and not sn.tables["sys_user_group"] and not sn.tables["cmn_department"]
    assert not sn.tables["cmdb_ci_hardware"]
    left = {r["name"] for r in sn.tables["cmdb_model_category"].values()}
    assert "Computer" in left and not left & {"Imaging Equipment", "Patient Care Equipment"}  # only ours removed


def test_roles_granted_and_removed(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    jane = next(r for r in sn.tables["sys_user"].values() if r["user_name"] == "jane.doe")
    roles = lambda uid: {g["role"] for g in sn.tables["sys_user_has_role"].values() if g["user"] == uid}  # noqa: E731
    assert roles(jane["sys_id"]) == {"r3"}            # Jane: the custom requester role only
    assert roles("u1") == {"r0", "r1", "r2"}         # John: itil, sn_incident_write, asset
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")  # not granted twice
    assert len(sn.tables["sys_user_has_role"]) == 4
    sn_seed.cmd_reset(sn, yes=True, memory=False)
    assert not sn.tables["sys_user_has_role"]


def test_clear_tickets_keeps_people_and_equipment(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json")
    mri = _equipment(sn)["CE-10421"]
    sn.tables["incident"] = {
        "i1": {"sys_id": "i1", "number": "INC1", "caller_id": "u1", "correlation_display": sn_seed.AGENT_MARK},
        "i2": {"sys_id": "i2", "number": "INC2", "caller_id": "u0", "cmdb_ci": mri.get("ci", "")},
        "i3": {"sys_id": "i3", "number": "INC3", "caller_id": "u0"},  # unrelated
        "i4": {"sys_id": "i4", "number": "INC4", "caller_id": "u1"},  # pre-existing user's own work: kept
    }
    sn_seed.cmd_clear_tickets(sn, yes=True)
    assert sorted(sn.tables["incident"]) == ["i3", "i4"]
    assert len(_equipment(sn)) == 8 and sn.tables["sys_user"]["u1"]


def test_an_office_seed_has_no_equipment(env):
    sn, users, _ = env
    sn_seed.cmd_set(sn, users, sn_seed.HERE / "catalog.json", None)
    assert not [r for r in sn.tables.get("alm_hardware", {}).values() if (r.get("asset_tag") or "").startswith("CE-")]
    assert sn.tables["alm_hardware"]  # people still get their own devices
