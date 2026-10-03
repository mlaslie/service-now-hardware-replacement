"""Wizard tools. Each one advances the request and stages the next card.

A tool never returns UI JSON to the model. It puts the card in `temp:card`,
returns a short summary, and `agent.render_staged_card` sends the card to the
user in place of the next model call. That keeps the step order in the model's
hands while the cards themselves stay deterministic.

Every ServiceNow call runs as the signed-in person, with the token Gemini
Enterprise forwarded (see `servicenow.py`). The requester comes from that
token, never from a tool argument, so the model cannot file or read tickets
on someone else's behalf.
"""

import asyncio
import datetime as dt
import functools
import logging
import re
import uuid

from google.adk.tools import ToolContext

from app import cards, memory, servicenow, vision
from app.cards import ISSUE_LABELS
from app.profile import URGENCY_ORDER

logger = logging.getLogger(__name__)

CARD_KEY = "temp:card"

# Problem choices, photo rules, urgency floors, service texts: config/organization.yaml.
PROFILE = cards.PROFILE

# Matches the priority ServiceNow derives from servicenow.URGENCY_TO_IMPACT_URGENCY.
_PRIORITY = {"low": "4 - Low", "normal": "3 - Moderate", "high": "2 - High", "critical": "1 - Critical"}
# Words that don't help find equipment by description.
_STOP_WORDS = {"the", "a", "an", "in", "on", "at", "of", "to", "is", "it", "its", "our", "my", "and", "or", "with",
               "has", "have", "not", "broken", "down", "working", "please", "there", "this", "that", "room", "bay",
               "floor", "unit", "department", "dept", "one", "machine", "device", "equipment"}


# --- helpers ------------------------------------------------------------------


def _jsonable(value):
    """Session state is stored as JSON."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _show(ctx: ToolContext, messages: list[dict]) -> None:
    ctx.state[CARD_KEY] = messages


def _new_draft() -> dict:
    return {"id": uuid.uuid4().hex[:8]}


def _draft(ctx: ToolContext) -> dict:
    draft = dict(ctx.state.get("draft") or {})
    if not draft.get("id"):
        # Saved at once: submit_ticket's duplicate check keys on it.
        draft["id"] = _new_draft()["id"]
        _save(ctx, draft)
    return draft


def _intake_draft(ctx: ToolContext) -> dict:
    """The draft to add device, problem or photo details to. Once a request is
    filed, new details (e.g. a photo sent after the confirmation) start a new
    one instead of editing the filed request."""
    draft = _draft(ctx)
    if draft.get("submitted_number"):
        draft = _new_draft()
        _save(ctx, draft)
    return draft


def _save(ctx: ToolContext, draft: dict) -> None:
    ctx.state["draft"] = _jsonable(draft)


async def _employee(ctx: ToolContext) -> dict | None:
    """The signed-in person's ServiceNow profile, refreshed by the A2A layer every turn."""
    user = ctx.state.get("end_user") or {}
    if not user.get("verified"):
        return None
    return user.get("profile") or None


def _no_identity(ctx: ToolContext) -> dict:
    problem = (ctx.state.get("end_user") or {}).get("problem", "")
    if problem == "servicenow_hibernating":
        msg = "ServiceNow is waking up. Ask the user to try again in a minute."
    elif problem in ("no_token", "token_rejected"):
        msg = ("The user's ServiceNow sign-in is missing or expired. Tell them to reconnect ServiceNow "
               "for this agent in Gemini Enterprise, then try again.")
    else:
        msg = "ServiceNow could not be reached. Ask the user to try again shortly."
    return {"status": "error", "message": msg}


