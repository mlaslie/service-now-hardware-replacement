"""Seed and reset ServiceNow demo data for the Hardware Replacement agent.

    uv run --group seed python seed/sn_seed.py login
    uv run --group seed python seed/sn_seed.py set seed/users.json [--equipment seed/equipment.json]
    uv run --group seed python seed/sn_seed.py report [--out seed/state/office-assets.pdf]
    uv run --group seed python seed/sn_seed.py reset [--user USER_NAME ...] [--yes] [--memory]
    uv run --group seed python seed/sn_seed.py clear-tickets [--yes]   # between demo runs

Credentials
-----------
The instance accepts neither basic auth nor client-credentials tokens for API
calls, so this uses the same OAuth authorization-code flow Gemini Enterprise
uses: `login` opens a browser, you sign in to ServiceNow as an admin, and the
token (plus refresh token) is cached in ~/.config/hw-seed/token.json (0600).

The OAuth client (ServiceNow: Application Registry > OAuth - Authorization code
grant, redirect URL http://localhost:8765/callback) comes from Secret Manager
secret `servicenow-seed-oauth` as "instance|client_id|client_secret", or from
SN_INSTANCE_URL / SN_CLIENT_ID / SN_CLIENT_SECRET.

What `set` creates
------------------
For each user in users.json: the user (or updates to an existing one), roles,
group memberships and the office kit in catalog.json. Then the hospital's
shared and clinical equipment in equipment.json: departments, room-level
locations, support groups, model categories and one asset per item, owned by a
department rather than a person.

What `set` records
------------------
Everything created or changed is written to seed/state/manifest.json, and every
asset created carries SEED_MARK in its comments, so `reset` can undo exactly
what was done: created records are deleted, pre-existing users are restored to
their original values, and every incident the seeded users are the caller on or
opened is deleted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import secrets
import string
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
MANIFEST = STATE_DIR / "manifest.json"
TOKEN_FILE = Path.home() / ".config" / "hw-seed" / "token.json"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SEED_MARK = "[hw-seed]"
SECRET_NAME = "servicenow-seed-oauth"
sys.path.insert(0, str(HERE.parent))
from app.config import load_env_file  # noqa: E402 - the same .env the agent uses

load_env_file()
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

# sys_user fields `set` may change; their prior values are snapshotted for reset.
USER_FIELDS = ("first_name", "last_name", "email", "title", "phone", "department", "cost_center",
               "manager", "location", "active")


def log(msg: str) -> None:
    print(msg, flush=True)


# --- Credentials ------------------------------------------------------------------


@dataclass
class OAuthClient:
    instance: str
    client_id: str
    client_secret: str


def load_client() -> OAuthClient:
    if os.environ.get("SN_CLIENT_ID"):
        return OAuthClient(os.environ["SN_INSTANCE_URL"].rstrip("/"),
                           os.environ["SN_CLIENT_ID"].strip(), os.environ["SN_CLIENT_SECRET"].strip())
    if not PROJECT:
        sys.exit("Set GOOGLE_CLOUD_PROJECT in .env (for the servicenow-seed-oauth secret), or set "
                 "SN_INSTANCE_URL, SN_CLIENT_ID and SN_CLIENT_SECRET.")
    from google.cloud import secretmanager
    raw = secretmanager.SecretManagerServiceClient().access_secret_version(
        name=f"projects/{PROJECT}/secrets/{SECRET_NAME}/versions/latest").payload.data.decode()
    instance, client_id, client_secret = (p.strip() for p in raw.split("|"))
    return OAuthClient(instance.rstrip("/"), client_id, client_secret)


def _save_token(tok: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 1800)) - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)


def login(client: OAuthClient) -> None:
    """Browser sign-in (authorization code). Sign in as an admin."""
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [""])[0] == state and "code" in q
            result.update(code=q.get("code", [""])[0] if ok else "", error=q.get("error", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Signed in. You can close this tab." if ok else f"Sign-in failed: {html.escape(result['error'] or 'state mismatch')}"
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = f"{client.instance}/oauth_auth.do?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client.client_id, "redirect_uri": REDIRECT_URI, "state": state})
    log(f"Opening the ServiceNow sign-in. Sign in as an admin.\n  {url}")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()
    if not result.get("code"):
        sys.exit(f"Sign-in did not complete: {result.get('error') or 'timed out'}")
    r = httpx.post(f"{client.instance}/oauth_token.do", data={
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
        "client_id": client.client_id, "client_secret": client.client_secret}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    _save_token(r.json())
    who = SN(client).current_user()
    log(f"Signed in as {who.get('user_name')} ({who.get('user_display_name')}). Token cached at {TOKEN_FILE}")


def access_token(client: OAuthClient) -> str:
    if not TOKEN_FILE.exists():
        sys.exit("Not signed in. Run: sn_seed.py login")
    tok = json.loads(TOKEN_FILE.read_text())
    if tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    r = httpx.post(f"{client.instance}/oauth_token.do", data={
        "grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", ""),
        "client_id": client.client_id, "client_secret": client.client_secret}, timeout=30)
    if r.status_code != 200:
        sys.exit("Session expired. Run: sn_seed.py login")
    new = r.json()
    new.setdefault("refresh_token", tok.get("refresh_token"))
    _save_token(new)
    return new["access_token"]


# --- ServiceNow Table API -----------------------------------------------------------


class SN:
    def __init__(self, client: OAuthClient):
        self.client = client
        self.http = httpx.Client(timeout=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {access_token(self.client)}", "Accept": "application/json"}
        r = self.http.request(method, self.client.instance + path, headers=headers, **kw)
        if r.status_code == 401:
            sys.exit("ServiceNow rejected the token. Run: sn_seed.py login")
        if method == "DELETE" and r.status_code in (200, 204):
            return {}
        if "json" not in r.headers.get("content-type", ""):
            sys.exit("ServiceNow returned a non-JSON page; the developer instance may be hibernating. Wake it and retry.")
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path.split('?')[0]} -> {r.status_code}: "
                               f"{(r.json().get('error') or {}).get('message', r.text[:200])}")
        return r.json()

    def current_user(self) -> dict:
        return self._call("GET", "/api/now/ui/user/current_user").get("result") or {}

    def query(self, table: str, q: str, fields: str = "sys_id", limit: int = 1000) -> list[dict]:
        out, offset = [], 0
        while True:
            rows = self._call("GET", f"/api/now/table/{table}", params={
                "sysparm_query": q, "sysparm_fields": fields, "sysparm_limit": min(limit, 500),
                "sysparm_offset": offset, "sysparm_exclude_reference_link": "true"}).get("result", [])
            out += rows
            if len(rows) < 500 or len(out) >= limit:
                return out[:limit]
            offset += len(rows)

    def one(self, table: str, q: str, fields: str = "sys_id") -> dict | None:
        rows = self.query(table, q, fields, limit=1)
        return rows[0] if rows else None

    def get(self, table: str, sys_id: str, fields: str) -> dict:
        return self._call("GET", f"/api/now/table/{table}/{sys_id}", params={
            "sysparm_fields": fields, "sysparm_exclude_reference_link": "true"}).get("result") or {}

    def create(self, table: str, data: dict) -> dict:
        return self._call("POST", f"/api/now/table/{table}", json=data,
                          params={"sysparm_exclude_reference_link": "true"})["result"]

    def update(self, table: str, sys_id: str, data: dict) -> dict:
        return self._call("PATCH", f"/api/now/table/{table}/{sys_id}", json=data,
                          params={"sysparm_exclude_reference_link": "true"})["result"]

    def delete(self, table: str, sys_id: str) -> None:
        self._call("DELETE", f"/api/now/table/{table}/{sys_id}")

    def table_exists(self, table: str) -> bool:
        return self.one("sys_db_object", f"name={table}") is not None


# --- Manifest -----------------------------------------------------------------------------


@dataclass
class Manifest:
    instance: str = ""
    created: dict[str, list[str]] = field(default_factory=dict)  # table -> sys_ids
    user_snapshots: dict[str, dict] = field(default_factory=dict)  # sys_id -> fields before `set`
    users: dict[str, dict] = field(default_factory=dict)  # user_name -> {sys_id, email, created}
    items: dict[str, list[str]] = field(default_factory=dict)  # user_name -> catalog keys issued
    equipment: dict[str, str] = field(default_factory=dict)  # asset tag -> sys_id (shared/clinical equipment)

    @classmethod
    def load(cls) -> "Manifest":
        return cls(**json.loads(MANIFEST.read_text())) if MANIFEST.exists() else cls()

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(self.__dict__, indent=2))

    def track(self, table: str, sys_id: str) -> None:
        self.created.setdefault(table, [])
        if sys_id not in self.created[table]:
            self.created[table].append(sys_id)
        self.save()  # after every write, so an interrupted run can still be reset


# --- set --------------------------------------------------------------------------------------


def _q(value: str) -> str:
    """Escape a value for an encoded query (^ separates conditions)."""
    return value.replace("^", "^^")


class Seeder:
    def __init__(self, sn: SN, manifest: Manifest, catalog: dict):
        self.sn, self.m, self.catalog = sn, manifest, catalog
        self._cache: dict[tuple, str] = {}

    def _find_or_create(self, table: str, q: str, data: dict) -> str:
        key = (table, q)
        if key in self._cache:
            return self._cache[key]
        row = self.sn.one(table, q)
        if row:
            sys_id = row["sys_id"]
        else:
            sys_id = self.sn.create(table, data)["sys_id"]
            self.m.track(table, sys_id)
            log(f"  + {table}: {data.get('name') or data}")
        self._cache[key] = sys_id
        return sys_id

    def company(self, name: str) -> str:
        return self._find_or_create("core_company", f"name={_q(name)}", {"name": name, "manufacturer": "true"})

    def category(self, candidates: list[str]) -> str:
        for name in candidates:
            row = self.sn.one("cmdb_model_category", f"name={_q(name)}")
            if row:
                return row["sys_id"]
        raise RuntimeError(f"No model category found among {candidates}; add one in ServiceNow or edit catalog.json")

    def hw_model(self, item: dict) -> str:
        return self._find_or_create(
            "cmdb_hardware_product_model", f"name={_q(item['model'])}^manufacturer={self.company(item['manufacturer'])}",
            {"name": item["model"], "manufacturer": self.company(item["manufacturer"]),
             "model_number": item["model_number"], "cmdb_model_category": self.category(item["category"])})

    def sw_model(self, item: dict) -> str:
        return self._find_or_create(
            "cmdb_software_product_model", f"name={_q(item['product'])}",
            {"name": item["product"], "manufacturer": self.company(item["publisher"]), "version": item["version"]})

    def location(self, loc: dict) -> str:
        return self._find_or_create("cmn_location", f"name={_q(loc['name'])}", {
            k: loc[k] for k in ("name", "street", "city", "state", "zip", "country") if loc.get(k)})

    def model_category(self, spec: dict) -> str:
        """An existing category, or a new hardware one. ServiceNow allows one category per
        CI class (cmdb_ci_hardware belongs to "Hardware"), so new ones get no CI class and
        `equipment` creates each asset's CI itself."""
        return self._find_or_create("cmdb_model_category", f"name={_q(spec['name'])}", {
            "name": spec["name"], "asset_class": "alm_hardware"})

    def group(self, name: str) -> str:
        return self._find_or_create("sys_user_group", f"name={_q(name)}", {"name": name, "active": "true"})

    def department(self, name: str) -> str:
        return self._find_or_create("cmn_department", f"name={_q(name)}", {"name": name})

    def cost_center(self, name: str) -> str:
        return self._find_or_create("cmn_cost_center", f"name={_q(name)}", {"name": name})

    def user(self, spec: dict) -> dict:
        fields = {k: spec[k] for k in ("first_name", "last_name", "email", "title", "phone") if spec.get(k)}
        if spec.get("location"):
            fields["location"] = self.location(spec["location"])
        if spec.get("department"):
            fields["department"] = self.department(spec["department"])
        if spec.get("cost_center"):
            fields["cost_center"] = self.cost_center(spec["cost_center"])
        if spec.get("manager"):
            mgr = self.sn.one("sys_user", f"user_name={_q(spec['manager'])}")
            if mgr:
                fields["manager"] = mgr["sys_id"]
            else:
                log(f"  ! manager {spec['manager']} not found; skipped")
        fields["active"] = "true"

        existing = self.sn.one("sys_user", f"user_name={_q(spec['user_name'])}", "sys_id," + ",".join(USER_FIELDS))
        if existing:
            sys_id = existing["sys_id"]
            if sys_id not in self.m.user_snapshots and spec["user_name"] not in self.m.users:
                self.m.user_snapshots[sys_id] = {k: existing.get(k, "") for k in USER_FIELDS}
            self.sn.update("sys_user", sys_id, fields)
            created = self.m.users.get(spec["user_name"], {}).get("created", False)
            log(f"  ~ updated user {spec['user_name']}")
        else:
            sys_id = self.sn.create("sys_user", {"user_name": spec["user_name"], **fields})["sys_id"]
            created = True
            self.m.track("sys_user", sys_id)
            log(f"  + created user {spec['user_name']}")
        self.m.users[spec["user_name"]] = {"sys_id": sys_id, "email": spec.get("email", ""), "created": created,
                                           "location": fields.get("location", "")}
        self.m.save()
        for group_name in spec.get("groups", []):
            group_id = self.group(group_name)
            if not self.sn.one("sys_user_grmember", f"user={sys_id}^group={group_id}"):
                member = self.sn.create("sys_user_grmember", {"user": sys_id, "group": group_id})
                self.m.track("sys_user_grmember", member["sys_id"])
                log(f"  + member of {group_name}")
        for role_name in spec.get("roles", []):
            role = self.sn.one("sys_user_role", f"name={_q(role_name)}")
            if not role:
                log(f"  ! role {role_name} not found; skipped")
            elif not self.sn.one("sys_user_has_role", f"user={sys_id}^role={role['sys_id']}"):
                grant = self.sn.create("sys_user_has_role", {"user": sys_id, "role": role["sys_id"]})
                self.m.track("sys_user_has_role", grant["sys_id"])
                log(f"  + role {role_name}")
        return self.m.users[spec["user_name"]]

    def _asset_tag(self) -> str:
        while True:
            tag = f"P{random.randint(1000000, 9999999)}"
            if not self.sn.one("alm_asset", f"asset_tag={tag}"):
                return tag

    @staticmethod
    def _serial(prefix: str, length: int = 12) -> str:
        return prefix + "".join(random.choices(string.ascii_uppercase + string.digits, k=max(4, length - len(prefix))))

    def issue(self, user_name: str, user: dict, keys: list[str] | None, has_licenses: bool) -> None:
        issued = set(self.m.items.get(user_name, []))
        today = dt.date.today()
        for item in self.catalog["hardware"]:
            if (keys and item["key"] not in keys) or item["key"] in issued:
                continue
            bought = today - dt.timedelta(days=item["age_days"])
            data = {
                "model": self.hw_model(item), "model_category": self.category(item["category"]),
                "asset_tag": self._asset_tag(), "serial_number": self._serial(item["serial_prefix"]),
                "assigned_to": user["sys_id"], "install_status": "1", "cost": str(item["cost"]),
                "purchase_date": bought.isoformat(),
                "warranty_expiration": (bought + dt.timedelta(days=365 * item["warranty_years"])).isoformat(),
                "comments": f"{SEED_MARK} {item['key']}",
            }
            if user.get("location"):
                data["location"] = user["location"]
            asset = self.sn.create("alm_hardware", data)
            self.m.track("alm_hardware", asset["sys_id"])
            self.m.items.setdefault(user_name, []).append(item["key"])
            self.m.save()
            log(f"    + {item['manufacturer']} {item['model']}  tag {data['asset_tag']}  s/n {data['serial_number']}")
        if not has_licenses:
            return
        for item in self.catalog["software"]:
            if (keys and item["key"] not in keys) or item["key"] in issued:
                continue
            start = today - dt.timedelta(days=90)
            data = {
                "model": self.sw_model(item), "asset_tag": self._asset_tag(),
                "serial_number": "-".join(self._serial("", 5) for _ in range(4)),  # license key
                "assigned_to": user["sys_id"], "install_status": "1", "cost": str(item["cost"]),
                "purchase_date": start.isoformat(),
                "end_date": (start + dt.timedelta(days=365 * item["term_years"])).isoformat(),
                "rights": "1", "comments": f"{SEED_MARK} {item['key']}",
            }
            lic = self.sn.create("alm_license", data)
            self.m.track("alm_license", lic["sys_id"])
            self.m.items.setdefault(user_name, []).append(item["key"])
            self.m.save()
            log(f"    + {item['publisher']} {item['product']}  tag {data['asset_tag']}")


    def equipment(self, spec: dict) -> None:
        """Hospital equipment: departments, rooms, support groups and one asset per item."""
        site = spec["site"]
        categories = {c["name"]: self.model_category(c) for c in spec.get("categories", [])}
        today = dt.date.today()
        for item in spec["equipment"]:
            if item["asset_tag"] in self.m.equipment:
                continue
            existing = self.sn.one("alm_hardware", f"asset_tag={_q(item['asset_tag'])}", "sys_id,comments")
            if existing:
                if SEED_MARK in (existing.get("comments") or ""):  # ours, from an earlier run
                    self.m.equipment[item["asset_tag"]] = existing["sys_id"]
                    self.m.save()
                else:
                    log(f"  = {item['asset_tag']} already exists and isn't seed data; left as is")
                continue
            category = categories.get(item["category"]) or self.category([item["category"]])
            model = self._find_or_create(
                "cmdb_hardware_product_model",
                f"name={_q(item['model'])}^manufacturer={self.company(item['manufacturer'])}",
                {"name": item["model"], "manufacturer": self.company(item["manufacturer"]),
                 "model_number": item["model_number"], "cmdb_model_category": category})
            room = self.location({**site, "name": f"{site['name']} - {item['room']}"})
            bought = today - dt.timedelta(days=item["age_days"])
            data = {
                "model": model, "model_category": category, "asset_tag": item["asset_tag"],
                "serial_number": item["serial"], "assigned_to": "", "install_status": "1",
                "department": self.department(item["department"]), "cost_center": self.cost_center(item["department"]),
                "location": room, "support_group": self.group(item["support_group"]), "cost": str(item["cost"]),
                "purchase_date": bought.isoformat(),
                "warranty_expiration": (bought + dt.timedelta(days=365 * item["warranty_years"])).isoformat(),
                "comments": f"{SEED_MARK} equipment",
            }
            asset = self.sn.create("alm_hardware", data)
            self.m.track("alm_hardware", asset["sys_id"])
            if not asset.get("ci"):
                # Tickets link to the CI, and the agent spots duplicate reports through it.
                ci = self.sn.create("cmdb_ci_hardware", {
                    "name": f"{item['model']} ({item['asset_tag']})", "asset_tag": item["asset_tag"],
                    "serial_number": item["serial"], "model_id": model, "manufacturer": self.company(item["manufacturer"]),
                    "department": data["department"], "location": room, "support_group": data["support_group"],
                    "asset": asset["sys_id"]})
                self.m.track("cmdb_ci", ci["sys_id"])
                self.sn.update("alm_hardware", asset["sys_id"], {"ci": ci["sys_id"]})
            self.m.equipment[item["asset_tag"]] = asset["sys_id"]
            self.m.save()
            log(f"  + {item['asset_tag']}  {item['manufacturer']} {item['model']}  ({item['department']}, {item['room']})")


