"""ServiceNow REST client that acts as the signed-in user.

Gemini Enterprise signs each person in to ServiceNow (OAuth authorization
code, through the agent's authorization resource) and forwards that user's
access token in the `Authorization` header. Every call here uses it, so
ServiceNow's own access controls apply: people see and change only what their
ServiceNow account allows. The token is held for the length of one request in
a ContextVar and never written to session state, memory or logs.

Hardware tickets are Incidents with category `hardware`. Every read or write of
a ticket is additionally filtered to tickets the signed-in user reported
(`caller_id`) or follows (`watch_list`), so asking for anyone else's ticket
number returns "not found".

Assets follow the usual hospital layout: personal devices are assigned to a
person; shared and clinical equipment belongs to a department, sits at a
location and is supported by a group (Clinical Engineering, Imaging
Engineering, the IT Service Desk).
"""

import asyncio
import contextvars
import datetime
import hashlib
import functools
import inspect
import logging
import re
import time

import httpx

from app import config
from app.profile import current as _current_profile

logger = logging.getLogger(__name__)

# The signed-in user's ServiceNow access token, for the current request only.
user_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("sn_user_token", default=None)

PROFILE = _current_profile()
HARDWARE = PROFILE.servicenow.ticket_category  # incident.category of every ticket this agent touches
ACTIVE_STATES = ("1", "2", "3")  # New, In Progress, On Hold
STATE_LABELS = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
STATE_CODES = {label.lower(): code for code, label in STATE_LABELS.items()}
PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}
# urgency -> (impact, urgency). ServiceNow derives priority from the pair.
URGENCY_TO_IMPACT_URGENCY = {k: (v.impact, v.urgency)
                             for k, v in _current_profile().servicenow.urgency_matrix.items()}

_ASSET_FIELDS = ",".join([
    "sys_id", "asset_tag", "serial_number", "display_name", "model.display_name", "model.manufacturer.name",
    "model_category.name", "purchase_date", "warranty_expiration", "install_status", "assigned_to", "ci",
    "assigned_to.name", "department", "department.name", "location", "location.name", "support_group",
    "support_group.name", "cost_center.name", "managed_by",
])
# The organization's ship-to field, if it has one (profile servicenow.ship_to_field).
SHIP_TO_FIELD = PROFILE.servicenow.ship_to_field
_INCIDENT_FIELDS = ",".join([
    "sys_id", "number", "short_description", "description", "state", "priority", "urgency", "impact",
    "category", "subcategory", "sys_created_on", "sys_updated_on", "assignment_group", "assignment_group.name",
    "assigned_to.name", "cmdb_ci.name", "caller_id", "caller_id.name", "watch_list",
] + ([SHIP_TO_FIELD] if SHIP_TO_FIELD else []))

# Device kinds, which decide how a problem is fixed:
#   personal  - assigned to a person: replaced and shipped
#   shared    - department IT equipment (reading-room workstation, printer): repaired on site
#   clinical  - medical equipment (model category in the profile's devices.clinical_categories): repaired on
#               site by its support group, usually Clinical or Imaging Engineering
PERSONAL, SHARED, CLINICAL = "personal", "shared", "clinical"


class NotSignedIn(Exception):
    """No ServiceNow token for this request, or ServiceNow rejected it."""


class ServiceNowError(Exception):
    pass


class Hibernating(ServiceNowError):
    """Developer instances hibernate when idle and answer with an HTML page."""


class Unavailable(ServiceNowError):
    """ServiceNow didn't answer in time, or couldn't be reached."""