def servicenow_errors(fn):
    """Turns ServiceNow failures into a result the model can explain in one sentence."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except servicenow.NotSignedIn:
            return {"status": "error", "message": "The user's ServiceNow sign-in expired. Tell them to "
                    "reconnect ServiceNow for this agent in Gemini Enterprise, then try again."}
        except servicenow.Hibernating:
            return {"status": "error", "message": "ServiceNow is waking up. Ask the user to try again in a minute."}
        except servicenow.ServiceNowError as exc:
            logger.warning("ServiceNow call failed in %s: %s", fn.__name__, exc)
            return {"status": "error", "message": f"ServiceNow refused the request ({exc}). "
                    "Say it couldn't be completed and suggest contacting the service desk."}
    return wrapper


def normalize_tag(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", (value or "").upper())


def eligibility(asset: dict) -> dict:
    """Warranty and refresh status, which decide how a replacement is funded."""
    out = {"in_warranty": False, "refresh_eligible": False, "age_years": None, "summary": "Unknown device age"}
    today = dt.date.today()
    try:
        purchased = dt.date.fromisoformat((asset.get("purchase_date") or "")[:10])
    except ValueError:
        purchased = None
    warranty_end = (asset.get("warranty_end") or "")[:10]
    try:
        out["in_warranty"] = bool(warranty_end) and dt.date.fromisoformat(warranty_end) >= today
    except ValueError:
        warranty_end = ""
    if purchased:
        age = (today - purchased).days / 365.25
        out["age_years"] = round(age, 1)
        years = PROFILE.devices.refresh_years.get(asset.get("device_type", ""), PROFILE.devices.default_refresh_years)
        out["refresh_eligible"] = age >= years
    if out["refresh_eligible"]:
        out["summary"] = f"Refresh eligible ({out['age_years']} yrs old)"
    elif out["in_warranty"]:
        out["summary"] = f"Under warranty until {warranty_end}"
    elif warranty_end:
        out["summary"] = f"Out of warranty since {warranty_end}"
    return out


# Characters a label photo (or a person) commonly confuses, mapped to one form.
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def _loose(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper()).translate(_CONFUSABLE)


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return edits + (len(b) - j) <= 1


def _words(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def match_own_asset(assets: list[dict], identifier: str = "", model: str = "",
                    manufacturer: str = "") -> tuple[dict | None, str]:
    """Picks one of the user's own devices when an exact inventory lookup failed.

    `identifier` is a serial number or asset tag as read or typed: compared with
    commonly confused characters folded (O/0, I/1, S/5...), one wrong, missing or
    extra character allowed, and Apple's "S" prefix ignored. `model` is compared
    by name. Only a single candidate counts. Returns (asset, what matched).
    """
    raw = re.sub(r"[^A-Z0-9]", "", (identifier or "").upper())
    if len(raw) >= 5:
        keys = {_loose(raw)} | ({_loose(raw[1:])} if raw.startswith("S") and len(raw) >= 9 else set())
        for field, label in (("serial_number", "serial number"), ("asset_tag", "asset tag")):
            hits = [a for a in assets if (target := _loose(a.get(field, ""))) and len(target) >= 5
                    and any(k == target or (len(target) >= 6 and _within_one_edit(k, target)) for k in keys)]
            if len(hits) == 1:
                return hits[0], label
    wanted = _words(model)
    if len(wanted) >= 4:
        maker = _words(manufacturer)
        hits = [a for a in assets
                if (have := _words(a.get("model", ""))) and (wanted in have or have in wanted)
                and (not maker or not a.get("manufacturer") or maker in _words(a["manufacturer"])
                     or _words(a["manufacturer"]) in maker)]
        if len(hits) == 1:
            return hits[0], "model"
    return None, ""


def _match_note(asset: dict, how: str) -> str:
    return (f"Matched to your {_device_name(asset)} by its {how}. If that's not the right device, "
            "choose Change something.")


def _device_name(asset: dict) -> str:
    return f"{asset.get('model') or 'device'} ({asset.get('asset_tag', '')})"


def _asset_summary(assets: list[dict]) -> list[dict]:
    return [{"asset_tag": a["asset_tag"], "type": a.get("device_type"), "model": a.get("model")} for a in assets]


def _relation(asset: dict, employee: dict) -> tuple[str, str, str]:
    """How the reporter relates to the device: (relation, text for the user, note
    for the ticket). Anyone may report anything; this only says what is known."""
    me, name = employee.get("sys_id"), employee.get("name", "the reporter")
    if asset.get("assigned_to"):
        if asset["assigned_to"] == me:
            return "yours", "You", "Assigned to the reporter"
        owner = asset.get("assigned_to_name") or "another person"
        return ("unconfirmed", f"{owner}, not you. You can still report it; the ticket will say so.",
                f"Ownership could not be confirmed: assigned to {owner}, reported by {name}")
    if asset.get("department_id") and asset["department_id"] == employee.get("department_id"):
        return ("department", f"{asset.get('department')} (your department)",
                f"Department equipment ({asset.get('department')}); the reporter is in this department")
    if asset.get("support_group_id") and asset["support_group_id"] in (employee.get("group_ids") or []):
        return ("group", f"{asset.get('department') or 'Shared equipment'}, supported by your group "
                f"({asset.get('support_group')})",
                f"Supported by {asset.get('support_group')}; the reporter is a member of that group")
    owner = asset.get("department") or "no department on record"
    return ("unconfirmed", f"{owner}. Not registered to you or your department; you can still report it "
            "and the ticket will say so.",
            f"Ownership could not be confirmed: registered to {owner}, reported by {name}, who is not "
            "in that department or its support group")


def _device_from_asset(asset: dict, employee: dict, confirmed: bool = False) -> dict:
    keys = ("sys_id", "asset_tag", "serial_number", "manufacturer", "model", "device_type", "purchase_date",
            "warranty_end", "assigned_to", "assigned_to_name", "ci", "kind", "category", "department",
            "department_id", "location", "location_id", "support_group", "support_group_id", "cost_center",
            "managed_by")
    relation, text, note = _relation(asset, employee)
    return {k: asset.get(k, "") for k in keys} | {
        "in_inventory": True, "relation": relation, "relation_text": text, "ownership_note": note,
        # A label photo that matched exactly already proves which device it is.
        "confirmed": confirmed}


def _set_device(draft: dict, device: dict, asset: dict | None = None) -> dict:
    """Puts a device on the draft, clearing what belonged to the previous one."""
    for key in ("suggested_device", "duplicates_checked", "match_note"):
        draft.pop(key, None)
    draft["device"] = device
    if asset:
        draft["eligibility"] = eligibility(asset)
    else:
        draft.pop("eligibility", None)
    return draft


def _words_of(asset: dict) -> set[str]:
    text = " ".join(str(asset.get(k) or "") for k in ("model", "manufacturer", "category", "device_type",
                                                      "location", "department", "asset_tag"))
    return set(_words(text).split())


def _score(asset: dict, wanted: list[str]) -> int:
    have = _words_of(asset)
    return sum(1 for w in wanted if w in have or (len(w) >= 4 and any(w in h for h in have)))


def _query_words(description: str) -> list[str]:
    return [w for w in _words(description).split() if w not in _STOP_WORDS and (len(w) > 1 or w.isdigit())]


# --- delivery addresses -------------------------------------------------------------
# A ticket's ship-to is always a real street address: the one in ServiceNow, a saved
# permanent address (verbatim, from memory.saved_addresses), or one the user typed.
# Temporary places (hotels, events) are used once and never saved or suggested.

_TEMPORARY = re.compile(r"\b(hotel|motel|inn|suites?|resort|lodge|marriott|hilton|hyatt|sheraton|westin|airbnb|"
                        r"conference|convention|event|airport|temporary|temp)\b", re.I)
_ADDRESS_ON_FILE = re.compile(r"\b(usual|default|on file|servicenow|original|regular|normal)\b", re.I)
_PLACE_ALIASES = {"home": ("home", "house", "my place", "apartment", "residence"), "office": ("office", "work")}


def _looks_like_street_address(text: str) -> bool:
    return bool(re.search(r"\d", text)) and len(text.split()) >= 3


def _has_phrase(text: str, phrase: str) -> bool:
    """Whole words only: "house" is in "my house" but not in "500 Warehouse Ave"."""
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I) is not None


def _canonical_place(text: str) -> str:
    return next((name for name, words in _PLACE_ALIASES.items() if any(_has_phrase(text, w) for w in words)), "")


def _match_saved(text: str, saved: list[dict]) -> dict | None:
    """A saved address the user referred to: by its address, or by its label ("my house" -> Home).
    A typed street address is only ever matched by the address itself, so "500 Warehouse Ave" stays
    what the user typed even with a saved "Home"."""
    key = memory.normalize_address(text)
    for a in saved:
        if key and key == memory.normalize_address(a["address"]):
            return a
    if _looks_like_street_address(text):
        return None
    place = _canonical_place(text)
    for a in saved:
        label = a.get("label") or ""
        if label and (_has_phrase(text, label) or (place and _canonical_place(label) == place)):
            return a
    return None


def _set_delivery(draft: dict, address: str = "", label: str = "", kind: str = "", saved: bool = False) -> None:
    if not address:
        draft.pop("delivery", None)
        draft.pop("delivery_location", None)
        return
    draft["delivery"] = {"address": address, "label": label, "kind": kind, "saved": saved}
    draft["delivery_location"] = address


async def _resolve_address(email: str, text: str, label: str = "", kind: str = "",
                           saved: list[dict] | None = None) -> dict:
    """{"address", "label", "kind", "saved"} for what the user asked, or {"error": ...}."""
    text = " ".join((text or "").split())
    saved = saved if saved is not None else await memory.saved_addresses(email)
    match = _match_saved(text, saved) or (_match_saved(label, saved) if label else None)
    if match:
        return {"address": match["address"], "label": match.get("label", ""), "kind": "permanent", "saved": True}
    if not _looks_like_street_address(text):
        return {"error": f"{text!r} is not a street address. Ask for the full address (street, city, state, "
                         "ZIP). Nothing was changed."}
    if kind not in ("permanent", "temporary"):
        kind = "temporary" if _TEMPORARY.search(f"{text} {label}") else "permanent"
    return {"address": text, "label": (label or _canonical_place(label or text).title()).strip(), "kind": kind,
            "saved": False}


def _refresh(draft: dict, employee: dict) -> dict:
    """Derives priority, recommendation, SLA and warnings from what is known."""
    issue = draft.get("issue") or {}
    category = issue.get("category", "")
    elig = draft.get("eligibility") or {}
    evidence = draft.get("evidence") or {}

    urgency = issue.get("urgency", "normal")
    if urgency not in URGENCY_ORDER:
        urgency = "normal"
    floor = PROFILE.min_urgency(category)  # e.g. a safety concern is always critical
    if floor and URGENCY_ORDER.index(floor) > URGENCY_ORDER.index(urgency):
        urgency = floor
    draft["priority"] = _PRIORITY[urgency]
    device = draft.get("device") or {}
    texts = PROFILE.service.recommendations
    chosen = PROFILE.issue(category)

    if cards.is_equipment(device):
        # Who does it is its own line ("Handled by").
        rec = texts.equipment_safety if PROFILE.is_safety(category) else texts.equipment_repair
    elif elig.get("refresh_eligible"):
        rec = texts.refresh
    elif (chosen and chosen.replace) or evidence.get("supports_replacement"):
        rec = (texts.warranty_replacement if elig.get("in_warranty")
               else texts.charged_replacement.format(cost_center=employee.get("cost_center") or "department"))
    elif chosen and chosen.recommendation:
        rec = chosen.recommendation
    elif elig.get("in_warranty"):
        rec = texts.warranty_repair
    else:
        rec = texts.repair_assessment
    draft["recommendation"] = rec
    targets = (PROFILE.service.equipment_response_targets if cards.is_equipment(device)
               else PROFILE.service.personal_response_targets)
    draft["sla"] = targets[draft["priority"][0]]

    warnings = list(draft.get("photo_warnings") or [])
    if draft.get("match_note"):
        warnings.append(draft["match_note"])
    # Unconfirmed ownership is shown once, as "Belongs to" (and noted on the ticket).
    if device and not device.get("in_inventory"):
        warnings.append("Device not found in inventory; details were read from your photo.")
    suggested = draft.get("suggested_device") or {}
    if suggested and suggested.get("asset_tag") != device.get("asset_tag"):
        warnings.append(f"Your photo shows asset {suggested['asset_tag']} ({suggested.get('model') or 'device'}), "
                        f"not the selected {device.get('asset_tag')}.")
    mismatch = _photo_mismatch(draft)
    if mismatch:
        warnings.append(mismatch)
    need, _ = PROFILE.photo_policy(category)
    if need == "required" and not evidence:
        warnings.append("A photo of the damage is still needed before this can be submitted.")
    draft["warnings"] = warnings
    return draft


def _photo_mismatch(draft: dict) -> str:
    """Flags a photo that shows a different kind or make of device than the one
    selected. Asset tag and serial checks can't catch this when the photo shows
    no label, e.g. a MacBook photo filed against a ThinkPad."""
    device = draft.get("device") or {}
    if not device.get("in_inventory"):
        return ""
    for photo in draft.get("photos") or []:
        f = photo.get("findings") or {}
        seen_type = (f.get("device_type") or "").lower()
        seen_make = (f.get("manufacturer") or "").lower()
        wrong_type = seen_type and seen_type != "other" and seen_type != (device.get("device_type") or "").lower()
        wrong_make = seen_make and seen_make not in (device.get("manufacturer") or "").lower() \
            and seen_make not in (device.get("model") or "").lower()
        if wrong_type or wrong_make:
            seen = " ".join(filter(None, [f.get("manufacturer"), f.get("model")])) or seen_type
            return (f"The photo shows {seen}, but the selected device is "
                    f"{device.get('manufacturer', '')} {device.get('model', '')}. Please confirm the device.").replace("  ", " ")
    return ""


async def _review(ctx: ToolContext, draft: dict, employee: dict) -> dict:
    draft = _refresh(draft, employee)
    if "saved_addresses" not in draft and not cards.is_equipment(draft.get("device") or {}):
        on_file = memory.normalize_address(employee.get("location_address", ""))
        draft["saved_addresses"] = [a for a in await memory.saved_addresses(employee.get("email", ""))
                                    if memory.normalize_address(a["address"]) != on_file][:3]
    _save(ctx, draft)
    _show(ctx, cards.review(draft, employee))
    ship_to = (draft.get("delivery") or {}).get("address") or employee.get("location_address") or employee.get("location")
    return {"status": "ok", "step": "review", "shown": "review card with Submit and Change buttons",
            "priority": draft["priority"], "recommendation": draft["recommendation"],
            "warnings": draft["warnings"], "ship_to": ship_to,
            "saved_addresses_offered_as_buttons": draft.get("saved_addresses", [])}


async def _next_step(ctx: ToolContext, draft: dict, employee: dict) -> dict:
    """Shows whichever step is still missing, so the wizard skips what is known."""
    _save(ctx, draft)
    device, issue = draft.get("device"), draft.get("issue")
    if not device:
        assets = await servicenow.my_assets(employee["sys_id"])
        _show(ctx, cards.device_picker(employee, assets))
        return {"status": "ok", "step": "choose_device", "assets": _asset_summary(assets)}
    if not device.get("confirmed"):
        _show(ctx, cards.confirm_device(device, draft.get("match_note", "")))
        result = {"status": "ok", "step": "confirm_device", "device": _device_name(device),
                  "belongs_to": device.get("relation_text")}
        if not issue:
            # The model tends to stop at this card; a problem already described would then be asked again.
            result["next"] = ("If the user's message already said what is wrong, call set_issue now, in this "
                              "turn: it is kept while they check the device. Never call confirm_device yourself.")
        return result
    if cards.is_equipment(device) and device.get("ci") and not draft.get("duplicates_checked"):
        draft["duplicates_checked"] = True
        _save(ctx, draft)
        existing = await servicenow.open_incidents_for_ci(device["ci"])
        if existing:
            _show(ctx, cards.existing_tickets(device, existing))
            return {"status": "ok", "step": "already_reported",
                    "open_tickets": [{k: t[k] for k in ("number", "state", "short_description", "caller")}
                                     for t in existing]}
    if not issue:
        _show(ctx, cards.issue_picker(device, (draft.get("evidence") or {}).get("category", "")))
        return {"status": "ok", "step": "describe_issue", "device": device.get("model")}
    need, what = PROFILE.photo_policy(issue["category"])
    if need and not draft.get("evidence") and not draft.get("photo_skipped"):
        _show(ctx, cards.photo_request(device, ISSUE_LABELS.get(issue["category"], "The problem"), what, need == "required"))
        return {"status": "ok", "step": "photo", "photo": need}
    return await _review(ctx, draft, employee)


# --- tools ------------------------------------------------------------------------


@servicenow_errors
async def start_request(tool_context: ToolContext) -> dict:
    """Starts a new hardware replacement request and shows the device picker (step 1).

    Call this when the user wants to replace or report broken hardware and no request
    is in progress, or when they click "Start another request".
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _new_draft()
    _save(tool_context, draft)
    tool_context.state["last_photo_ids"] = []
    result = await _next_step(tool_context, draft, employee)
    remembered = await memory.recall(tool_context, employee["email"])  # addresses are filtered out
    return result | {"employee": {k: employee.get(k) for k in ("name", "department", "location")},
                     "remembered_about_user": remembered}


