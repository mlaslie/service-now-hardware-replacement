"""ServiceNow permission checker: can this user do everything the agent does for them?

Run as a ServiceNow admin (the same sign-in as the seed tool). For each user you
name, it **impersonates** them (no password needed), performs every call the
agent makes with the same fields, then reads back as admin what ServiceNow
actually stored. ServiceNow drops fields a user may not set without an error,
so "saved" is checked, not assumed.

    uv run python scripts/sn_doctor.py --as jane.doe               # check an existing user
    uv run python scripts/sn_doctor.py --as jane.doe --read-only   # no test ticket (production)
    uv run python scripts/sn_doctor.py --matrix                    # role matrix with temporary users
    uv run python scripts/sn_doctor.py --matrix --markdown docs/role-matrix.md
    uv run python scripts/sn_doctor.py --matrix --persona none --persona u_hardware_requester --persona itil

What it writes: one test ticket per user (and one "someone else's" ticket, created
as admin), deleted at the end. `--matrix` also creates temporary users (no
password, deleted at the end). Impersonation is recorded in ServiceNow's logs.
Sign in first: `uv run --group seed python seed/sn_seed.py login` (as an admin).
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "seed"))
sys.path.insert(0, str(ROOT))
import sn_seed  # noqa: E402  (OAuth sign-in, .env)

from app.profile import load as load_profile  # noqa: E402

TICKET_CATEGORY = load_profile().servicenow.ticket_category
MARK = "[sn_doctor]"
# A 1x1 PNG, for the attachment check.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

# Role sets measured by --matrix. Order: least to most.
PERSONAS = {
    "no roles (typical employee)": [],
    "itil": ["itil"],
    "itil + sn_incident_write": ["itil", "sn_incident_write"],
    "itil + sn_incident_write + asset": ["itil", "sn_incident_write", "asset"],
}


# --- sessions ----------------------------------------------------------------------------


class Admin:
    """The admin's OAuth session: setup, read-back and clean-up."""

    def __init__(self):
        client = sn_seed.load_client()
        self.instance = client.instance
        self.token = sn_seed.access_token(client)
        self.http = httpx.Client(base_url=self.instance, timeout=30,
                                 headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})

    def get(self, path: str, **params) -> dict:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json().get("result") or {}

    def query(self, table: str, q: str, fields: str = "sys_id", limit: int = 100) -> list[dict]:
        return self.get(f"/api/now/table/{table}", sysparm_query=q, sysparm_fields=fields,
                        sysparm_limit=limit, sysparm_exclude_reference_link="true") or []

    def create(self, table: str, data: dict) -> dict:
        r = self.http.post(f"/api/now/table/{table}", json=data, params={"sysparm_exclude_reference_link": "true"})
        r.raise_for_status()
        return r.json()["result"]

    def delete(self, table: str, sys_id: str) -> bool:
        """Best-effort cleanup, but never silent: a record left behind is printed."""
        try:
            r = self.http.delete(f"/api/now/table/{table}/{sys_id}")
            ok = r.status_code in (200, 204, 404)
        except httpx.HTTPError as exc:
            ok, r = False, exc
        if not ok:
            print(f"  ! could not delete {table}/{sys_id} ({getattr(r, 'status_code', type(r).__name__)}); "
                  "remove it by hand", file=sys.stderr)
        return ok

    def impersonate(self, user_sys_id: str) -> httpx.Client:
        """A cookie session that acts as the user. The admin token only starts it."""
        session = httpx.Client(base_url=self.instance, timeout=30, headers={"Accept": "application/json"})
        r = session.post(f"/api/now/ui/impersonate/{user_sys_id}", json={},
                         headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 400:
            raise SystemExit(f"Impersonation failed ({r.status_code}): {r.text[:200]}. The signed-in "
                             "account must be an admin (or have the impersonator role).")
        return session


# --- checks --------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    feature: str          # what the agent uses it for
    status: str = "skip"  # pass | partial | fail | skip
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    user: str
    roles: list[str]
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kw) -> Check:
        c = Check(*args, **kw)
        self.checks.append(c)
        return c


def _ok(r: httpx.Response) -> bool:
    return r.status_code < 400 and "json" in r.headers.get("content-type", "")