def _base() -> str:
    return config.SN_INSTANCE_URL.rstrip("/")


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """One client for the process: connections (and TLS sessions) are reused across calls."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, limits=httpx.Limits(max_connections=20))
    return _client


async def _request(method: str, path: str, *, token: str | None = None, params: dict | None = None,
                   json: dict | None = None, content: bytes | None = None, headers: dict | None = None) -> dict:
    token = token or user_token.get()
    if not token:
        raise NotSignedIn()
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json", **(headers or {})}
    where = f"{method} {path.split('?')[0]}"
    try:
        r = await _http().request(method, _base() + path, params=params, json=json, content=content, headers=hdrs)
    except httpx.TimeoutException as exc:
        raise Unavailable(f"{where}: ServiceNow did not answer in time") from exc
    except httpx.TransportError as exc:
        raise Unavailable(f"{where}: ServiceNow could not be reached ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:  # e.g. a body labelled gzip that isn't (a proxy's error page)
        raise ServiceNowError(f"{where}: unreadable answer ({type(exc).__name__})") from exc
    if r.status_code == 401:
        raise NotSignedIn()
    is_json = "json" in r.headers.get("content-type", "")
    try:
        body = r.json() if is_json and r.content else {}
    except ValueError:
        body, is_json = {}, False
    if r.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        detail = (error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else "") or \
            f"HTTP {r.status_code}" + ("" if is_json else " (non-JSON page)")
        if not is_json and "hibernat" in r.text[:2000].lower():
            raise Hibernating("ServiceNow returned a hibernation page; the instance is waking up.")
        raise ServiceNowError(f"{where} -> {r.status_code}: {detail}")
    if not is_json:
        # A 200 HTML page: developer instances answer like this while hibernating or waking up.
        raise Hibernating("ServiceNow returned a non-JSON page; the instance may be hibernating.")
    if not r.content and method != "DELETE" and r.status_code != 204:
        raise ServiceNowError(f"{where} -> {r.status_code}: empty answer")
    return body if isinstance(body, dict) else {"result": body}


def _value(field) -> str:
    """Reference fields come back as {"link": ..., "value": ...} unless displayed. Always text: any
    other JSON value (a number, a list, null) from an unusual instance or proxy becomes text or ""."""
    if isinstance(field, dict):
        field = field.get("value", "")
    if field is None or isinstance(field, (list, dict)):
        return ""
    return field if isinstance(field, str) else str(field)


def _g(rec, key: str, default: str = "") -> str:
    """A row's field as text, whatever JSON ServiceNow (or a proxy) sent."""
    return (_value(rec.get(key)) if isinstance(rec, dict) else "") or default


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
    fields = ("sys_id,user_name,name,first_name,email,phone,mobile_phone,title,department,"
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
        "department_id": _value(rec.get("department")),
        **await _groups(sys_id, token),
        "is_admin": await _is_admin(sys_id, token),
        "cost_center": " ".join(filter(None, [rec.get("cost_center.code"), rec.get("cost_center.name")])),
        "manager": rec.get("manager.name", ""),
        "location": rec.get("location.name", ""),
        "location_address": address,
    }
    now = time.monotonic()
    if len(_user_cache) > 200:  # each token refresh is a new key: drop expired entries
        for k in [k for k, (expires, _) in _user_cache.items() if expires <= now]:
            _user_cache.pop(k, None)
    _user_cache[key] = (now + 300, user)
    return user


async def _is_admin(user_sys_id: str, token: str) -> bool:
    """True when the signed-in account has the admin role (D11): tickets would be filed as that account,
    usually by mistake (someone authorized Gemini Enterprise with an admin account). Only an account
    that can read role grants can see this, which is itself a sign of admin; others read False."""
    try:
        rows = (await _request("GET", "/api/now/table/sys_user_has_role", token=token, params={
            "sysparm_query": f"user={user_sys_id}^role.name=admin", "sysparm_fields": "sys_id", "sysparm_limit": 1,
        })).get("result", [])
    except ServiceNowError:
        return False
    return bool(rows)


