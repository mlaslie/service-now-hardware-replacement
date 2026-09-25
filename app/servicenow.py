"""ServiceNow REST client that acts as the signed-in user.

Gemini Enterprise signs each person in to ServiceNow (OAuth authorization
code, through the agent's authorization resource) and forwards that user's
access token in the `Authorization` header. Every call here uses it, so
ServiceNow's own access controls apply: people see and change only what their
ServiceNow account allows. The token is held for the length of one request in
a ContextVar and never written to session state, memory or logs.

Hardware tickets are Incidents with category `hardware`. Every read or write of
a ticket is additionally filtered to `caller_id = <signed-in user>`, so asking
for someone else's ticket number returns "not found".
"""

import contextvars
import hashlib
import logging
import re
import time

import httpx

from app import config

logger = logging.getLogger(__name__)

# The signed-in user's ServiceNow access token, for the current request only.
user_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("sn_user_token", default=None)

HARDWARE = "hardware"
ACTIVE_STATES = ("1", "2", "3")  # New, In Progress, On Hold
STATE_LABELS = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
STATE_CODES = {label.lower(): code for code, label in STATE_LABELS.items()}
PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}
# urgency -> (impact, urgency). ServiceNow derives priority from the pair.
URGENCY_TO_IMPACT_URGENCY = {"critical": ("1", "1"), "high": ("1", "2"), "normal": ("2", "2"), "low": ("2", "3")}

_ASSET_FIELDS = ",".join([
    "sys_id", "asset_tag", "serial_number", "display_name", "model.display_name", "model.manufacturer.name",
    "model_category.name", "purchase_date", "warranty_expiration", "install_status", "assigned_to", "ci",
])
_INCIDENT_FIELDS = ",".join([
    "sys_id", "number", "short_description", "description", "state", "priority", "urgency", "impact",
    "category", "subcategory", "sys_created_on", "sys_updated_on", "assignment_group.name",
    "assigned_to.name", "cmdb_ci.name",
])


class NotSignedIn(Exception):
    """No ServiceNow token for this request, or ServiceNow rejected it."""


class ServiceNowError(Exception):
    pass


class Hibernating(ServiceNowError):
    """Developer instances hibernate when idle and answer with an HTML page."""


def _base() -> str:
    return config.SN_INSTANCE_URL.rstrip("/")


async def _request(method: str, path: str, *, token: str | None = None, params: dict | None = None,
                   json: dict | None = None, content: bytes | None = None, headers: dict | None = None) -> dict:
    token = token or user_token.get()
    if not token:
        raise NotSignedIn()
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json", **(headers or {})}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.request(method, _base() + path, params=params, json=json, content=content, headers=hdrs)
    if r.status_code == 401:
        raise NotSignedIn()
    if "json" not in r.headers.get("content-type", ""):
        raise Hibernating("ServiceNow returned a non-JSON page; the instance may be hibernating.")
    if r.status_code >= 400:
        detail = (r.json().get("error") or {}).get("message", r.text[:200])
        raise ServiceNowError(f"{method} {path.split('?')[0]} -> {r.status_code}: {detail}")
    return r.json()


def _value(field) -> str:
    """Reference fields come back as {"link": ..., "value": ...} unless displayed."""
    if isinstance(field, dict):
        return field.get("value", "")
    return field or ""


# --- The signed-in user --------------------------------------------------------

_user_cache: dict[str, tuple[float, dict]] = {}