def _result(r: httpx.Response):
    try:
        return r.json().get("result")
    except ValueError:
        return None


def _val(v) -> str:
    return v.get("value", "") if isinstance(v, dict) else (v or "")


class Doctor:
    def __init__(self, admin: Admin, read_only: bool, asset_tag: str | None):
        self.admin = admin
        self.read_only = read_only
        self.asset = self._sample_asset(asset_tag)
        self.cleanup: list[tuple[str, str]] = []

    def _sample_asset(self, tag: str | None) -> dict:
        """Equipment with a department, support group and CI: exercises every lookup."""
        q = f"asset_tag={tag}" if tag else "departmentISNOTEMPTY^support_groupISNOTEMPTY^assigned_toISEMPTY^install_status!=7"
        rows = self.admin.query("alm_hardware", q, "sys_id,asset_tag,department,support_group,location,ci", 20)
        for row in rows:
            ci = row.get("ci") or next((c["sys_id"] for c in self.admin.query("cmdb_ci", f"asset={row['sys_id']}")), "")
            if ci:
                return row | {"ci": ci}
        raise SystemExit("No equipment with a department, support group and configuration item found. "
                         "Run the seed tool (equipment.json) or pass --asset TAG.")

    # -- one user

    def run(self, user: dict, roles: list[str]) -> Report:
        rep = Report(user["user_name"], roles)
        s = self.admin.impersonate(user["sys_id"])
        try:
            self._identity(s, user, rep)
            self._devices(s, user, rep)
            if self.read_only:
                for name, feature in (("Create a ticket", "filing a request"), ("Follow someone else's ticket",
                                      "joining an open ticket on shared equipment")):
                    rep.add(name, feature, "skip", "--read-only")
            else:
                self._tickets(s, user, rep)
        finally:
            try:
                s.get("/logout.do")  # end the impersonated session on the server, not just locally
            except httpx.HTTPError:
                pass
            s.close()
            for table, sys_id in reversed(self.cleanup):
                self.admin.delete(table, sys_id)
            self.cleanup.clear()
        return rep

    def _limits(self, s: httpx.Client, user: dict, rep: Report, admin_id: str, other: str) -> None:
        """What a requester should NOT be able to do. PASS = refused; PART = allowed, i.e. the role
        grants more than the agent needs (expected for itil, a fulfiller role)."""
        before = self.admin.get(f"/api/now/table/incident/{other}", sysparm_exclude_reference_link="true",
                                sysparm_fields="short_description,caller_id,assignment_group,watch_list")
        s.patch(f"/api/now/table/incident/{other}", json={
            "short_description": f"{MARK} changed by a follower", "caller_id": user["sys_id"]})
        after = self.admin.get(f"/api/now/table/incident/{other}", sysparm_exclude_reference_link="true",
                               sysparm_fields="short_description,caller_id")
        changed = [k for k in ("short_description", "caller_id") if _val(after.get(k)) != _val(before.get(k))]
        c = rep.add("Limit: edit someone else's ticket", "only the reporter changes a ticket")
        c.status, c.detail = ("pass", "refused") if not changed else ("partial", "allowed: " + ", ".join(changed))
        if changed:
            c.hint = "The role can edit others' tickets beyond notes and following."
            self.admin.http.patch(f"/api/now/table/incident/{other}", json={
                k: _val(before.get(k)) for k in ("short_description", "caller_id")})

        s.patch(f"/api/now/table/incident/{other}", json={"watch_list": user["sys_id"]})
        got = self.admin.get(f"/api/now/table/incident/{other}", sysparm_fields="watch_list",
                             sysparm_exclude_reference_link="true")
        kept = admin_id in _val(got.get("watch_list"))
        c = rep.add("Limit: remove other followers", "followers can only add or remove themselves")
        c.status, c.detail = ("pass", "refused") if kept else ("partial", "other followers were removed")
        if not kept:
            c.hint = "Install the follow-only business rule (create_hardware_requester_role.js)."

        hidden = self.admin.create("incident", {"caller_id": admin_id, "category": "inquiry",
                                                "short_description": f"{MARK} not a hardware ticket"})
        self.cleanup.append(("incident", hidden["sys_id"]))
        r = s.get("/api/now/table/incident", params={"sysparm_query": f"sys_id={hidden['sys_id']}",
                                                      "sysparm_fields": "sys_id"})
        seen = _ok(r) and bool(_result(r))
        c = rep.add("Limit: read a non-hardware ticket", "requesters see hardware tickets only")
        c.status, c.detail = ("pass", "hidden") if not seen else ("partial", "readable")
        if seen:
            c.hint = "The role reads incidents outside the hardware category."

    def _identity(self, s: httpx.Client, user: dict, rep: Report) -> None:
        r = s.get("/api/now/ui/user/current_user")
        me = _result(r) or {}
        c = rep.add("Who am I", "knowing who is asking (every turn)")
        if me.get("user_sys_id") != user["sys_id"]:
            c.status, c.detail = "fail", f"current_user returned {me.get('user_name')!r}"
            return
        r = s.get(f"/api/now/table/sys_user/{user['sys_id']}", params={
            "sysparm_fields": "name,email,department.name,cost_center.name,manager.name,location.street,location.city"})
        rec = _result(r) or {}
        missing = [f for f in ("name", "email", "department.name", "location.street") if not rec.get(f)]
        c.status = "pass" if not missing else "partial"
        c.detail = "name, email, department, address readable" if not missing else f"blank or hidden: {missing}"
        if missing:
            c.hint = "Fill these on the user record; the ship-to comes from location.street/city."

        r = s.get("/api/now/table/sys_user_grmember", params={"sysparm_query": f"user={user['sys_id']}",
                                                             "sysparm_fields": "group"})
        seen = len(_result(r) or []) if _ok(r) else -1
        total = len(self.admin.query("sys_user_grmember", f"user={user['sys_id']}"))
        c = rep.add("Read own group memberships", "recognising equipment the user's group supports")
        if seen < 0:
            c.status, c.detail = "fail", "access denied"
        elif total == 0:
            c.status, c.detail = "skip", "the user is in no groups"
        else:
            c.status = "pass" if seen == total else "fail"
            c.detail = f"{seen} of {total} visible"
        if c.status == "fail":
            c.hint = "sys_user_grmember read: needs itil, group_viewer, sn_cmdb_user or user_admin."

    def _devices(self, s: httpx.Client, user: dict, rep: Report) -> None:
        def count(q):
            r = s.get("/api/now/table/alm_hardware", params={"sysparm_query": q, "sysparm_fields": "sys_id",
                                                            "sysparm_limit": 200})
            return len(_result(r) or []) if _ok(r) else -1

        q = f"assigned_to={user['sys_id']}^install_status!=7"
        seen, total = count(q), len(self.admin.query("alm_hardware", q, limit=200))
        c = rep.add("List own devices", "the device list (step 1)")
        if total == 0:
            c.status, c.detail = "skip", "no devices assigned to this user"
        else:
            c.status = "pass" if seen == total else "fail"
            c.detail = f"{max(seen, 0)} of {total} visible" if seen >= 0 else "access denied"
        if c.status == "fail":
            c.hint = "alm_hardware / alm_asset read ACLs hide assets from this user."

        c = rep.add("Find a device by tag", "typed or photographed asset tags and serials")
        c.status = "pass" if count(f"asset_tag={self.asset['asset_tag']}") == 1 else "fail"
        c.detail = f"asset {self.asset['asset_tag']}"

        dept = self.asset.get("department")
        q = f"department={dept}^assigned_toISEMPTY^install_status!=7"
        seen, total = count(q), len(self.admin.query("alm_hardware", q, limit=200))
        c = rep.add("List a department's equipment", "finding equipment by description (\"the MRI\")")
        c.status = "pass" if seen == total and total else "fail"
        c.detail = f"{max(seen, 0)} of {total} visible"

        r = s.get("/api/now/table/cmdb_ci", params={"sysparm_query": f"asset={self.asset['sys_id']}",
                                                   "sysparm_fields": "sys_id"})
        c = rep.add("Read the device record (CI)", "linking tickets to equipment; duplicate detection")
        c.status = "pass" if _ok(r) and _result(r) else "fail"
        if c.status == "fail":
            c.hint = "cmdb_ci read: needs cmdb_read / itil (or the CI ACLs of your instance)."

    def _tickets(self, s: httpx.Client, user: dict, rep: Report) -> None:
        admin_id = self.admin.get("/api/now/ui/user/current_user").get("user_sys_id", "")
        fields = {
            "caller_id": user["sys_id"], "category": TICKET_CATEGORY, "contact_type": "self-service",
            "impact": "1", "urgency": "2", "short_description": f"{MARK} test ticket",
            "description": f"{MARK} Problem: test\nShip to: 1 Test St", "cmdb_ci": self.asset["ci"],
            "assignment_group": self.asset.get("support_group", ""), "location": self.asset.get("location", ""),
            "watch_list": admin_id, "correlation_id": f"sn_doctor:{user['sys_id']}",
        }
        r = s.post("/api/now/table/incident", json=fields)
        created = _result(r) if _ok(r) else None
        c = rep.add("Create a ticket", "filing a request")
        if not created:
            c.status, c.detail = "fail", f"HTTP {r.status_code}: {r.text[:120]}"
            c.hint = "incident create ACL."
            return
        inc = created["sys_id"]
        self.cleanup.append(("incident", inc))
        stored = self.admin.get(f"/api/now/table/incident/{inc}", sysparm_fields=",".join(fields),
                                sysparm_exclude_reference_link="true")
        why = {
            "description": "incident.description write: sn_incident_write (the agent then puts details in a note)",
            "urgency": "incident.urgency write: sn_incident_write (priority falls back to the default)",
            "impact": "incident.impact write: sn_incident_write",
            "assignment_group": "routing to the support group: the agent notes it for the desk",
            "watch_list": "incident.watch_list write: sn_incident_write",
            "location": "task.location write", "cmdb_ci": "incident.cmdb_ci write", "caller_id": "incident.caller_id write",
        }
        dropped = [k for k, v in fields.items() if v and _val(stored.get(k)) != v]
        c.status = "pass" if not dropped else "partial"
        c.detail = "every field saved" if not dropped else "dropped: " + ", ".join(dropped)
        c.hint = "; ".join(why[k] for k in dropped if k in why)

        r = s.patch(f"/api/now/table/incident/{inc}", json={"comments": f"{MARK} note"})
        c = rep.add("Add a note", "notes, and requests the user can't make directly")
        c.status = "pass" if _ok(r) else "fail"

        r = s.get("/api/now/table/sys_journal_field", params={
            "sysparm_query": f"element_id={inc}^element=comments", "sysparm_fields": "value"})
        via_table = _ok(r) and bool(_result(r))
        r2 = s.get(f"/api/now/table/incident/{inc}", params={"sysparm_fields": "comments",
                                                             "sysparm_display_value": "true"})
        via_field = MARK in str((_result(r2) or {}).get("comments", ""))
        c = rep.add("Read notes", "\"show me the notes\"")
        c.status = "pass" if via_table or via_field else "fail"
        c.detail = "journal table" if via_table else ("ticket's comments field (fallback)" if via_field else "hidden")

        r = s.post("/api/now/attachment/file", content=PNG, headers={"Content-Type": "image/png"},
                   params={"table_name": "incident", "table_sys_id": inc, "file_name": "sn_doctor.png"})
        c = rep.add("Attach a photo", "copying the user's photos onto the ticket")
        c.status = "pass" if _ok(r) else "fail"
        c.detail = "" if _ok(r) else f"HTTP {r.status_code}"

        for name, feature, patch, key in (
            ("Change status (reopen)", "\"move it back to in progress\"", {"state": "2"}, "state"),
            ("Change urgency", "\"make it urgent\"", {"urgency": "1"}, "urgency"),
            ("Change ship-to", "\"ship it to my house\"", {"description": f"{MARK} Problem: test\nShip to: 2 New Ave"},
             "description"),
        ):
            s.patch(f"/api/now/table/incident/{inc}", json=patch)
            got = self.admin.get(f"/api/now/table/incident/{inc}", sysparm_fields=key)
            c = rep.add(name, feature)
            c.status = "pass" if _val(got.get(key)) == patch[key] else "fail"
            if c.status == "fail":
                c.detail = "not saved"
                c.hint = "The agent adds a note asking the service desk to make this change."

        other = self.admin.create("incident", {
            "caller_id": admin_id, "category": TICKET_CATEGORY, "short_description": f"{MARK} someone else's ticket",
            "cmdb_ci": self.asset["ci"]})
        self.cleanup.append(("incident", other["sys_id"]))
        r = s.get("/api/now/table/incident", params={
            "sysparm_query": f"cmdb_ci={self.asset['ci']}^category={TICKET_CATEGORY}^stateIN1,2,3^sys_id={other['sys_id']}",
            "sysparm_fields": "sys_id"})
        c = rep.add("See someone else's open ticket", "\"this is already reported\" on shared equipment")
        c.status = "pass" if _ok(r) and _result(r) else "fail"
        if c.status == "fail":
            c.hint = "incident read ACL (itil or sn_incident_read). Without it every reporter files a new ticket."

        r = s.patch(f"/api/now/table/incident/{other['sys_id']}", json={"watch_list": f"{admin_id},{user['sys_id']}",
                                                                       "comments": f"{MARK} also affected"})
        got = self.admin.get(f"/api/now/table/incident/{other['sys_id']}", sysparm_fields="watch_list",
                             sysparm_exclude_reference_link="true")
        c = rep.add("Follow someone else's ticket", "joining an open ticket on shared equipment")
        c.status = "pass" if user["sys_id"] in _val(got.get("watch_list")) else "fail"
        if c.status == "fail":
            c.hint = "incident write on others' tickets / watch_list. The user can still report separately."

        self._limits(s, user, rep, admin_id, other["sys_id"])

        r = s.patch(f"/api/now/table/incident/{inc}", json={
            "state": "8", "close_code": "Solved Remotely (Permanently)", "close_notes": f"{MARK} cancel"})
        got = self.admin.get(f"/api/now/table/incident/{inc}", sysparm_fields="state")
        c = rep.add("Cancel own ticket", "\"cancel it, I found a spare\"")
        c.status = "pass" if _val(got.get("state")) == "8" else "fail"
        if c.status == "fail":
            c.hint = "The agent adds a note asking the service desk to cancel."