def cmd_set(sn: SN, users_file: Path, catalog_file: Path, equipment_file: Path | None = None) -> None:
    users = json.loads(users_file.read_text())
    catalog = json.loads(catalog_file.read_text())
    m = Manifest.load()
    if m.instance and m.instance != sn.client.instance:
        sys.exit(f"Manifest belongs to {m.instance}; reset it first or move {MANIFEST} aside.")
    m.instance = sn.client.instance
    has_licenses = sn.table_exists("alm_license")
    if not has_licenses:
        log("! alm_license (software assets) is not available on this instance; software items are skipped.")
    seeder = Seeder(sn, m, catalog)
    for spec in users:
        log(f"{spec['user_name']}:")
        user = seeder.user(spec)
        seeder.issue(spec["user_name"], user, spec.get("items"), has_licenses)
    equipment_file = equipment_file or HERE / "equipment.json"
    if equipment_file.exists():
        log("hospital equipment:")
        seeder.equipment(json.loads(equipment_file.read_text()))
    m.save()
    log(f"Done. {len(users)} users, {len(m.equipment)} pieces of equipment. Manifest: {MANIFEST}")


# --- report -------------------------------------------------------------------------------------


ITEM_FIELDS = ("sys_class_name,asset_tag,serial_number,display_name,model.display_name,model.model_number,"
               "model.manufacturer.name,model_category.name,install_status,purchase_date,warranty_expiration,"
               "end_date,cost,comments")