@servicenow_errors
async def select_device(asset_tag: str, tool_context: ToolContext) -> dict:
    """Records which device the request is for, by asset tag, then shows the next step.

    Args:
        asset_tag: The asset tag (or serial number), from a device button or typed by the user.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    asset = await servicenow.find_asset(asset_tag=asset_tag) or await servicenow.find_asset(serial_number=asset_tag)
    how = ""
    if not asset:
        asset, how = match_own_asset(await _nearby_assets(employee), identifier=asset_tag)
    if not asset:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": f"No asset {asset_tag} in inventory; asked the user for a label photo."}
    draft = _set_device(_intake_draft(tool_context), _device_from_asset(asset, employee), asset)
    draft["match_note"] = _match_note(asset, how) if how else ""
    return await _next_step(tool_context, draft, employee)


async def _nearby_assets(employee: dict) -> list[dict]:
    """The user's own devices and their department's equipment: where a fuzzy
    match or a description is most likely to point."""
    own = await servicenow.my_assets(employee["sys_id"])
    dept = await servicenow.department_assets(employee.get("department_id", ""))
    seen = {a["sys_id"] for a in own}
    return own + [a for a in dept if a["sys_id"] not in seen]


@servicenow_errors
async def find_device(description: str, tool_context: ToolContext) -> dict:
    """Finds a device or piece of equipment from how the user describes it ("the MRI",
    "the portable x-ray", "bed 312", "the infusion pump in ED bay 7"), then asks the user to
    confirm it. Searches their own devices and their department's equipment first, then
    the whole inventory.

    Args:
        description: What the user called it, including any place they mentioned.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    wanted = _query_words(description)
    if not wanted:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": "Nothing to search for; asked for the asset tag or a photo."}
    candidates = await _nearby_assets(employee)
    best = _best_matches(candidates, wanted)
    if not best:
        # Beyond their own area, e.g. a pump found in another department.
        for word in sorted(wanted, key=len, reverse=True)[:2]:
            candidates += await servicenow.search_assets(word)
        best = _best_matches(candidates, wanted)
    if not best:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": f"Nothing matched {description!r}; asked for the asset tag or a photo."}
    if len(best) > 1:
        _show(tool_context, cards.device_choices("Which one do you mean?", best[:6]))
        return {"status": "ok", "step": "choose_device", "matches": _asset_summary(best[:6])}
    draft = _set_device(_intake_draft(tool_context), _device_from_asset(best[0], employee), best[0])
    return await _next_step(tool_context, draft, employee)