# --- temporary users for --matrix ----------------------------------------------------------


def temp_user(admin: Admin, doctor: Doctor, label: str, roles: list[str]) -> dict:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    loc = next(iter(admin.query("cmn_location", "streetISNOTEMPTY", "sys_id", 1)), {}).get("sys_id", "")
    user = admin.create("sys_user", {
        "user_name": f"sn.doctor.{suffix}", "first_name": "SN Doctor", "last_name": label[:40],
        "email": f"sn.doctor.{suffix}@example.invalid", "department": doctor.asset.get("department", ""),
        "location": loc, "active": "true"})
    try:
        _equip_temp_user(admin, doctor, user, roles, suffix)
    except BaseException:
        drop_user(admin, user)  # never leave a user with roles behind
        raise
    return user


def _equip_temp_user(admin: Admin, doctor: Doctor, user: dict, roles: list[str], suffix: str) -> None:
    for role in roles:
        rid = next(iter(admin.query("sys_user_role", f"name={role}")), {}).get("sys_id")
        if rid:
            admin.create("sys_user_has_role", {"user": user["sys_id"], "role": rid})
    # One device and one group, so "list own devices" and "read own groups" are measured.
    model = next(iter(admin.query("alm_hardware", f"sys_id={doctor.asset['sys_id']}", "model")), {}).get("model", "")
    admin.create("alm_hardware", {"model": model, "asset_tag": f"SNDOC-{suffix}", "assigned_to": user["sys_id"],
                                  "install_status": "1", "comments": MARK})
    if doctor.asset.get("support_group"):
        admin.create("sys_user_grmember", {"user": user["sys_id"], "group": doctor.asset["support_group"]})