EQUIPMENT_FIELDS = ("asset_tag,serial_number,model.display_name,model.manufacturer.name,department.name,"
                    "location.name,support_group.name,warranty_expiration")
STATUS = {"1": "In use", "2": "On order", "3": "In maintenance", "6": "In stock", "7": "Retired", "8": "Missing"}


def collect(sn: SN, m: Manifest) -> list[dict]:
    """Current ServiceNow data for every seeded user (so the report reflects reality)."""
    people = []
    for user_name, u in m.users.items():
        rec = sn.get("sys_user", u["sys_id"], "user_name,name,email,title,phone,department.name,cost_center.name,"
                     "manager.name,location.name,location.street,location.city,location.state,location.zip")
        if not rec:
            continue
        items = sn.query("alm_asset", f"assigned_to={u['sys_id']}^ORDERBYsys_class_name", ITEM_FIELDS)
        people.append({"user": rec, "items": items})
    return people


def cmd_report(sn: SN, out: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    m = Manifest.load()
    if not m.users and not m.equipment:
        sys.exit("Nothing seeded yet. Run: sn_seed.py set users.json")
    people = collect(sn, m)

    green, ink, muted, band = (colors.HexColor(c) for c in ("#22873B", "#25282A", "#56595A", "#F1F5F2"))
    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Title"], textColor=green, fontSize=22, alignment=0)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=ink, fontSize=16, spaceAfter=4)
    small = ParagraphStyle("s", parent=styles["Normal"], textColor=muted, fontSize=8.5, leading=11)
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=8, leading=10, textColor=ink)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(muted)
        canvas.drawString(0.5 * inch, 0.35 * inch, f"Office asset register  |  {m.instance}")
        canvas.drawRightString(10.5 * inch, 0.35 * inch, f"Page {doc.page}")
        canvas.setStrokeColor(green)
        canvas.setLineWidth(2)
        canvas.line(0.5 * inch, 7.95 * inch, 10.5 * inch, 7.95 * inch)
        canvas.restoreState()

    story = [Paragraph("Office Asset Register", title),
             Paragraph(f"Generated {dt.datetime.now():%B %d, %Y %H:%M}  |  {len(people)} people  |  "
                       f"{sum(len(p['items']) for p in people)} assets  |  {len(m.equipment)} pieces of equipment  |  "
                       f"{html.escape(m.instance)}", small),
             Spacer(1, 12)]
    summary = [["Name", "User ID", "Department", "Location", "Assets", "Total cost"]]
    for p in people:
        u = p["user"]
        cost = sum(float(i.get("cost") or 0) for i in p["items"])
        summary.append([u.get("name", ""), u.get("user_name", ""), u.get("department.name", ""),
                        u.get("location.name", ""), str(len(p["items"])), f"${cost:,.0f}"])
    story.append(_table(summary, [2.1, 1.6, 1.4, 2.5, 0.8, 1.2], green, band, colors, inch))

    for p in people:
        u, items = p["user"], p["items"]
        address = ", ".join(filter(None, [u.get("location.street"), u.get("location.city"),
                                          " ".join(filter(None, [u.get("location.state"), u.get("location.zip")]))]))
        story += [PageBreak(), Paragraph(html.escape(u.get("name", "")), h1),
                  Paragraph(html.escape(" | ".join(filter(None, [u.get("title"), u.get("department.name")]))), small),
                  Spacer(1, 8)]
        info = [["User ID", u.get("user_name", ""), "Email", u.get("email", "")],
                ["Phone", u.get("phone", "") or "-", "Manager", u.get("manager.name", "") or "-"],
                ["Cost center", u.get("cost_center.name", "") or "-", "Location", u.get("location.name", "") or "-"],
                ["Ship-to address", address or "-", "", ""]]
        t = Table(info, colWidths=[1.3 * inch, 3.7 * inch, 1.1 * inch, 3.9 * inch])
        t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9), ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
                               ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 9), ("TEXTCOLOR", (0, 0), (-1, -1), ink),
                               ("SPAN", (1, 3), (3, 3)), ("BACKGROUND", (0, 0), (-1, -1), band),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5)]))
        story += [t, Spacer(1, 12)]
        rows = [["Type", "Manufacturer", "Model", "Model no.", "Asset tag", "Serial / key", "Status", "Purchased",
                 "Warranty / term"]]
        for i in items:
            software = i.get("sys_class_name") == "alm_license"
            rows.append([Paragraph(html.escape("Software" if software else (i.get("model_category.name") or "-")), cell),
                         Paragraph(html.escape(i.get("model.manufacturer.name") or "-"), cell),
                         Paragraph(html.escape(i.get("model.display_name") or i.get("display_name") or "-"), cell),
                         Paragraph(html.escape(i.get("model.model_number") or "-"), cell),
                         i.get("asset_tag") or "-", Paragraph(html.escape(i.get("serial_number") or "-"), cell),
                         STATUS.get(str(i.get("install_status")), str(i.get("install_status") or "-")),
                         (i.get("purchase_date") or "-")[:10],
                         ((i.get("end_date") if software else i.get("warranty_expiration")) or "-")[:10]])
        if len(rows) == 1:
            rows.append(["No assets assigned", "", "", "", "", "", "", "", ""])
        story.append(_table(rows, [0.95, 0.9, 1.85, 1.05, 0.8, 1.75, 0.6, 0.8, 1.0], green, band, colors, inch))

    equipment = [sn.get("alm_hardware", sid, EQUIPMENT_FIELDS) for sid in m.equipment.values()]
    equipment = [e for e in equipment if e]
    if equipment:
        story += [PageBreak(), Paragraph("Shared and clinical equipment", h1),
                  Paragraph("Owned by departments, not people. Anyone can report a problem with it; tickets go to "
                            "the support group.", small), Spacer(1, 8)]
        rows = [["Asset tag", "Equipment", "Serial", "Department", "Location", "Supported by", "Warranty"]]
        for e in sorted(equipment, key=lambda e: (e.get("department.name") or "", e.get("asset_tag") or "")):
            rows.append([e.get("asset_tag") or "-",
                         Paragraph(html.escape(" ".join(filter(None, [e.get("model.manufacturer.name"),
                                                                      e.get("model.display_name")]))), cell),
                         e.get("serial_number") or "-", Paragraph(html.escape(e.get("department.name") or "-"), cell),
                         Paragraph(html.escape(e.get("location.name") or "-"), cell),
                         Paragraph(html.escape(e.get("support_group.name") or "-"), cell),
                         (e.get("warranty_expiration") or "-")[:10]])
        story.append(_table(rows, [0.9, 2.3, 1.2, 1.4, 2.4, 1.2, 0.8], green, band, colors, inch))

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title="Office Asset Register")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    log(f"Report: {out}")