async def current_user(token: str) -> dict:
    """Who this token belongs to, with the directory details the agent uses.

    Cached per token for a few minutes: this runs on every turn.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    cached = _user_cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    me = (await _request("GET", "/api/now/ui/user/current_user", token=token)).get("result") or {}
    sys_id = me.get("user_sys_id")
    if not sys_id:
        raise NotSignedIn()
    fields = ("sys_id,user_name,name,first_name,email,phone,mobile_phone,title,"
              "department.name,cost_center.name,cost_center.code,manager.name,manager.email,"
              "location.name,location.street,location.city,location.state,location.zip,location.country")
    rec = (await _request("GET", f"/api/now/table/sys_user/{sys_id}", token=token,
                          params={"sysparm_fields": fields})).get("result") or {}
    address = ", ".join(filter(None, [rec.get("location.street"), rec.get("location.city"),
                                      " ".join(filter(None, [rec.get("location.state"), rec.get("location.zip")]))]))
    user = {
        "sys_id": sys_id,
        "user_name": rec.get("user_name") or me.get("user_name", ""),
        "name": rec.get("name") or me.get("user_display_name", ""),
        "first_name": rec.get("first_name", ""),
        "email": (rec.get("email") or "").lower(),
        "phone": rec.get("mobile_phone") or rec.get("phone") or "",
        "title": rec.get("title", ""),
        "department": rec.get("department.name", ""),
        "cost_center": " ".join(filter(None, [rec.get("cost_center.code"), rec.get("cost_center.name")])),
        "manager": rec.get("manager.name", ""),
        "location": rec.get("location.name", ""),
        "location_address": address,
    }
    _user_cache[key] = (time.monotonic() + 300, user)
    return user


# --- Assets ----------------------------------------------------------------------


_LAPTOP_NAMES = ("macbook", "thinkpad", "latitude", "elitebook", "probook", "xps", "surface laptop",
                 "zenbook", "spectre", "envy", "chromebook", "notebook", "laptop")


def _device_type(category: str, name: str) -> str:
    """ServiceNow's demo data files laptops under the generic "Computer"
    category, so the model name decides laptop vs desktop."""
    category, name = category.lower(), name.lower()
    if any(w in category for w in ("laptop", "notebook")) or any(w in name for w in _LAPTOP_NAMES):
        return "laptop"
    for kind, words in (("monitor", ("monitor", "display")), ("phone", ("phone", "mobile", "iphone", "pixel")),
                        ("tablet", ("tablet", "ipad")), ("desktop", ("desktop", "computer", "pc", "workstation"))):
        if any(w in category for w in words) or any(w in name for w in words):
            return kind
    return "other"


def _asset(rec: dict) -> dict:
    name = rec.get("model.display_name") or rec.get("display_name", "")
    kind = _device_type(rec.get("model_category.name") or "", name)
    return {
        "sys_id": rec.get("sys_id", ""),
        "asset_tag": rec.get("asset_tag", ""),
        "serial_number": rec.get("serial_number", ""),
        "manufacturer": rec.get("model.manufacturer.name", ""),
        "model": rec.get("model.display_name") or rec.get("display_name", ""),
        "device_type": kind,
        "purchase_date": rec.get("purchase_date", ""),
        "warranty_end": rec.get("warranty_expiration", ""),
        "assigned_to": _value(rec.get("assigned_to")),
        "ci": _value(rec.get("ci")),
    }


async def my_assets(user_sys_id: str, limit: int = 20) -> list[dict]:
    q = f"assigned_to={user_sys_id}^install_status!=7^ORDERBYmodel_category"  # 7 = retired
    result = await _request("GET", "/api/now/table/alm_hardware",
                            params={"sysparm_query": q, "sysparm_fields": _ASSET_FIELDS, "sysparm_limit": limit})
    return [_asset(r) for r in result.get("result", [])]


async def find_asset(asset_tag: str = "", serial_number: str = "") -> dict | None:
    """By asset tag, then serial number. Returns None if the user can't see it."""
    clauses = []
    tag = "".join(ch for ch in (asset_tag or "").upper() if ch.isalnum() or ch == "-")
    if tag:
        clauses.append(f"asset_tag={tag}")
    serial = (serial_number or "").strip().upper().replace(" ", "")
    if serial:
        clauses.append(f"serial_number={serial}")
    for clause in clauses:
        result = await _request("GET", "/api/now/table/alm_hardware",
                                params={"sysparm_query": clause, "sysparm_fields": _ASSET_FIELDS, "sysparm_limit": 1})
        if result.get("result"):
            return _asset(result["result"][0])
    return None


# --- Incidents -----------------------------------------------------------------------


def _incident(rec: dict) -> dict:
    return {
        "sys_id": rec.get("sys_id", ""),
        "number": rec.get("number", ""),
        "short_description": rec.get("short_description", ""),
        "description": rec.get("description", ""),
        "state": STATE_LABELS.get(str(rec.get("state")), str(rec.get("state"))),
        "state_code": str(rec.get("state", "")),
        "priority": str(rec.get("priority", "")),
        "urgency": str(rec.get("urgency", "")),
        "impact": str(rec.get("impact", "")),
        "opened": rec.get("sys_created_on", ""),
        "updated": rec.get("sys_updated_on", ""),
        "assignment_group": rec.get("assignment_group.name", ""),
        "assigned_to": rec.get("assigned_to.name", ""),
        "device": rec.get("cmdb_ci.name", ""),
        "url": f"{_base()}/nav_to.do?uri=incident.do?sys_id={rec.get('sys_id', '')}",
    }


def _mine(user_sys_id: str) -> str:
    """The filter every ticket read and write goes through."""
    return f"caller_id={user_sys_id}^category={HARDWARE}"


async def create_incident(fields: dict) -> dict:
    result = await _request("POST", "/api/now/table/incident", json=fields,
                            params={"sysparm_fields": _INCIDENT_FIELDS, "sysparm_display_value": "false"})
    return _incident(result["result"])