async def _groups(user_sys_id: str, token: str) -> dict:
    """The user's group memberships, used to recognise equipment their group supports.
    Empty if ServiceNow doesn't let the user read memberships."""
    try:
        rows = (await _request("GET", "/api/now/table/sys_user_grmember", token=token, params={
            "sysparm_query": f"user={user_sys_id}", "sysparm_fields": "group,group.name", "sysparm_limit": 500,
        })).get("result", [])
    except ServiceNowError as exc:
        logger.info("group memberships not readable: %s", exc)
        rows = []
    return {"group_ids": [_value(r.get("group")) for r in rows],
            "groups": [r.get("group.name", "") for r in rows]}


# --- Assets ----------------------------------------------------------------------


_LAPTOP_NAMES = ("macbook", "thinkpad", "latitude", "elitebook", "probook", "xps", "surface laptop",
                 "zenbook", "spectre", "envy", "chromebook", "notebook", "laptop")


def _device_type(category: str, name: str) -> str:
    """The profile's devices.category_types decides when it lists the model category. Otherwise a
    guess: ServiceNow's demo data files laptops under the generic "Computer" category, so the model
    name decides laptop vs desktop."""
    mapped = {k.lower(): v for k, v in PROFILE.devices.category_types.items()}.get(category.lower())
    if mapped:
        return mapped
    category, name = category.lower(), name.lower()
    if any(w in category for w in ("laptop", "notebook")) or any(w in name for w in _LAPTOP_NAMES):
        return "laptop"
    for kind, words in (("monitor", ("monitor", "display")), ("phone", ("phone", "mobile", "iphone", "pixel")),
                        ("tablet", ("tablet", "ipad")), ("desktop", ("desktop", "computer", "pc", "workstation"))):
        if any(w in category for w in words) or any(w in name for w in words):
            return kind
    return "other"


def _kind(category: str, assigned_to: str) -> str:
    if category.lower() in {c.lower() for c in PROFILE.devices.clinical_categories}:
        return CLINICAL
    return PERSONAL if assigned_to else SHARED


def _asset(rec: dict) -> dict:
    name = _g(rec, "model.display_name") or _g(rec, "display_name")
    category = _g(rec, "model_category.name") or ""
    device_kind = _kind(category, _g(rec, "assigned_to"))
    kind = "medical equipment" if device_kind == CLINICAL else _device_type(category, name)
    return {
        "sys_id": _g(rec, "sys_id"),
        "asset_tag": _g(rec, "asset_tag"),
        "serial_number": _g(rec, "serial_number"),
        "manufacturer": _g(rec, "model.manufacturer.name"),
        "model": _g(rec, "model.display_name") or _g(rec, "display_name"),
        "device_type": kind,
        "purchase_date": _g(rec, "purchase_date"),
        "warranty_end": _g(rec, "warranty_expiration"),
        "assigned_to": _g(rec, "assigned_to"),
        "assigned_to_name": _g(rec, "assigned_to.name"),
        "ci": _g(rec, "ci"),
        "kind": device_kind,
        "category": category,
        "department_id": _g(rec, "department"),
        "department": _g(rec, "department.name"),
        "location_id": _g(rec, "location"),
        "location": _g(rec, "location.name"),
        "support_group_id": _g(rec, "support_group"),
        "support_group": _g(rec, "support_group.name"),
        "cost_center": _g(rec, "cost_center.name"),
        "managed_by": _g(rec, "managed_by"),
    }


async def _assets(query: str, limit: int) -> list[dict]:
    """Searches every configured asset table (alm_hardware, plus e.g. a clinical
    device table where a hospital keeps medical equipment separately)."""
    out: list[dict] = []
    for table in PROFILE.servicenow.asset_tables:
        result = await _request("GET", f"/api/now/table/{table}",
                                params={"sysparm_query": query, "sysparm_fields": _ASSET_FIELDS, "sysparm_limit": limit})
        out += [_asset(r) for r in result.get("result", [])]
        if len(out) >= limit:
            break
    out = out[:limit]
    await _link_cis(out)
    return out