def drop_user(admin: Admin, user: dict) -> None:
    for table in ("sys_user_grmember", "sys_user_has_role"):
        for row in admin.query(table, f"user={user['sys_id']}"):
            admin.delete(table, row["sys_id"])
    for row in admin.query("alm_hardware", f"assigned_to={user['sys_id']}"):
        admin.delete("alm_hardware", row["sys_id"])
    admin.delete("sys_user", user["sys_id"])


# --- output ---------------------------------------------------------------------------------

ICON = {"pass": "PASS", "partial": "PART", "fail": "FAIL", "skip": "skip"}


def print_report(rep: Report) -> None:
    print(f"\n{rep.user}  (roles: {', '.join(rep.roles) or 'none'})")
    for c in rep.checks:
        print(f"  {ICON[c.status]:4}  {c.name:32} {c.detail}")
        if c.hint and c.status != "pass":
            print(f"        -> {c.hint}")


def matrix_markdown(reports: list[tuple[str, Report]], instance: str) -> str:
    names = [c.name for c in reports[0][1].checks]
    features = {c.name: c.feature for c in reports[0][1].checks}
    head = "| What the agent does | Used for | " + " | ".join(p for p, _ in reports) + " |"
    lines = [f"<!-- Generated by scripts/sn_doctor.py --matrix against {instance}. Do not edit by hand. -->",
             head, "|" + "---|" * (len(reports) + 2)]
    for n in names:
        cells = []
        for _, rep in reports:
            c = next(x for x in rep.checks if x.name == n)
            cells.append({"pass": "yes", "partial": f"partly ({c.detail.replace('dropped: ', 'drops ')})",
                          "fail": "no", "skip": "-"}[c.status])
        lines.append(f"| {n} | {features[n]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--as", dest="users", action="append", metavar="USER_NAME", help="user to check (repeatable)")
    ap.add_argument("--matrix", action="store_true", help="measure role sets with temporary users")
    ap.add_argument("--read-only", action="store_true", help="skip checks that create a test ticket")
    ap.add_argument("--asset", metavar="TAG", help="equipment to test with (default: first with a CI)")
    ap.add_argument("--persona", action="append", metavar="ROLES",
                    help="with --matrix: a role set to measure, 'none' or roles joined by '+' (repeatable; "
                         "default: none, itil, itil+sn_incident_write, itil+sn_incident_write+asset)")
    ap.add_argument("--markdown", type=Path, help="with --matrix: write the matrix as a Markdown table")
    ap.add_argument("--json", type=Path, help="write all results as JSON")
    args = ap.parse_args()
    if not args.users and not args.matrix:
        ap.error("give --as USER_NAME or --matrix")

    admin = Admin()
    doctor = Doctor(admin, args.read_only, args.asset)
    print(f"ServiceNow {admin.instance}  |  test equipment {doctor.asset['asset_tag']}  |  ticket category "
          f"{TICKET_CATEGORY!r}" + ("  |  read-only" if args.read_only else ""))
    results: list[tuple[str, Report]] = []
    for name in args.users or []:
        user = next(iter(admin.query("sys_user", f"user_name={name}", "sys_id,user_name")), None)
        if not user:
            sys.exit(f"No user {name!r}")
        roles = sorted({r["role.name"] for r in admin.query("sys_user_has_role", f"user={user['sys_id']}^inherited=false",
                                                             "role.name", 200) if r.get("role.name")})
        rep = doctor.run(user, roles)
        print_report(rep)
        results.append((name, rep))
    personas = PERSONAS
    if args.persona:
        personas = {}
        for spec in args.persona:
            roles = [] if spec.strip().lower() == "none" else [r.strip() for r in spec.split("+") if r.strip()]
            for role in roles:
                if not admin.query("sys_user_role", f"name={role}"):
                    sys.exit(f"Role {role!r} not found on this instance.")
            personas[" + ".join(roles) or "no roles"] = roles
    if args.matrix:
        for label, roles in personas.items():
            user = temp_user(admin, doctor, label, roles)
            try:
                rep = doctor.run(user, roles)
            finally:
                drop_user(admin, user)
            rep.user = label
            print_report(rep)
            results.append((label, rep))
        if args.markdown:
            args.markdown.write_text(matrix_markdown([r for r in results if r[0] in personas], admin.instance))
            print(f"\nMatrix written to {args.markdown}")
    if args.json:
        args.json.write_text(json.dumps([{"user": n, "roles": r.roles, "checks": [c.__dict__ for c in r.checks]}
                                         for n, r in results], indent=1))
    failed = sum(c.status == "fail" for _, r in results for c in r.checks)
    print(f"\n{failed} check(s) failed." if failed else "\nAll checks passed.")


if __name__ == "__main__":
    main()