def _table(rows, widths, green, band, colors, inch):
    from reportlab.platypus import Table, TableStyle
    t = Table(rows, colWidths=[w * inch for w in widths], repeatRows=1)
    style = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5), ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
             ("BACKGROUND", (0, 0), (-1, 0), green), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DADEE3")),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    style += [("BACKGROUND", (0, r), (-1, r), band) for r in range(2, len(rows), 2)]
    t.setStyle(TableStyle(style))
    return t


# --- reset ------------------------------------------------------------------------------------


# Delete order: records that reference others first.
# cmdb_ci: the configuration items ServiceNow created for seeded assets.
DELETE_ORDER = ["sys_user_grmember", "sys_user_has_role", "alm_license", "alm_hardware", "cmdb_ci",
                "cmdb_software_product_model", "cmdb_hardware_product_model", "cmdb_model_category", "core_company",
                "sys_user_group", "sys_user", "cmn_location", "cmn_department", "cmn_cost_center"]


# Set on every ticket the agent files (app/tools.py submit_ticket).
AGENT_MARK = "Gemini Enterprise - Hardware Replacement agent"


def demo_tickets(sn: SN, users: dict, extra_query: str = "") -> list[dict]:
    """Tickets safe to delete: every ticket of a user this script created, but only the agent's
    tickets for people who existed before (their other tickets are real work)."""
    ids = [u["sys_id"] for u in users.values()]
    created = {u["sys_id"] for u in users.values() if u.get("created")}
    q = "^OR".join(filter(None, [f"caller_idIN{','.join(ids)}" if ids else "",
                                 f"opened_byIN{','.join(ids)}" if ids else "", extra_query]))
    if not q:
        return []
    rows = sn.query("incident", q, "sys_id,number,short_description,caller_id,opened_by,correlation_display,cmdb_ci")

    def ref(v):
        return v.get("value", "") if isinstance(v, dict) else (v or "")

    def ours(r):
        people = {ref(r.get("caller_id")), ref(r.get("opened_by"))}
        return bool(people & created) or (r.get("correlation_display") or "").startswith(AGENT_MARK) \
            or bool(extra_query and ref(r.get("cmdb_ci")) and ref(r.get("cmdb_ci")) in extra_query)
    return [r for r in rows if ours(r)]