async def _link_cis(assets: list[dict]) -> None:
    """ServiceNow keeps an asset's `ci` only when its model category has a CI class;
    equipment categories often don't, but the CI still points back at the asset."""
    missing = {a["sys_id"]: a for a in assets if not a["ci"]}
    if not missing:
        return
    try:
        result = await _request("GET", "/api/now/table/cmdb_ci", params={
            "sysparm_query": f"assetIN{','.join(missing)}", "sysparm_fields": "sys_id,asset", "sysparm_limit": len(missing)})
    except ServiceNowError as exc:
        logger.info("CIs not readable: %s", exc)
        return
    for row in result.get("result", []):
        asset = missing.get(_value(row.get("asset")))
        if asset:
            asset["ci"] = row["sys_id"]


async def my_assets(user_sys_id: str, limit: int = 20) -> list[dict]:
    return await _assets(f"assigned_to={user_sys_id}^install_status!=7^ORDERBYmodel_category", limit)  # 7 = retired


async def department_assets(department_id: str, limit: int = 100) -> list[dict]:
    """Equipment owned by a department and not assigned to a person."""
    if not department_id:
        return []
    return await _assets(f"department={department_id}^assigned_toISEMPTY^install_status!=7^ORDERBYdisplay_name", limit)


async def search_assets(term: str, limit: int = 20) -> list[dict]:
    """Assets whose name, category or location mentions `term`, anywhere in the inventory."""
    term = re.sub(r"[^\w .+-]", "", term).strip()
    if len(term) < 2:
        return []
    q = (f"display_nameLIKE{term}^ORmodel_category.nameLIKE{term}^ORlocation.nameLIKE{term}"
         "^install_status!=7")
    return await _assets(q, limit)


_TICKET_NUMBER = re.compile(r"[A-Z]{2,8}\d{4,12}")


def correlation_key(value: str | None) -> str:
    """A correlation id as it may appear in a query: letters, digits, '-', '_' and ':' only. The id is
    built from the conversation id the client sends; the session store validates it today, but the
    query must not depend on that."""
    return "".join(ch for ch in (value or "") if ch.isascii() and (ch.isalnum() or ch in "-_:"))[:100]


def _identifier(value: str | None) -> str:
    """An asset tag or serial as it may appear in a query: letters, digits and dashes only, so
    user or photo text can never add query operators (^, ^OR, ^NQ...)."""
    return "".join(ch for ch in (value or "").upper() if ch.isalnum() or ch == "-")


async def find_asset(asset_tag: str = "", serial_number: str = "") -> dict | None:
    """By asset tag, then serial number. Returns None if the user can't see it."""
    clauses = []
    tag = _identifier(asset_tag)
    if tag:
        clauses.append(f"asset_tag={tag}")
    serial = _identifier(serial_number)
    if serial:
        clauses.append(f"serial_number={serial}")
    for clause in clauses:
        found = await _assets(clause, 1)
        if found:
            return found[0]
    return None


# --- Incidents -----------------------------------------------------------------------


def _incident(rec: dict) -> dict:
    return {
        "sys_id": _g(rec, "sys_id"),
        "number": _g(rec, "number"),
        "short_description": _g(rec, "short_description"),
        "description": _g(rec, "description"),
        "state": STATE_LABELS.get(_g(rec, "state"), _g(rec, "state")),
        "state_code": str(_g(rec, "state")),
        "priority": str(_g(rec, "priority")),
        "urgency": str(_g(rec, "urgency")),
        "impact": str(_g(rec, "impact")),
        "opened": _g(rec, "sys_created_on"),
        "updated": _g(rec, "sys_updated_on"),
        "assignment_group": _g(rec, "assignment_group.name"),
        "assigned_to": _g(rec, "assigned_to.name"),
        "device": _g(rec, "cmdb_ci.name"),
        "assignment_group_id": _g(rec, "assignment_group"),
        "caller_id": _g(rec, "caller_id"),
        "caller": _g(rec, "caller_id.name"),
        "watch_list": [w for w in str(_g(rec, "watch_list") or "").split(",") if w],
        "ship_to": _g(rec, SHIP_TO_FIELD) if SHIP_TO_FIELD else "",
        "url": f"{_base()}/nav_to.do?uri=incident.do?sys_id={_g(rec, "sys_id")}",
    }


