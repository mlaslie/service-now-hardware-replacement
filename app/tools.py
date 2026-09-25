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

from google.adk.tools import ToolContext

from app import cards, memory, servicenow, vision
from app.cards import ISSUE_LABELS

logger = logging.getLogger(__name__)

CARD_KEY = "temp:card"

# category -> (photo need, what to photograph)
PHOTO_POLICY = {
    "cracked_screen": ("required", "the screen, switched on if possible, so the cracks are visible"),
    "physical_damage": ("required", "the damaged area, close enough to see it clearly"),
    "liquid_damage": ("recommended", "where the liquid got in, plus any stains or corrosion"),
    "battery": ("recommended", "the underside, and the trackpad if it looks lifted or the case bulges"),
    "keyboard_trackpad": ("recommended", "the keyboard, showing the broken or missing keys"),
    "other": ("optional", "whatever shows the problem"),
    "wont_power_on": (None, ""),
    "performance": (None, ""),
}

# Matches the priority ServiceNow derives from servicenow.URGENCY_TO_IMPACT_URGENCY.
_PRIORITY = {"low": "4 - Low", "normal": "3 - Moderate", "high": "2 - High", "critical": "1 - Critical"}
REFRESH_YEARS = {"laptop": 3, "desktop": 4, "monitor": 5, "phone": 2, "tablet": 3}
# A device that cannot be used at all blocks the employee's work.
_BLOCKING = {"wont_power_on", "liquid_damage", "cracked_screen"}
_REPLACE = {"cracked_screen", "liquid_damage", "physical_damage", "wont_power_on"}


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


def _draft(ctx: ToolContext) -> dict:
    return dict(ctx.state.get("draft") or {})


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
        out["refresh_eligible"] = age >= REFRESH_YEARS.get(asset.get("device_type", ""), 4)
    if out["refresh_eligible"]:
        out["summary"] = f"Refresh eligible ({out['age_years']} yrs old)"
    elif out["in_warranty"]:
        out["summary"] = f"Under warranty until {warranty_end}"
    elif warranty_end:
        out["summary"] = f"Out of warranty since {warranty_end}"
    return out


def _asset_summary(assets: list[dict]) -> list[dict]:
    return [{"asset_tag": a["asset_tag"], "type": a.get("device_type"), "model": a.get("model")} for a in assets]


def _device_from_asset(asset: dict) -> dict:
    keys = ("sys_id", "asset_tag", "serial_number", "manufacturer", "model",
            "device_type", "purchase_date", "warranty_end", "assigned_to", "ci")
    return {k: asset.get(k, "") for k in keys} | {"in_inventory": True}


def _refresh(draft: dict, employee: dict) -> dict:
    """Derives priority, recommendation, SLA and warnings from what is known."""
    issue = draft.get("issue") or {}
    category = issue.get("category", "")
    elig = draft.get("eligibility") or {}
    evidence = draft.get("evidence") or {}

    urgency = issue.get("urgency", "normal")
    if category in _BLOCKING and urgency in ("low", "normal"):
        urgency = "high"
    draft["priority"] = _PRIORITY.get(urgency, _PRIORITY["normal"])

    if elig.get("refresh_eligible"):
        rec = "Replace with current standard model (refresh eligible, no cost to department)"
    elif category in _REPLACE or evidence.get("supports_replacement"):
        rec = ("Warranty replacement" if elig.get("in_warranty")
               else f"Replacement, charged to {employee.get('cost_center', 'department')}")
    elif category == "performance":
        rec = "Remote diagnostics first; replace if unresolved"
    elif elig.get("in_warranty"):
        rec = "Warranty repair with a loaner device"
    else:
        rec = "Repair assessment; replace if repair is uneconomical"
    draft["recommendation"] = rec
    draft["sla"] = ("Next business day" if draft["priority"].startswith(("1", "2"))
                    else "2-3 business days")

    warnings = list(draft.get("photo_warnings") or [])
    device = draft.get("device") or {}
    assigned = device.get("assigned_to")
    if assigned and assigned != employee.get("sys_id"):
        warnings.append("Inventory shows this device assigned to someone else; the desk will confirm ownership.")
    if device and not device.get("in_inventory"):
        warnings.append("Device not found in inventory; details were read from your photo.")
    mismatch = _photo_mismatch(draft)
    if mismatch:
        warnings.append(mismatch)
    need, _ = PHOTO_POLICY.get(category, (None, ""))
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
    _save(ctx, draft)
    _show(ctx, cards.review(draft, employee))
    return {"status": "ok", "step": "review", "shown": "review card with Submit and Change buttons",
            "priority": draft["priority"], "recommendation": draft["recommendation"],
            "warnings": draft["warnings"]}