def plan_reset(sn: SN, m: Manifest, only: set[str] | None = None) -> dict:
    """What reset will do. With `only`, just those users: their tickets, their seeded
    assets, and the user (deleted if the script created them, otherwise restored).
    Shared records (models, manufacturers, locations...) are kept in that case."""
    names = [n for n in m.users if not only or n in only]
    users = {n: m.users[n] for n in names}
    user_ids = [u["sys_id"] for u in users.values()]
    plan: dict = {"users": names, "incidents": [], "delete": {},
                  "restore_users": {sid: f for sid, f in m.user_snapshots.items() if sid in user_ids}}
    if user_ids:
        plan["incidents"] = demo_tickets(sn, users)
    for table in ("alm_license", "alm_hardware"):
        if table in m.created or sn.table_exists(table):
            q = f"commentsLIKE{SEED_MARK}" + (f"^assigned_toIN{','.join(user_ids)}" if only else "")
            marked = [r["sys_id"] for r in sn.query(table, q)] if (user_ids or not only) else []
            ids = marked if only else list(dict.fromkeys(m.created.get(table, []) + marked))
            if ids:
                plan["delete"][table] = ids
                if table == "alm_hardware":
                    cis = [sn.get(table, sid, "ci").get("ci") for sid in ids]
                    if not only:
                        cis += m.created.get("cmdb_ci", [])
                    cis = list(dict.fromkeys(c for c in cis if c))
                    if cis:
                        plan["delete"]["cmdb_ci"] = cis
    if only and user_ids and m.created.get("sys_user_grmember"):
        members = [r["sys_id"] for r in sn.query("sys_user_grmember", f"userIN{','.join(user_ids)}")
                   if r["sys_id"] in m.created["sys_user_grmember"]]
        if members:
            plan["delete"]["sys_user_grmember"] = members
    if only and user_ids and m.created.get("sys_user_has_role"):
        grants = [r["sys_id"] for r in sn.query("sys_user_has_role", f"userIN{','.join(user_ids)}")
                  if r["sys_id"] in m.created["sys_user_has_role"]]
        if grants:
            plan["delete"]["sys_user_has_role"] = grants
    if only:
        created_users = [u["sys_id"] for u in users.values() if u.get("created")]
        if created_users:
            plan["delete"]["sys_user"] = created_users
    else:
        for table in DELETE_ORDER:
            if table not in ("alm_license", "alm_hardware", "cmdb_ci") and m.created.get(table):
                plan["delete"][table] = list(m.created[table])
    plan["delete"] = {t: plan["delete"][t] for t in DELETE_ORDER if t in plan["delete"]}
    return plan