def _mine(user_sys_id: str) -> str:
    """The filter every ticket read and write goes through: tickets the user
    reported or follows (watch list). `^OR` binds to the condition before it."""
    return f"caller_id={user_sys_id}^ORwatch_listLIKE{user_sys_id}^category={HARDWARE}"


async def open_incidents_for_ci(ci: str, limit: int = 3) -> list[dict]:
    """Open hardware tickets on a device, whoever reported them: shared equipment
    is often reported by several people. Empty if ServiceNow hides them."""
    if not ci:
        return []
    q = f"cmdb_ci={ci}^category={HARDWARE}^stateIN{','.join(ACTIVE_STATES)}^ORDERBYDESCsys_created_on"
    try:
        result = await _request("GET", "/api/now/table/incident",
                                params={"sysparm_query": q, "sysparm_fields": _INCIDENT_FIELDS, "sysparm_limit": limit})
    except ServiceNowError as exc:
        logger.info("open tickets for the device not readable: %s", exc)
        return []
    return [_incident(r) for r in result.get("result", [])]


# The watch list is one text field: following is read, add, write. Two people following at the same
# moment could each write a list without the other. So in this process, changes to one ticket's list
# take turns; each write is read back and retried (a stale write may also be refused by the custom
# role's follow-only business rule); and one look shortly afterwards repairs a change that a write from
# another server instance undid.
WATCH_RECHECK_SECONDS = 1.0
_WATCH_LOCKS: dict[str, asyncio.Lock] = {}


async def _watchers(sys_id: str) -> list[str]:
    current = (await _request("GET", f"/api/now/table/incident/{sys_id}",
                              params={"sysparm_fields": "watch_list", "sysparm_exclude_reference_link": "true"})
               ).get("result") or {}
    return [w for w in _value(current.get("watch_list")).split(",") if w]


async def _set_watching(user_sys_id: str, sys_id: str, watching: bool) -> bool:
    """Adds or removes only this user; True if ServiceNow ended up as asked."""
    async def apply() -> bool:
        for _ in range(3):
            current = await _watchers(sys_id)
            if (user_sys_id in current) == watching:
                return True
            wanted = current + [user_sys_id] if watching else [w for w in current if w != user_sys_id]
            try:
                await _request("PATCH", f"/api/now/table/incident/{sys_id}", json={"watch_list": ",".join(wanted)},
                               params={"sysparm_fields": "sys_id"})
            except ServiceNowError as exc:  # e.g. the business rule refused a list that was already stale
                logger.info("watch list change refused (%s); reading it again", type(exc).__name__)
        return (user_sys_id in await _watchers(sys_id)) == watching

    lock = _WATCH_LOCKS.setdefault(sys_id, asyncio.Lock())
    if len(_WATCH_LOCKS) > 2000:
        for key in [k for k, v in _WATCH_LOCKS.items() if not v.locked()][:1000]:
            _WATCH_LOCKS.pop(key, None)
    async with lock:
        done = await apply()
    if done and WATCH_RECHECK_SECONDS:
        await asyncio.sleep(WATCH_RECHECK_SECONDS)
        async with lock:
            done = await apply()
    return done


async def _ticket(sys_id: str) -> dict:
    result = await _request("GET", f"/api/now/table/incident/{sys_id}",
                            params={"sysparm_fields": _INCIDENT_FIELDS, "sysparm_display_value": "false"})
    return _incident(result.get("result") or {})