def _best_matches(assets: list[dict], wanted: list[str]) -> list[dict]:
    unique = list({a["sys_id"]: a for a in assets}.values())
    scored = [(a, _score(a, wanted)) for a in unique]
    top = max((sc for _, sc in scored), default=0)
    return [a for a, sc in scored if top and sc == top]


@servicenow_errors
async def confirm_device(correct: bool, tool_context: ToolContext) -> dict:
    """The user checked the device details shown (model, asset tag, serial).

    Args:
        correct: True if they said it's the right device, False if not.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if not draft.get("device"):
        return await _next_step(tool_context, draft, employee)
    if correct:
        draft["device"]["confirmed"] = True
        return await _next_step(tool_context, draft, employee)
    draft = _set_device(draft, {})
    draft.pop("device")
    result = await _next_step(tool_context, draft, employee)
    return result | {"message": "Not that device. Showed their devices; they can also give the asset tag, "
                                "serial number or a photo of the sticker."}


async def request_label_photo(tool_context: ToolContext) -> dict:
    """Asks the user for a photo of the asset tag or serial label.

    Use when the device isn't in their list ("A different device") or they don't know
    which device it is.
    """
    _show(tool_context, cards.label_photo_request())
    return {"status": "ok", "step": "label_photo"}


@servicenow_errors
async def set_issue(category: str, description: str, urgency: str, tool_context: ToolContext) -> dict:
    """Records what is wrong, then shows the photo step if useful, otherwise the review.

    Args:
        category: For the user's own devices: {PERSONAL_ISSUES}. For shared or
            equipment: {EQUIPMENT_ISSUES}. {SAFETY_RULE}
        description: The problem in the user's own words, lightly cleaned up. Use the
            category label if they only clicked a button.
        urgency: low, normal, high or critical. Infer it from what they said: "I have a
            client demo tomorrow" is high, "can't work at all" is critical. Default normal.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    if category not in ISSUE_LABELS:
        return {"status": "error", "message": f"Unknown category {category!r}. Use one of {list(ISSUE_LABELS)}."}
    draft = _intake_draft(tool_context)
    draft["issue"] = {"category": category, "description": description.strip(),
                      "urgency": urgency if urgency in _PRIORITY else "normal"}
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def analyze_photos(tool_context: ToolContext) -> dict:
    """Reads the photo(s) the user just attached: device, label data and damage.

    Call whenever a message contains "[Photo attached: ...]". Fills in the device from the
    asset tag or serial number, records damage as evidence, then shows the next step.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    photo_ids = tool_context.state.get("last_photo_ids") or []
    photos = [tool_context.state.get(f"photo:{pid}") for pid in photo_ids]
    photos = [p for p in photos if p]
    if not photos:
        return {"status": "error", "message": "No new photo found. Ask the user to attach it again."}

    draft = _intake_draft(tool_context)
    hint = (draft.get("issue") or {}).get("description", "")
    results = await asyncio.gather(
        *(vision.analyze_photo(p["uri"], p["mime_type"], hint) for p in photos), return_exceptions=True
    )
    findings = []
    for photo, result in zip(photos, results):
        if isinstance(result, Exception):
            logger.warning("photo analysis failed for %s: %s", photo["photo_id"], result)
            continue
        findings.append((photo, result.model_dump(mode="json")))
    tool_context.state["last_photo_ids"] = []
    if not findings:
        return {"status": "error", "message": "The photo could not be read. Ask the user to retake it in better light."}

    draft.setdefault("photos", [])
    photo_warnings = list(draft.get("photo_warnings") or [])
    note = ""
    owned: list[dict] | None = None
    for photo, f in findings:
        draft["photos"].append({"uri": photo["uri"], "photo_id": photo["photo_id"], "filename": photo.get("filename", ""),
                                "mime_type": photo["mime_type"], "findings": f})

        # Label data -> inventory match; failing that, one of the user's own devices.
        current = draft.get("device") or {}
        has_id = bool(f["asset_tag"] or f["serial_number"])
        asset = await servicenow.find_asset(f["asset_tag"], f["serial_number"]) if has_id else None
        how = ""
        if not asset and not current and f["image_kind"] != "unrelated":
            if owned is None:
                owned = await _nearby_assets(employee)
            asset, how = match_own_asset(owned, f["serial_number"] or f["asset_tag"], f["model"], f["manufacturer"])
        if asset:
            if current and current.get("asset_tag") != asset["asset_tag"]:
                # Offered on the review card as "Use <device> instead" (see _refresh).
                draft["suggested_device"] = _device_from_asset(asset, employee)
            elif not current:
                _set_device(draft, _device_from_asset(asset, employee, confirmed=not how), asset)
                draft["match_note"] = _match_note(asset, how) if how else ""
            photo_serial = f["serial_number"].upper().replace(" ", "")
            if not how and photo_serial and asset.get("serial_number") and photo_serial != asset["serial_number"].upper():
                photo_warnings.append(
                    f"Serial on the label ({photo_serial}) differs from inventory ({asset['serial_number']}).")
        elif has_id and not current:
            _set_device(draft, {
                "asset_tag": normalize_tag(f["asset_tag"]), "serial_number": f["serial_number"],
                "manufacturer": f["manufacturer"], "model": f["model"], "part_number": f["part_number"],
                "device_type": f["device_type"], "in_inventory": False, "confirmed": True,
                "kind": "clinical" if f["device_type"] == "medical equipment" else "personal",
                "relation": "unconfirmed", "relation_text": "Not found in inventory",
                "ownership_note": "Not found in inventory; details were read from the reporter's photo",
            })
        elif not current and f["image_kind"] in ("label", "device"):
            note = "I couldn't read an asset tag or serial number. Try a closer, sharper photo of the label."

        # Damage -> evidence.
        if f["damage_present"]:
            draft["evidence"] = {
                "summary": f"{f['damage_description']} (severity: {f['damage_severity']})",
                "severity": f["damage_severity"], "category": f["issue_category"],
                "supports_replacement": f["supports_replacement"], "photo_uri": photo["uri"],
            }
        elif draft.get("issue") and PROFILE.photo_policy(draft["issue"]["category"])[0] == "required" \
                and f["image_kind"] not in ("label",):
            photo_warnings.append("The photo didn't clearly show the damage; the desk may ask for another.")
            draft["evidence"] = {"summary": "Photo provided; damage not clearly visible", "severity": "none",
                                 "category": "", "supports_replacement": False, "photo_uri": photo["uri"]}
    draft["photo_warnings"] = photo_warnings

    summary = [{"image_kind": f["image_kind"], "device": f"{f['manufacturer']} {f['model']}".strip(),
                "asset_tag": f["asset_tag"], "serial": f["serial_number"],
                "damage": f["damage_description"] if f["damage_present"] else "none visible",
                "confidence": f["confidence"]} for _, f in findings]

    if not draft.get("device"):
        _save(tool_context, draft)
        assets = await servicenow.my_assets(employee["sys_id"])
        _show(tool_context, cards.photo_findings(findings[-1][1], assets, note))
        return {"status": "ok", "step": "device_unknown", "findings": summary, "assets": _asset_summary(assets)}
    result = await _next_step(tool_context, draft, employee)
    return result | {"findings": summary}


@servicenow_errors
async def skip_photo(tool_context: ToolContext) -> dict:
    """The user chose not to add a photo. Moves on to the review step."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    draft["photo_skipped"] = True
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def update_request(tool_context: ToolContext, description: str = "", urgency: str = "",
                         category: str = "", delivery_location: str = "", delivery_label: str = "",
                         delivery_kind: str = "") -> dict:
    """Changes details on the request, then shows the review card again.

    Args:
        description: New problem description, if the user changed it.
        urgency: low, normal, high or critical, if the user changed it.
        category: New issue category, if the user changed it.
        delivery_location: Only when the user asked to ship somewhere else: the full street
            address as they typed it, or their words for a saved place ("my house"), which is
            looked up. "my usual address" goes back to the address on file.
        delivery_label: A short name for a new address, e.g. "Home", "Denver office", "Hotel".
        delivery_kind: "permanent" (home, an office) or "temporary" (hotel, event, trip).
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    issue = dict(draft.get("issue") or {})
    if description:
        issue["description"] = description.strip()
    if urgency in _PRIORITY:
        issue["urgency"] = urgency
    if category in ISSUE_LABELS:
        issue["category"] = category
    if issue:
        draft["issue"] = issue
    if delivery_location:
        if _ADDRESS_ON_FILE.search(delivery_location) and not _looks_like_street_address(delivery_location):
            _set_delivery(draft)
        else:
            found = await _resolve_address(employee["email"], delivery_location, delivery_label, delivery_kind,
                                           draft.get("saved_addresses"))
            if "error" in found:
                _save(tool_context, draft)
                return {"status": "need_address", "message": found["error"]}
            _set_delivery(draft, **found)
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def choose_ship_to(address: str, tool_context: ToolContext) -> dict:
    """The user picked where to ship from the review card: a saved address, or "" for the
    address on file in ServiceNow.

    Args:
        address: The saved address from the button, or "" for the address on file.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if not address:
        _set_delivery(draft)
    else:
        match = _match_saved(address, draft.get("saved_addresses") or await memory.saved_addresses(employee["email"]))
        if not match:
            return {"status": "not_found", "message": "That saved address wasn't found. Ask for the full address."}
        _set_delivery(draft, match["address"], match.get("label", ""), "permanent", saved=True)
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def show_review(tool_context: ToolContext) -> dict:
    """Shows the current step again: the review card if everything is filled in."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    return await _next_step(tool_context, _draft(tool_context), employee)


@servicenow_errors
async def submit_ticket(tool_context: ToolContext) -> dict:
    """Files the request with the service desk. Only call after the user clicked
    "Submit request" or clearly said to submit on the review step."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if draft.get("submitted_number"):
        # A double click, or "submit" said twice: never file a duplicate.
        _show(tool_context, cards.confirmation(draft["submitted_number"], draft))
        return {"status": "already_submitted", "ticket": draft["submitted_number"]}
    if not draft.get("device") or not draft.get("issue"):
        return await _next_step(tool_context, draft, employee)
    draft = _refresh(draft, employee)
    need, _ = PROFILE.photo_policy(draft["issue"]["category"])
    if need == "required" and not draft.get("evidence"):
        return await _next_step(tool_context, draft, employee)

    issue, device = draft["issue"], draft["device"]
    session_id = tool_context.session.id if tool_context.session else ""
    # One per request, not per conversation: a retried turn (e.g. the reply
    # failed after filing) must not file twice, but a second request must file.
    conversation_id = f"{session_id}:{draft['id']}" if session_id else ""
    existing = await servicenow.find_open_by_correlation(employee["sys_id"], conversation_id) if conversation_id else None
    if existing:
        ticket = existing
    else:
        impact, urgency = servicenow.URGENCY_TO_IMPACT_URGENCY[_urgency_key(draft)]
        where = device.get("location") or device.get("department")
        # The organization's extra fields first (config/organization.yaml servicenow.ticket_fields);
        # what the agent sets itself below always wins.
        configured = _configured_fields(draft)
        fields = {
            **configured,
            "caller_id": employee["sys_id"],
            "category": servicenow.HARDWARE,
            "impact": impact,
            "urgency": urgency,
            "short_description": (f"{ISSUE_LABELS.get(issue['category'])}: {device.get('model') or 'device'} "
                                  f"{device.get('asset_tag', '')}"
                                  + (f" - {where}" if cards.is_equipment(device) and where else "")).strip(),
            "description": _ticket_description(draft, employee),
            "correlation_id": conversation_id,
            "correlation_display": "Gemini Enterprise - Hardware Replacement agent",
        }
        if device.get("ci"):
            fields["cmdb_ci"] = device["ci"]
        if device.get("support_group_id"):
            fields["assignment_group"] = device["support_group_id"]  # e.g. Clinical Engineering
        if device.get("location_id"):
            fields["location"] = device["location_id"]
        # Whoever the device belongs to hears about it too (and sees it in their ticket list).
        watchers = [w for w in (device.get("assigned_to"), device.get("managed_by")) if w and w != employee["sys_id"]]
        if watchers:
            fields["watch_list"] = ",".join(dict.fromkeys(watchers))
        ticket = await servicenow.create_incident(fields)
        # Filed: record it before anything else can fail, so a retry or a double click never
        # files twice and the user is never told it failed.
        draft["submitted_number"], draft["submitted_url"] = ticket["number"], ticket["url"]
        _save(tool_context, draft)
        try:
            await _after_filing(tool_context, employee, draft, ticket, fields, configured)
        except Exception as exc:  # noqa: BLE001  (notes are best-effort; the ticket exists)
            logger.warning("ticket %s filed; follow-up notes incomplete: %s", ticket["number"], type(exc).__name__)

    draft["assigned_priority"] = servicenow.PRIORITY_LABELS.get(ticket["priority"], ticket["priority"])
    draft["priority_note"] = "" if draft["assigned_priority"] == draft["priority"] else (
        f"You asked for {draft['priority']} handling. Per policy ServiceNow set this ticket to "
        f"{draft['assigned_priority']}; your request and reason were noted on the ticket for the service desk.")

    draft["submitted_number"] = ticket["number"]
    draft["submitted_url"] = ticket["url"]
    _save(tool_context, draft)
    delivery = draft.get("delivery") or {}
    if delivery.get("kind") == "permanent" and not delivery.get("saved"):
        await memory.save_address(employee["email"], delivery.get("label", ""), delivery["address"])
    await memory.remember_conversation(tool_context, employee["email"])
    _show(tool_context, cards.confirmation(ticket["number"], draft))
    return {"status": "submitted", "ticket": ticket["number"], "priority": draft["assigned_priority"],
            "requested_priority": draft["priority"], "sla": draft["sla"]}


