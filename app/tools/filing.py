"""Filing the request: exactly one ServiceNow ticket per request, then best-effort notes,
photos and the requested priority.
"""

import asyncio
import json
import logging

from google.adk.tools import ToolContext

from app import cards, memory, servicenow
from app.cards import ISSUE_LABELS
from app.tools._common import PROFILE, _PRIORITY, _draft, _employee, _no_identity, _save, _show, servicenow_errors
from app.tools.review import _next_step, _refresh

logger = logging.getLogger(__name__)

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

    session_id = tool_context.session.id if tool_context.session else ""
    # One per request, not per conversation: a retried turn (e.g. the reply
    # failed after filing) must not file twice, but a second request must file.
    conversation_id = f"{session_id}:{draft['id']}" if session_id else ""
    async with _filing_lock(conversation_id):
        return await _file_once(tool_context, employee, draft, conversation_id)


# Two submits for the same request at the same moment (a double click, a retried turn) each have
# their own copy of the session state, so neither sees the other's ticket number. In this process a
# lock per request serializes them and remembers what was filed; across instances the correlation
# id lookup inside the lock catches it.
_FILING_LOCKS: dict[str, asyncio.Lock] = {}


_FILED: dict[str, dict] = {}


def _filing_lock(key: str) -> asyncio.Lock:
    if not key:
        return asyncio.Lock()  # no session id (tests, local runs): nothing to share
    if len(_FILING_LOCKS) > 2000:
        for k in [k for k, lock in _FILING_LOCKS.items() if not lock.locked()][:1000]:
            _FILING_LOCKS.pop(k, None)
            _FILED.pop(k, None)
    return _FILING_LOCKS.setdefault(key, asyncio.Lock())


async def _file_once(tool_context: ToolContext, employee: dict, draft: dict, conversation_id: str) -> dict:
    issue, device = draft["issue"], draft["device"]
    existing = _FILED.get(conversation_id) if conversation_id else None
    if not existing and conversation_id:
        existing = await servicenow.find_open_by_correlation(employee["sys_id"], conversation_id)
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
        if conversation_id:
            _FILED[conversation_id] = ticket
        _log_path(tool_context, draft, ticket)
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
    if delivery.get("kind") == "permanent" and not delivery.get("saved") and PROFILE.features.saved_addresses:
        await memory.save_address(employee["email"], delivery.get("label", ""), delivery["address"])
    if PROFILE.features.memory:
        await memory.remember_conversation(tool_context, employee["email"])
    _show(tool_context, cards.confirmation(ticket["number"], draft))
    return {"status": "submitted", "ticket": ticket["number"], "priority": draft["assigned_priority"],
            "requested_priority": draft["priority"], "sla": draft["sla"]}


def _log_path(tool_context: ToolContext, draft: dict, ticket: dict) -> None:
    """One structured line per filed ticket: which path people actually take (D10). Filter in Cloud
    Logging with jsonPayload.event="ticket_filed" or textPayload:"ticket_filed"."""
    device, issue = draft.get("device") or {}, draft.get("issue") or {}
    logger.info("ticket_filed %s", json.dumps({
        "event": "ticket_filed", "ticket": ticket.get("number", ""),
        "source": draft.get("source", "unknown"),           # tag, tag_fuzzy, description, photo_label, photo_match
        "device_kind": device.get("kind", ""), "relation": device.get("relation", ""),
        "issue": issue.get("category", ""), "urgency": issue.get("urgency", ""),
        "photos": len(draft.get("photos") or []), "photo_skipped": bool(draft.get("photo_skipped")),
        "ship_to": (draft.get("delivery") or {}).get("kind", "on_file"),
        "display": tool_context.state.get("ui_mode", ""),
        "priority_changed_by_servicenow": draft.get("priority", "")[:1] != str(ticket.get("priority", ""))[:1],
    }, sort_keys=True))


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