async def follow_incident(user_sys_id: str, sys_id: str, note: str) -> dict:
    """Adds the user to a ticket's watch list (so it shows up as theirs) and adds their note, as two
    separate writes so a refused watch-list change can't lose the note. Returns the ticket as saved."""
    await _set_watching(user_sys_id, sys_id, True)
    if note:
        await _request("PATCH", f"/api/now/table/incident/{sys_id}", json={"comments": note},
                       params={"sysparm_fields": "sys_id"})
    return await _ticket(sys_id)


async def unfollow_incident(user_sys_id: str, sys_id: str) -> dict:
    """Takes only this user off a ticket's watch list. Returns the ticket as ServiceNow saved it."""
    await _set_watching(user_sys_id, sys_id, False)
    return await _ticket(sys_id)


async def create_incident(fields: dict) -> dict:
    result = await _request("POST", "/api/now/table/incident", json=fields,
                            params={"sysparm_fields": _INCIDENT_FIELDS, "sysparm_display_value": "false"})
    return _incident(result["result"])


async def dropped_fields(sys_id: str, wanted: dict[str, str]) -> dict[str, str]:
    """Fields ServiceNow didn't store as asked (it drops what a user may not set, silently)."""
    if not wanted:
        return {}
    try:
        result = await _request("GET", f"/api/now/table/incident/{sys_id}", params={
            "sysparm_fields": ",".join(wanted), "sysparm_display_value": "false"})
    except ServiceNowError as exc:
        logger.info("could not read back configured fields: %s", exc)
        return {}
    stored = result.get("result") or {}
    return {k: v for k, v in wanted.items() if str(_value(stored.get(k))) != str(v)}


async def find_open_by_correlation(user_sys_id: str, correlation_id: str) -> dict | None:
    """A ticket already filed from this conversation, so a retry never duplicates it."""
    correlation_id = correlation_key(correlation_id)
    if not correlation_id:
        return None
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
    if not _TICKET_NUMBER.fullmatch(number):
        return None  # also keeps query operators (^, ^NQ...) out of the "mine" filter
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
    text = (text if isinstance(text, str) else _value(text)).replace("\r\n", "\n").strip()
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
    return newest_first(entries)


_NOTE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S",
                      "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %I:%M:%S %p", "%d.%m.%Y %H:%M:%S")


def _note_time(when: str):
    for fmt in _NOTE_TIME_FORMATS:
        try:
            return datetime.datetime.strptime((when or "").strip(), fmt)
        except ValueError:
            continue
    return None


def newest_first(entries: list[dict]) -> list[dict]:
    """By time, newest first. Display formats ("09/25/2026 05:55 PM") don't sort as text, so times
    are parsed; if any can't be, ServiceNow's own order (newest first per field) is kept."""
    times = [_note_time(e.get("when", "")) for e in entries]
    if None in times:
        return list(entries)
    order = sorted(range(len(entries)), key=lambda i: times[i], reverse=True)
    return [entries[i] for i in order]


async def attach(table_sys_id: str, filename: str, data: bytes, mime_type: str) -> None:
    await _request("POST", "/api/now/attachment/file", content=data, headers={"Content-Type": mime_type},
                   params={"table_name": "incident", "table_sys_id": table_sys_id, "file_name": filename})


# --- Answers in an unexpected shape -------------------------------------------------------------
# Every public call turns a malformed answer ({"result": "x"}, {"result": null}, a missing field)
# into a ServiceNowError, which the tools explain, instead of a crash in the middle of a turn.

def _shape_guard(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (AttributeError, KeyError, TypeError, IndexError, ValueError) as exc:
            logger.warning("unexpected ServiceNow answer in %s: %s", fn.__name__, type(exc).__name__)
            raise ServiceNowError(f"{fn.__name__}: unexpected answer from ServiceNow ({type(exc).__name__})") from exc
    return wrapper


for _name, _fn in list(globals().items()):
    if not _name.startswith("_") and inspect.iscoroutinefunction(_fn) and _fn.__module__ == __name__:
        globals()[_name] = _shape_guard(_fn)