async def _after_filing(tool_context: ToolContext, employee: dict, draft: dict, ticket: dict, fields: dict,
                        configured: dict) -> None:
    """Notes and photos added once a ticket exists: details ServiceNow dropped, what the desk must
    set, the requested priority. Best-effort: submit_ticket has already recorded the ticket."""
    issue, device = draft["issue"], draft["device"]
    refused = await servicenow.dropped_fields(ticket["sys_id"], {k: fields[k] for k in configured if k in fields})
    if not ticket["description"].strip():
        # ServiceNow drops fields the caller isn't allowed to set (e.g. without
        # the itil role). Comments are always allowed, so the details go there.
        await servicenow.update_incident(employee["sys_id"], ticket["number"], {
            "comments": "Request details (added by the Hardware Replacement agent):\n\n" + fields["description"]})
        logger.info("description not accepted by ServiceNow; details added as a note")
    desk_notes = [f"Could not set {k} = {v!r} from the reporter's account; please set it." for k, v in refused.items()]
    if device.get("relation") == "unconfirmed":
        desk_notes.append(f"{device.get('ownership_note')}. Filed anyway, as the agent allows anyone to report equipment.")
    if fields.get("assignment_group") and ticket.get("assignment_group_id") != fields["assignment_group"]:
        desk_notes.append(f"Please route to {device.get('support_group')}, the equipment's support group; "
                          "it could not be set from the reporter's account.")
    if desk_notes:
        await servicenow.update_incident(employee["sys_id"], ticket["number"], {"comments": "\n".join(desk_notes)})
    await _attach_photos(tool_context, ticket["sys_id"], draft)
    requested = draft["priority"].split(" ")[0]
    if ticket["priority"] and ticket["priority"] != requested:
        # ServiceNow derives priority itself and may drop impact/urgency the
        # caller isn't allowed to set; record what was asked for, and why.
        await servicenow.update_incident(employee["sys_id"], ticket["number"], {"comments": (
            f"Requested priority {draft['priority']} ({issue.get('description', '')}); "
            f"ServiceNow assigned priority {ticket['priority']}. Please review.")})
        logger.info("priority %s requested, ServiceNow assigned %s", requested, ticket["priority"])