async def _next_step(ctx: ToolContext, draft: dict, employee: dict) -> dict:
    """Shows whichever step is still missing, so the wizard skips what is known."""
    _save(ctx, draft)
    device, issue = draft.get("device"), draft.get("issue")
    if not device:
        assets = await servicenow.my_assets(employee["sys_id"])
        _show(ctx, cards.device_picker(employee, assets))
        return {"status": "ok", "step": "choose_device", "assets": _asset_summary(assets)}
    if not issue:
        _show(ctx, cards.issue_picker(device, (draft.get("evidence") or {}).get("category", "")))
        return {"status": "ok", "step": "describe_issue", "device": device.get("model")}
    need, what = PHOTO_POLICY.get(issue["category"], ("optional", "the problem"))
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
    draft: dict = {}
    _save(tool_context, draft)
    tool_context.state["last_photo_ids"] = []
    result = await _next_step(tool_context, draft, employee)
    remembered = await memory.recall(tool_context, employee["email"])
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
    if not asset:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": f"No asset {asset_tag} in inventory; asked the user for a label photo."}
    draft = _draft(tool_context)
    draft["device"] = _device_from_asset(asset)
    draft["eligibility"] = eligibility(asset)
    return await _next_step(tool_context, draft, employee)


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
        category: One of cracked_screen, wont_power_on, battery, keyboard_trackpad,
            liquid_damage, physical_damage, performance, other.
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
    draft = _draft(tool_context)
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

    draft = _draft(tool_context)
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
    for photo, f in findings:
        draft["photos"].append({"uri": photo["uri"], "photo_id": photo["photo_id"], "filename": photo.get("filename", ""),
                                "mime_type": photo["mime_type"], "findings": f})

        # Label data -> inventory match.
        if f["asset_tag"] or f["serial_number"]:
            asset = await servicenow.find_asset(f["asset_tag"], f["serial_number"])
            current = draft.get("device") or {}
            if asset:
                if current and current.get("asset_tag") != asset["asset_tag"]:
                    photo_warnings.append(
                        f"The photo shows asset {asset['asset_tag']}, not the selected {current.get('asset_tag')}.")
                elif not current:
                    draft["device"] = _device_from_asset(asset)
                    draft["eligibility"] = eligibility(asset)
                photo_serial = f["serial_number"].upper().replace(" ", "")
                if photo_serial and asset.get("serial_number") and photo_serial != asset["serial_number"].upper():
                    photo_warnings.append(
                        f"Serial on the label ({photo_serial}) differs from inventory ({asset['serial_number']}).")
            elif not current:
                draft["device"] = {
                    "asset_tag": normalize_tag(f["asset_tag"]), "serial_number": f["serial_number"],
                    "manufacturer": f["manufacturer"], "model": f["model"], "part_number": f["part_number"],
                    "device_type": f["device_type"], "in_inventory": False,
                }
        elif not draft.get("device") and f["image_kind"] in ("label", "device"):
            note = "I couldn't read an asset tag or serial number. Try a closer, sharper photo of the label."

        # Damage -> evidence.
        if f["damage_present"]:
            draft["evidence"] = {
                "summary": f"{f['damage_description']} (severity: {f['damage_severity']})",
                "severity": f["damage_severity"], "category": f["issue_category"],
                "supports_replacement": f["supports_replacement"], "photo_uri": photo["uri"],
            }
        elif draft.get("issue") and PHOTO_POLICY.get(draft["issue"]["category"], (None,))[0] == "required" \
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
                         category: str = "", delivery_location: str = "") -> dict:
    """Changes details on the request, then shows the review card again.

    Args:
        description: New problem description, if the user changed it.
        urgency: low, normal, high or critical, if the user changed it.
        category: New issue category, if the user changed it.
        delivery_location: Where the replacement should go, if not their usual office.
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
        draft["delivery_location"] = delivery_location.strip()
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
    need, _ = PHOTO_POLICY.get(draft["issue"]["category"], (None, ""))
    if need == "required" and not draft.get("evidence"):
        return await _next_step(tool_context, draft, employee)

    issue, device = draft["issue"], draft["device"]
    conversation_id = tool_context.session.id if tool_context.session else ""
    # A retried turn (e.g. the reply failed after filing) must not file twice.
    existing = await servicenow.find_open_by_correlation(employee["sys_id"], conversation_id) if conversation_id else None
    if existing:
        ticket = existing
    else:
        impact, urgency = servicenow.URGENCY_TO_IMPACT_URGENCY[_urgency_key(draft)]
        fields = {
            "caller_id": employee["sys_id"],
            "category": servicenow.HARDWARE,
            "contact_type": "self-service",
            "impact": impact,
            "urgency": urgency,
            "short_description": (f"{ISSUE_LABELS.get(issue['category'])}: {device.get('model') or 'device'} "
                                  f"{device.get('asset_tag', '')}").strip(),
            "description": _ticket_description(draft, employee),
            "correlation_id": conversation_id,
            "correlation_display": "Gemini Enterprise - Hardware Replacement agent",
        }
        if device.get("ci"):
            fields["cmdb_ci"] = device["ci"]
        ticket = await servicenow.create_incident(fields)
        if not ticket["description"].strip():
            # ServiceNow drops fields the caller isn't allowed to set (e.g. without
            # the itil role). Comments are always allowed, so the details go there.
            await servicenow.update_incident(employee["sys_id"], ticket["number"], {
                "comments": "Request details (added by the Hardware Replacement agent):\n\n" + fields["description"]})
            logger.info("description not accepted by ServiceNow; details added as a note")
        await _attach_photos(tool_context, ticket["sys_id"], draft)
        requested = draft["priority"].split(" ")[0]
        if ticket["priority"] and ticket["priority"] != requested:
            # ServiceNow derives priority itself and may drop impact/urgency the
            # caller isn't allowed to set; record what was asked for, and why.
            await servicenow.update_incident(employee["sys_id"], ticket["number"], {"comments": (
                f"Requested priority {draft['priority']} ({issue.get('description', '')}); "
                f"ServiceNow assigned priority {ticket['priority']}. Please review.")})
            logger.info("priority %s requested, ServiceNow assigned %s", requested, ticket["priority"])

    draft["assigned_priority"] = servicenow.PRIORITY_LABELS.get(ticket["priority"], ticket["priority"])
    draft["priority_note"] = "" if draft["assigned_priority"] == draft["priority"] else (
        f"You asked for {draft['priority']} handling. Per policy ServiceNow set this ticket to "
        f"{draft['assigned_priority']}; your request and reason were noted on the ticket for the service desk.")

    draft["submitted_number"] = ticket["number"]
    draft["submitted_url"] = ticket["url"]
    _save(tool_context, draft)
    await memory.remember_conversation(tool_context, employee["email"])
    _show(tool_context, cards.confirmation(ticket["number"], draft))
    return {"status": "submitted", "ticket": ticket["number"], "priority": draft["assigned_priority"],
            "requested_priority": draft["priority"], "sla": draft["sla"]}


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
        f"Device: {' '.join(filter(None, [device.get('manufacturer'), device.get('model')]))}",
        f"Asset tag: {device.get('asset_tag') or 'unknown'}   Serial: {device.get('serial_number') or 'unknown'}",
        f"Coverage: {elig.get('summary', 'unknown')}",
        "",
        f"Recommended fulfilment: {draft.get('recommendation')}",
        f"Target: {draft.get('sla')}",
        f"Ship to: {draft.get('delivery_location') or employee.get('location_address') or employee.get('location')}",
        f"Bill to: {employee.get('cost_center') or 'n/a'} ({employee.get('department') or 'n/a'})",
    ]
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
        ship_to: A new full shipping address, accepted as typed.
    """
    changes = []
    if status:
        ch = _state_change(status, note)
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
        address: The full new shipping address, accepted as typed.
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
    reason = f"Canceled by the requester. Reason: {reason.strip()}"
    return await _apply_changes(tool_context, number, [_state_change("canceled", reason)], reason)


ALL_TOOLS = [start_request, select_device, request_label_photo, set_issue, analyze_photos,
             skip_photo, update_request, show_review, submit_ticket,
             list_my_tickets, get_ticket, update_ticket, add_ticket_note, change_ticket_shipping,
             request_urgent_handling, cancel_ticket]