async def find_open_by_correlation(user_sys_id: str, correlation_id: str) -> dict | None:
    """A ticket already filed from this conversation, so a retry never duplicates it."""
    q = f"{_mine(user_sys_id)}^correlation_id={correlation_id}"
    result = await _request("GET", "/api/now/table/incident",
                            params={"sysparm_query": q, "sysparm_fields": _INCIDENT_FIELDS, "sysparm_limit": 1})
    rows = result.get("result", [])
    return _incident(rows[0]) if rows else None


async def my_incidents(user_sys_id: str, active_only: bool = True, limit: int = 10) -> list[dict]:
    q = _mine(user_sys_id)
    if active_only:
        q += "^stateIN" + ",".join(ACTIVE_STATES)
    q += "^ORDERBYDESCsys_created_on"
    result = await _request("GET", "/api/now/table/incident",
                            params={"sysparm_query": q, "sysparm_fields": _INCIDENT_FIELDS, "sysparm_limit": limit,
                                    "sysparm_display_value": "false"})
    return [_incident(r) for r in result.get("result", [])]


async def my_incident(user_sys_id: str, number: str) -> dict | None:
    number = (number or "").strip().upper()
    if not number:
        return None
    q = f"{_mine(user_sys_id)}^number={number}"
    result = await _request("GET", "/api/now/table/incident",
                            params={"sysparm_query": q, "sysparm_fields": _INCIDENT_FIELDS, "sysparm_limit": 1,
                                    "sysparm_display_value": "false"})
    rows = result.get("result", [])
    return _incident(rows[0]) if rows else None


async def update_incident(user_sys_id: str, number: str, fields: dict) -> dict | None:
    """Updates one of the caller's own hardware tickets; None if it isn't theirs."""
    ticket = await my_incident(user_sys_id, number)
    if not ticket:
        return None
    result = await _request("PATCH", f"/api/now/table/incident/{ticket['sys_id']}", json=fields,
                            params={"sysparm_fields": _INCIDENT_FIELDS, "sysparm_display_value": "false"})
    return _incident(result["result"])


# "2026-09-25 17:55:00 - John Doe (Additional comments)". Tolerates \r\n line
# endings, 12-hour times and other date formats a user's profile may impose.
_JOURNAL_HEADER = re.compile(
    r"^(?P<when>\d[\d/.\-]+ \d{1,2}:\d{2}(?::\d{2})?(?: ?[AaPp][Mm])?) - (?P<who>.+?) "
    r"\((?P<kind>(?:Additional )?[Cc]omments|Work [Nn]otes)\)\s*$", re.M)


def parse_journal(text: str) -> list[dict]:
    """Splits a journal field's display value into entries.

    Falls back to one entry with the raw text rather than reporting "no notes"
    when ServiceNow's format isn't recognized.
    """
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    matches = list(_JOURNAL_HEADER.finditer(text))
    if not matches:
        return [{"when": "", "who": "", "kind": "note", "text": text}]
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append({"when": m.group("when"), "who": m.group("who"),
                        "kind": "work note" if m.group("kind").lower().startswith("work") else "note",
                        "text": text[m.end():end].strip()})
    return entries


async def notes(incident_sys_id: str) -> list[dict]:
    """Notes on a ticket, newest first: comments, plus work notes if the caller's role allows.

    Reads the journal table (one row per note). If the caller can't read it,
    falls back to the incident's journal fields rendered as text.
    """
    entries: list[dict] = []
    try:
        result = await _request("GET", "/api/now/table/sys_journal_field", params={
            "sysparm_query": f"element_id={incident_sys_id}^elementINcomments,work_notes^ORDERBYDESCsys_created_on",
            "sysparm_fields": "element,value,sys_created_on,sys_created_by", "sysparm_limit": 100})
        for row in result.get("result", []):
            entries.append({"when": row.get("sys_created_on", ""), "who": row.get("sys_created_by", ""),
                            "kind": "note" if row.get("element") == "comments" else "work note",
                            "text": (row.get("value") or "").strip()})
    except ServiceNowError as exc:
        logger.info("journal table not readable for this user (%s); using journal fields", exc)
    if not entries:
        result = await _request("GET", f"/api/now/table/incident/{incident_sys_id}",
                                params={"sysparm_fields": "comments,work_notes", "sysparm_display_value": "true"})
        rec = result.get("result") or {}
        entries = parse_journal(rec.get("comments", "")) + parse_journal(rec.get("work_notes", ""))
        logger.info("notes from journal fields: %d (comments %d chars, work_notes %d chars)",
                    len(entries), len(rec.get("comments") or ""), len(rec.get("work_notes") or ""))
    else:
        logger.info("notes from journal table: %d", len(entries))
    return sorted(entries, key=lambda e: e["when"], reverse=True)


async def attach(table_sys_id: str, filename: str, data: bytes, mime_type: str) -> None:
    await _request("POST", "/api/now/attachment/file", content=data, headers={"Content-Type": mime_type},
                   params={"table_name": "incident", "table_sys_id": table_sys_id, "file_name": filename})