def _configured_fields(draft: dict) -> dict[str, str]:
    """servicenow.ticket_fields from the profile, filled in for this request."""
    issue, device = draft.get("issue") or {}, draft.get("device") or {}
    chosen = PROFILE.issue(issue.get("category", ""))
    settings = PROFILE.servicenow
    return settings.render_fields({
        "issue_key": issue.get("category", ""),
        "issue_value": (chosen.servicenow_value or chosen.key) if chosen else issue.get("category", ""),
        "issue_label": ISSUE_LABELS.get(issue.get("category", ""), ""),
        "device_value": settings.device_value(device.get("device_type", ""), device.get("category", "")),
        "device_type": device.get("device_type", ""),
        "device_kind": device.get("kind", ""),
        "model_category": device.get("category", ""),
        "department": device.get("department", ""),
        "location": device.get("location", ""),
    })


def _urgency_key(draft: dict) -> str:
    for key, label in _PRIORITY.items():
        if label == draft.get("priority"):
            return key
    return "normal"


def _ticket_description(draft: dict, employee: dict) -> str:
    """Everything the service desk needs, in the ticket body."""
    issue, device, elig = draft["issue"], draft["device"], draft.get("eligibility") or {}
    evidence = draft.get("evidence") or {}
    lines = [
        f"Problem: {ISSUE_LABELS.get(issue['category'])}",
        f"Details: {issue.get('description', '')}",
        "",
        f"Device: {cards.maker_model(device)}",
        f"Asset tag: {device.get('asset_tag') or 'unknown'}   Serial: {device.get('serial_number') or 'unknown'}",
    ]
    if cards.is_equipment(device):
        lines += [
            f"Location: {device.get('location') or 'unknown'}",
            f"Department: {device.get('department') or 'unknown'}",
            f"Ownership: {device.get('ownership_note')}",
            "",
            f"Service: {draft.get('recommendation')}",
            f"Support group: {device.get('support_group') or 'not set on the asset'}",
            f"Target: {draft.get('sla')}",
            f"Bill to: {device.get('cost_center') or device.get('department') or 'n/a'}",
        ]
    else:
        lines += [f"Coverage: {elig.get('summary', 'unknown')}"]
        if device.get("relation") != "yours":
            lines.append(f"Ownership: {device.get('ownership_note')}")
        lines += [
            "",
            f"Recommended fulfilment: {draft.get('recommendation')}",
            f"Target: {draft.get('sla')}",
            f"Ship to: {draft.get('delivery_location') or employee.get('location_address') or employee.get('location')}",
            f"Bill to: {employee.get('cost_center') or 'n/a'} ({employee.get('department') or 'n/a'})",
        ]
    lines += ["", f"Reported by: {employee.get('name', '')} ({employee.get('email', '')}), "
                  f"{employee.get('title') or 'no title'}, {employee.get('department') or 'no department'}"]
    if PROFILE.is_safety(issue["category"]):
        lines += ["", f"SAFETY CONCERN. The reporter was told: {cards.SAFETY_TEXT}"]
    if evidence:
        lines += ["", f"Photo evidence: {evidence.get('summary')}"]
    if draft.get("warnings"):
        lines += ["", "Notes for the service desk:"] + [f"- {w}" for w in draft["warnings"]]
    lines += ["", "Filed by the Hardware Replacement agent in Gemini Enterprise, as the signed-in user."]
    return "\n".join(lines)