def cmd_reset(sn: SN, yes: bool, memory: bool, only: set[str] | None = None) -> None:
    m = Manifest.load()
    if not m.users and not m.created:
        sys.exit("Nothing to reset (no manifest).")
    if only and not only <= set(m.users):
        sys.exit(f"Not seeded by this tool: {', '.join(sorted(only - set(m.users)))}")
    plan = plan_reset(sn, m, only)
    emails = [m.users[n]["email"] for n in plan["users"] if m.users[n].get("email")]
    log(f"Reset plan for {m.instance}" + (f" (users: {', '.join(plan['users'])})" if only else " (everything)") + ":")
    log(f"  incidents to delete ({len(plan['incidents'])}): "
        + ", ".join(i["number"] for i in plan["incidents"][:20]) + (" ..." if len(plan["incidents"]) > 20 else ""))
    for table, ids in plan["delete"].items():
        log(f"  {table}: delete {len(ids)}")
    log(f"  users to restore to their original values: {len(plan['restore_users'])}")
    if memory:
        log(f"  agent memories to delete for: {', '.join(emails)}")
    if not yes:
        log("\nDry run. Re-run with --yes to apply.")
        return

    for inc in plan["incidents"]:
        sn.delete("incident", inc["sys_id"])  # attachments are removed with the record
    log(f"deleted {len(plan['incidents'])} incidents")
    for sys_id, fields in plan["restore_users"].items():
        try:
            sn.update("sys_user", sys_id, fields)
        except RuntimeError as exc:
            log(f"  ! could not restore user {sys_id}: {exc}")
    failed, deleted = [], set()
    for table, ids in plan["delete"].items():
        for sys_id in ids:
            try:
                sn.delete(table, sys_id)
                deleted.add(sys_id)
            except RuntimeError as exc:
                if "404" in str(exc):  # already gone, e.g. a CI removed with its asset
                    deleted.add(sys_id)
                else:  # still referenced by something outside the seed
                    failed.append((table, sys_id, str(exc)))
        log(f"deleted {table}: {len(ids)}")
    if memory:
        _delete_memories(emails)

    if only:
        for name in plan["users"]:
            sid = m.users.pop(name)["sys_id"]
            m.items.pop(name, None)
            m.user_snapshots.pop(sid, None)
        m.created = {t: [i for i in ids if i not in deleted] for t, ids in m.created.items()}
        m.save()
        log(f"Reset complete for {', '.join(plan['users'])}." + (f" {len(failed)} records could not be deleted." if failed else ""))
        return
    if failed:
        log(f"! {len(failed)} records could not be deleted (kept in the manifest):")
        for f in failed[:10]:
            log(f"  {f[0]} {f[1]}: {f[2][:120]}")
        m.created = {}
        for table, sys_id, _ in failed:
            m.created.setdefault(table, []).append(sys_id)
        m.users, m.user_snapshots, m.items, m.equipment = {}, {}, {}, {}
        m.save()
    else:
        MANIFEST.unlink(missing_ok=True)
        log("Reset complete.")