async def _attach_photos(ctx: ToolContext, incident_sys_id: str, draft: dict) -> None:
    """Copies the conversation's photos onto the incident. A failed copy never
    fails the ticket; the photo evidence is also in the description."""
    for photo in draft.get("photos") or []:
        if not photo.get("filename"):
            continue
        try:
            part = await ctx.load_artifact(photo["filename"])
            if part and part.inline_data and part.inline_data.data:
                await servicenow.attach(incident_sys_id, photo["filename"], part.inline_data.data,
                                        part.inline_data.mime_type or photo.get("mime_type", "image/jpeg"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not attach %s: %s", photo["filename"], type(exc).__name__)

# --- existing tickets ---------------------------------------------------------------
# Every call is filtered in servicenow.py to the caller's own hardware incidents,
# and runs with their token, so ServiceNow's own access rules also apply. A
# change ServiceNow doesn't allow (e.g. priority without the itil role) is
# checked after the fact and reported honestly, never shown as done.


async def _show_ticket(ctx: ToolContext, ticket: dict, note: str = "", view: str = "change") -> dict:
    """Stages the ticket card for the requested view; notes are fetched only when shown."""
    needs_notes = view in ("last_note", "notes", "details")
    entries = await servicenow.notes(ticket["sys_id"]) if needs_notes else []
    _show(ctx, cards.ticket_detail(ticket, entries, note, view))
    result = {"number": ticket["number"], "short_description": ticket["short_description"], "state": ticket["state"],
              "priority": servicenow.PRIORITY_LABELS.get(ticket["priority"], ticket["priority"]),
              "assigned_to": " / ".join(filter(None, [ticket["assignment_group"], ticket["assigned_to"]])) or "unassigned",
              "opened": ticket["opened"], "updated": ticket["updated"], "shown": view}
    if view == "last_note":
        result["latest_note"] = entries[0] if entries else None
    elif view == "notes":
        result["notes"] = entries
    elif view == "details":
        result["description"] = ticket["description"]
        result["notes"] = entries
    return result


async def _own_ticket(ctx: ToolContext, number: str) -> tuple[dict | None, dict | None]:
    """(employee, ticket), or an error result in place of the ticket."""
    employee = await _employee(ctx)
    if not employee:
        return None, _no_identity(ctx)
    ticket = await servicenow.my_incident(employee["sys_id"], number)
    if not ticket:
        return None, {"status": "not_found", "message": f"{number} is not one of this user's hardware tickets."}
    return employee, ticket


@servicenow_errors
async def list_my_tickets(tool_context: ToolContext, include_closed: bool = False) -> dict:
    """Shows the user's hardware tickets, newest first.

    Args:
        include_closed: True to include resolved, closed and canceled tickets.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    tickets = await servicenow.my_incidents(employee["sys_id"], active_only=not include_closed)
    for t in tickets:
        t["following"] = bool(t.get("caller_id")) and t["caller_id"] != employee["sys_id"]
    _show(tool_context, cards.ticket_list(tickets, include_closed))
    return {"status": "ok", "count": len(tickets),
            "tickets": [{k: t[k] for k in ("number", "short_description", "state", "priority")} for t in tickets]}


@servicenow_errors
async def get_ticket(number: str, tool_context: ToolContext, show: str = "status") -> dict:
    """Shows one of the user's hardware tickets. The card always has the current status,
    priority, dates and assignee, plus only what was asked for.

    Args:
        number: The incident number, e.g. "INC0010023".
        show: What to add below the status. "status" for status questions ("has it been
            assigned?", "any update?"); "last_note" for the latest note or update;
            "notes" for all notes; "details" only when they ask for all details or the full
            ticket (description with device, ship-to, bill-to and photo evidence, plus notes).
    """
    if show not in ("status", "last_note", "notes", "details"):
        show = "status"
    employee, ticket = await _own_ticket(tool_context, number)
    if not employee:
        return ticket
    return {"status": "ok", "ticket": await _show_ticket(tool_context, ticket, view=show)}


def replace_ship_to(description: str, address: str) -> str | None:
    """The description with its "Ship to:" line replaced, or None if it has none."""
    lines = description.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Ship to:"):
            lines[i] = f"Ship to: {address}"
            return "\n".join(lines)
    return None


def _change(label: str, requested: str, fields: dict, verify) -> dict:
    return {"label": label, "requested": requested, "fields": fields, "verify": verify}


# Statuses update_ticket may set. Cancel has its own tool (confirmation, reporter only);
# closing is the service desk's.
_USER_STATUSES = ("new", "in progress", "on hold", "resolved")


def _state_change(target: str, reason: str) -> dict | None:
    code = servicenow.STATE_CODES.get(target.strip().lower())
    if not code:
        return None
    fields = {"state": code}
    if code in ("6", "7", "8"):  # resolving/closing/canceling needs a close code and notes
        fields.update({"close_code": "Solved Remotely (Permanently)", "close_notes": reason or "Requested by the user"})
    if code == "3":
        fields["hold_reason"] = "1"  # awaiting caller
    return _change("Status", servicenow.STATE_LABELS[code], fields, lambda t, c=code: t["state_code"] == c)


def _urgency_change(urgency: str) -> dict | None:
    if urgency not in servicenow.URGENCY_TO_IMPACT_URGENCY:
        return None
    impact, level = servicenow.URGENCY_TO_IMPACT_URGENCY[urgency]
    return _change("Urgency", urgency, {"impact": impact, "urgency": level}, lambda t, u=level: t["urgency"] == u)


async def _follower_request(ctx: ToolContext, employee: dict, ticket: dict, changes: list[dict],
                            note: str) -> dict:
    """A follower asked to change someone else's ticket: only the reporter can change it, so the
    request goes on the ticket as a note for the service desk, and nothing else is attempted."""
    reporter = ticket.get("caller") or "the person who reported it"
    lines = "\n".join(f"- {ch['label']}: {ch['requested']}" for ch in changes)
    comment = (f"{employee.get('name') or 'A follower'}, who follows this ticket, asked through the Hardware "
               f"Replacement agent for the following. Please review:\n{lines}")
    if note.strip():
        comment = f"{note.strip()}\n\n{comment}"
    updated = await servicenow.update_incident(employee["sys_id"], ticket["number"], {"comments": comment})
    asked = "; ".join(f"{c['label'].lower()} to {c['requested']}" for c in changes)
    message = (f"Only {reporter} can change this ticket, so nothing was changed. Your request ({asked}) was "
               "added to the ticket as a note for the service desk.")
    result = {"status": "not_permitted", "reason": "follower", "note_added": True, "changed": [],
              "requested_in_note": [{"change": c["label"], "value": c["requested"]} for c in changes]}
    result["ticket"] = await _show_ticket(ctx, updated or ticket, message)
    return result


async def _apply_changes(ctx: ToolContext, number: str, changes: list[dict], note: str = "") -> dict:
    """The one path every ticket change takes.

    1. Attempt all requested field changes (plus the user's note) in one update.
    2. Read back what ServiceNow actually stored and verify each change.
    3. For anything ServiceNow didn't apply (policy/permissions), add a note to
       the ticket with exactly what the user asked for, so the service desk can do it.
    4. Report per change, so the user is told plainly what was and wasn't done.
    """
    employee, ticket = await _own_ticket(ctx, number)
    if not employee:
        return ticket
    if changes and ticket.get("caller_id") and ticket["caller_id"] != employee["sys_id"]:
        return await _follower_request(ctx, employee, ticket, changes, note)
    fields: dict = {}
    for ch in changes:
        fields.update(ch["fields"](ticket) if callable(ch["fields"]) else ch["fields"])
    if note.strip():
        fields["comments"] = note.strip()
    try:
        updated = await servicenow.update_incident(employee["sys_id"], number, fields) if fields else ticket
    except servicenow.ServiceNowError as exc:
        # The whole update was refused; keep the user's note at least.
        logger.info("update refused (%s); retrying with the note only", exc)
        updated = (await servicenow.update_incident(employee["sys_id"], number, {"comments": note.strip()})
                   if note.strip() else ticket)

    applied, not_applied = [], []
    for ch in changes:
        (applied if ch["verify"](updated) else not_applied).append(ch)
    if not_applied:
        lines = "\n".join(f"- {ch['label']}: {ch['requested']}" for ch in not_applied)
        updated = await servicenow.update_incident(employee["sys_id"], number, {"comments": (
            "The requester asked for the following through the Hardware Replacement agent, but ServiceNow "
            f"policy did not allow the change from their account. Please review and apply:\n{lines}")})

    parts = []
    if note.strip():
        parts.append("Your note was added.")
    if applied:
        parts.append("Done: " + "; ".join(f"{c['label'].lower()} set to {c['requested']}" for c in applied) + ".")
    if not_applied:
        parts.append("Not changed due to ServiceNow policy: "
                     + "; ".join(f"{c['label'].lower()} to {c['requested']}" for c in not_applied)
                     + ". A note asking the service desk to make this change was added to the ticket.")
    result = {
        "status": "ok",
        "note_added": bool(note.strip()),
        "changed": [{"change": c["label"], "value": c["requested"]} for c in applied],
        "not_permitted_note_added": [{"change": c["label"], "value": c["requested"]} for c in not_applied],
    }
    result["ticket"] = await _show_ticket(ctx, updated, " ".join(parts))
    return result


@servicenow_errors
async def update_ticket(number: str, tool_context: ToolContext, note: str = "", status: str = "",
                        urgency: str = "", ship_to: str = "") -> dict:
    """Makes any change the user asks for on one of their hardware tickets, in one step.
    Pass only what they asked to change. Every change is attempted and verified; anything
    ServiceNow doesn't allow is added to the ticket as a note for the service desk.

    Args:
        number: The incident number.
        note: Text to add as a note, in the user's words (e.g. "the tracking number doesn't work").
        status: New status: New, In Progress, On Hold or Resolved (e.g. to reopen a resolved
            ticket, use In Progress). To cancel, use cancel_ticket.
        urgency: low, normal, high or critical.
        ship_to: The new shipping address: a full street address, or the user's words for a
            saved place ("my house"), which is looked up.
    """
    changes = []
    if ship_to:
        employee = await _employee(tool_context)
        found = await _resolve_address((employee or {}).get("email", ""), ship_to)
        if "error" in found:
            return {"status": "need_address", "message": found["error"]}
        ship_to = found["address"]
    if status:
        ch = _state_change(status, note) if status.strip().lower() in _USER_STATUSES else None
        if not ch:
            return {"status": "error", "message": f"Unknown status {status!r}. Use New, In Progress, On Hold or Resolved."}
        changes.append(ch)
    if urgency:
        ch = _urgency_change(urgency)
        if not ch:
            return {"status": "error", "message": "Urgency must be low, normal, high or critical."}
        changes.append(ch)
    if ship_to:
        address = " ".join(ship_to.split())

        def ship_fields(ticket, a=address):
            new = replace_ship_to(ticket["description"], a)
            return {"description": new} if new else {}
        changes.append(_change("Ship-to address", address, ship_fields,
                               lambda t, a=address: f"Ship to: {a}" in t["description"]))
        note = (note + "\n" if note else "") + f"Shipping address changed by the requester to: {address}"
    if not changes and not note.strip():
        return {"status": "error", "message": "Nothing to change was given."}
    return await _apply_changes(tool_context, number, changes, note)


@servicenow_errors
async def add_ticket_note(number: str, note: str, tool_context: ToolContext) -> dict:
    """Adds a note the service desk will see. If the user also asks for a change (status,
    urgency, address), use update_ticket instead so the change is attempted too.

    Args:
        number: The incident number.
        note: The note, in the user's words.
    """
    return await _apply_changes(tool_context, number, [], note)


@servicenow_errors
async def change_ticket_shipping(number: str, address: str, tool_context: ToolContext) -> dict:
    """Changes where the replacement for a submitted ticket should be shipped.

    Args:
        number: The incident number.
        address: The full street address, or the user's words for a saved place ("my house").
    """
    return await update_ticket(number, tool_context, ship_to=address)


@servicenow_errors
async def request_urgent_handling(number: str, reason: str, tool_context: ToolContext) -> dict:
    """Asks for a ticket to be handled urgently (raises urgency to high).

    Args:
        number: The incident number.
        reason: Why it's urgent, in the user's words.
    """
    return await update_ticket(number, tool_context, urgency="high",
                               note=f"Requester asked for urgent handling: {reason.strip()}")


@servicenow_errors
async def cancel_ticket(number: str, reason: str, tool_context: ToolContext) -> dict:
    """Cancels one of the user's open hardware tickets, with the reason added to its notes.
    Only call after the user confirmed. Tickets are never deleted, only canceled.

    Args:
        number: The incident number.
        reason: Why, in the user's words.
    """
    employee, ticket = await _own_ticket(tool_context, number)
    if not employee:
        return ticket
    if ticket["state_code"] not in servicenow.ACTIVE_STATES:
        return {"status": "error", "message": f"{number} is already {ticket['state'].lower()}."}
    if ticket.get("caller_id") and ticket["caller_id"] != employee["sys_id"]:
        return {"status": "not_permitted", "message": (
            f"Only the person who reported {number} ({ticket.get('caller') or 'someone else'}) can cancel it. "
            "The user follows it. Offer to add a note instead, e.g. that it is working again.")}
    reason = f"Canceled by the requester. Reason: {reason.strip()}"
    return await _apply_changes(tool_context, number, [_state_change("canceled", reason)], reason)


@servicenow_errors
async def follow_ticket(number: str, tool_context: ToolContext) -> dict:
    """Adds the user's report to an open ticket someone already filed for the same
    equipment, and makes them a follower so it appears in their tickets and they get updates.
    Use when they chose "Add my note" on the "already reported" card.

    Args:
        number: The open ticket's number.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    device = draft.get("device") or {}
    ticket = next((t for t in await servicenow.open_incidents_for_ci(device.get("ci", ""), limit=10)
                   if t["number"] == number.strip().upper()), None)
    if not ticket:
        return {"status": "not_found", "message": f"{number} is no longer open for this device. Offer to report it as new."}
    issue = draft.get("issue") or {}
    note = " ".join(filter(None, [
        f"Also reported by {employee.get('name')} ({employee.get('department') or 'no department'}).",
        f"{ISSUE_LABELS.get(issue.get('category'), '')}: {issue.get('description', '')}" if issue else ""]))
    saved = await servicenow.follow_incident(employee["sys_id"], ticket["sys_id"], note)
    following = employee["sys_id"] in saved.get("watch_list", [])
    draft["submitted_number"], draft["followed"] = saved["number"], True
    _save(tool_context, draft)
    message = ("Your note was added and you're now following this ticket, so it shows in your tickets."
               if following else "Your note was added. ServiceNow didn't let your account follow the ticket, "
               "so ask about it by its number.")
    result = await _show_ticket(tool_context, saved, message, "change")
    return result | {"status": "ok", "following": following, "note_added": note}


@servicenow_errors
async def report_separately(tool_context: ToolContext) -> dict:
    """The user wants their own ticket even though the equipment already has an open one."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    draft["duplicates_checked"] = True
    return await _next_step(tool_context, draft, employee)


# set_issue's argument docs list this organization's problem choices (config/organization.yaml).
_safety_keys = [i.key for i in PROFILE.issues.equipment + PROFILE.issues.personal if i.safety]
set_issue.__doc__ = (set_issue.__doc__
                     .replace("{PERSONAL_ISSUES}", ", ".join(PROFILE.keys("personal")))
                     .replace("{EQUIPMENT_ISSUES}", ", ".join(PROFILE.keys("equipment")))
                     .replace("{SAFETY_RULE}", f"Use {' or '.join(dict.fromkeys(_safety_keys))} whenever a person is, "
                              "or could be, put at risk." if _safety_keys else ""))

ALL_TOOLS = [start_request, select_device, find_device, confirm_device, request_label_photo, set_issue,
             analyze_photos, skip_photo, update_request, choose_ship_to, show_review, submit_ticket, follow_ticket, report_separately,
             list_my_tickets, get_ticket, update_ticket, add_ticket_note, change_ticket_shipping,
             request_urgent_handling, cancel_ticket]