def cmd_clear_tickets(sn: SN, yes: bool) -> None:
    """Deletes the demo tickets (see demo_tickets) and any ticket on seeded equipment, keeping
    users, devices and equipment: a clean slate for the next demo run."""
    m = Manifest.load()
    cis = [c for c in (sn.get("alm_hardware", sid, "ci").get("ci") for sid in m.equipment.values()) if c]
    cis += [r["sys_id"] for r in sn.query("cmdb_ci", f"assetIN{','.join(m.equipment.values())}")] if m.equipment else []
    # Tickets on seeded equipment are demo tickets, whoever filed them.
    incidents = demo_tickets(sn, m.users, f"cmdb_ciIN{','.join(dict.fromkeys(cis))}" if cis else "")
    log(f"tickets to delete ({len(incidents)}): " + ", ".join(i["number"] for i in incidents))
    if not yes:
        log("\nDry run. Re-run with --yes to apply.")
        return
    for inc in incidents:
        sn.delete("incident", inc["sys_id"])
    log(f"deleted {len(incidents)} tickets")


def _delete_memories(emails: list[str]) -> None:
    import vertexai
    engine = os.environ.get("AGENT_ENGINE_ID", "")
    if not engine:
        return log("! AGENT_ENGINE_ID is not set in .env; agent memories were not deleted")
    location = os.environ.get("AGENT_ENGINE_LOCATION") or os.environ.get("REGION", "us-central1")
    client = vertexai.Client(project=PROJECT, location=location)
    name = f"projects/{PROJECT}/locations/{location}/reasoningEngines/{engine}"
    count = 0
    for mem in client.agent_engines.memories.list(name=name):
        if (mem.scope or {}).get("user_id") in emails:
            client.agent_engines.memories.delete(name=mem.name)
            count += 1
    log(f"deleted {count} agent memories")


# --- CLI ------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="sign in to ServiceNow in the browser (as an admin)")
    p = sub.add_parser("set", help="create/update users and issue office items")
    p.add_argument("users", type=Path)
    p.add_argument("--catalog", type=Path, default=HERE / "catalog.json")
    p.add_argument("--equipment", type=Path, default=HERE / "equipment.json",
                   help="shared and clinical equipment to create (default seed/equipment.json)")
    p = sub.add_parser("report", help="write the PDF asset register")
    p.add_argument("--out", type=Path, default=STATE_DIR / "office-assets.pdf")
    p = sub.add_parser("reset", help="undo everything `set` did and delete the users' tickets")
    p.add_argument("--yes", action="store_true", help="apply (default is a dry run)")
    p.add_argument("--memory", action="store_true", help="also delete the users' agent memories")
    p.add_argument("--user", action="append", dest="users", metavar="USER_NAME",
                   help="reset only this seeded user (repeatable); default is everything")
    p = sub.add_parser("clear-tickets", help="delete the demo tickets only (seeded users' and on seeded equipment)")
    p.add_argument("--yes", action="store_true", help="apply (default is a dry run)")
    args = ap.parse_args()

    client = load_client()
    if args.cmd == "login":
        return login(client)
    sn = SN(client)
    if args.cmd == "set":
        cmd_set(sn, args.users, args.catalog, args.equipment)
    elif args.cmd == "report":
        cmd_report(sn, args.out)
    elif args.cmd == "clear-tickets":
        cmd_clear_tickets(sn, args.yes)
    elif args.cmd == "reset":
        cmd_reset(sn, args.yes, args.memory, set(args.users) if args.users else None)


if __name__ == "__main__":
    main()
