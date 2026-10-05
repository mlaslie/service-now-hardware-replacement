"""Existing tickets: list, show, change (attempt, verify, note), cancel, follow.
"""

import asyncio
import logging

from google.adk.tools import ToolContext

from app import cards, servicenow
from app.cards import ISSUE_LABELS
from app.tools._common import PROFILE, _draft, _employee, _no_identity, _save, _show, servicenow_errors
from app.tools.addresses import _resolve_address, replace_ship_to
from app.tools.review import _next_step

logger = logging.getLogger(__name__)

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
async def list_my_tickets(tool_context: ToolContext, include_closed: bool = False,
                          latest_notes: bool = False) -> dict:
    """Shows the user's hardware tickets, newest first.

    Args:
        include_closed: True to include resolved, closed and canceled tickets.
        latest_notes: True when they ask what's happening or for updates across their tickets
            ("any news on my replacement?"): each open ticket then shows its latest note.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    shown = 10
    tickets = await servicenow.my_incidents(employee["sys_id"], active_only=not include_closed, limit=shown + 1)
    more = len(tickets) > shown
    tickets = tickets[:shown]
    for t in tickets:
        t["following"] = bool(t.get("caller_id")) and t["caller_id"] != employee["sys_id"]
    if latest_notes:
        # One digest instead of opening each ticket (D8): the newest note of up to 5 open tickets.
        open_ = [t for t in tickets if t.get("state_code") in servicenow.ACTIVE_STATES][:5]
        found = await asyncio.gather(*(servicenow.notes(t["sys_id"]) for t in open_), return_exceptions=True)
        for t, entries in zip(open_, found):
            if isinstance(entries, list) and entries:
                t["latest_note"] = entries[0]["text"][:200]
    _show(tool_context, cards.ticket_list(tickets, include_closed, more))
    return {"status": "ok", "count": len(tickets), "more": more,
            "tickets": [{k: t.get(k, "") for k in ("number", "short_description", "state", "priority", "latest_note")}
                        for t in tickets]}


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
    settings = PROFILE.servicenow  # the instance's own choice values (config/organization.yaml)
    if code in ("6", "7", "8"):  # resolving/closing/canceling needs a close code and notes
        close_code = settings.close_codes.cancel if code == "8" else settings.close_codes.resolve
        fields.update({"close_code": close_code, "close_notes": reason or "Requested by the user"})
    if code == "3":
        fields["hold_reason"] = settings.hold_reason
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
    # Only the reporter changes a ticket. Fail closed: a ticket whose reporter ServiceNow didn't
    # return (a field the account can't read) is treated as someone else's.
    if changes and ticket.get("caller_id") != employee["sys_id"]:
        return await _follower_request(ctx, employee, ticket, changes, note)
    # Changes the organization leaves to the service desk (profile requester_changes) become a request note.
    by_desk = [ch for ch in changes if not getattr(PROFILE.requester_changes, ch.get("policy", ""), True)]
    if by_desk:
        changes = [ch for ch in changes if not any(ch is d for d in by_desk)]
        lines = "\n".join(f"- {ch['label']}: {ch['requested']}" for ch in by_desk)
        note = (note.strip() + "\n\n" if note.strip() else "") + (
            "The requester asked for the following through the Hardware Replacement agent. Changes like these are "
            f"made by the service desk here. Please review:\n{lines}")
    # Changes that can't apply to this ticket at all are explained, not reported as refused.
    skipped = [(ch, reason) for ch in changes if (reason := ch.get("precheck", lambda t: "")(ticket))]
    changes = [ch for ch in changes if not any(ch is c for c, _ in skipped)]
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
    for ch, reason in skipped:
        parts.append(f"{ch['label']} not changed: {reason}.")
    if by_desk:
        parts.append("Sent to the service desk, who make this change: "
                     + "; ".join(f"{c['label'].lower()} to {c['requested']}" for c in by_desk) + ".")
    if not_applied:
        parts.append("Not changed due to ServiceNow policy: "
                     + "; ".join(f"{c['label'].lower()} to {c['requested']}" for c in not_applied)
                     + ". A note asking the service desk to make this change was added to the ticket.")
    result = {
        "status": "ok",
        "note_added": bool(note.strip()),
        "changed": [{"change": c["label"], "value": c["requested"]} for c in applied],
        "not_permitted_note_added": [{"change": c["label"], "value": c["requested"]} for c in not_applied],
        "not_applicable": [{"change": c["label"], "value": c["requested"], "reason": r} for c, r in skipped],
        "requested_from_desk": [{"change": c["label"], "value": c["requested"]} for c in by_desk],
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
        changes.append(ch | {"policy": "status"})
    if urgency:
        ch = _urgency_change(urgency)
        if not ch:
            return {"status": "error", "message": "Urgency must be low, normal, high or critical."}
        changes.append(ch | {"policy": "urgency"})
    if ship_to:
        address = " ".join(ship_to.split())

        field = servicenow.SHIP_TO_FIELD  # the organization's ship-to field, if any

        def ship_fields(ticket, a=address):
            new = replace_ship_to(ticket["description"], a)
            out = {"description": new} if new else {}
            if field:
                out[field] = a
            return out

        def shipped_to(ticket, a=address) -> bool:
            if field:
                return ticket.get("ship_to") == a
            return f"Ship to: {a}" in ticket["description"]

        def no_ship_line(ticket):
            if "Ship to:" in (ticket.get("description") or "") or (field and ticket.get("ship_to")):
                return ""
            if field and not (ticket.get("description") or "").strip():
                return ""  # details went to a note, but the field takes the address
            if not (ticket.get("description") or "").strip():
                return "the ticket's details are in its notes, so the service desk will update the address"
            return "this ticket has no shipping address (equipment is repaired on site)"
        change = _change("Ship-to address", address, ship_fields, shipped_to)
        change["precheck"] = no_ship_line
        change["policy"] = "ship_to"
        changes.append(change)
        note = (note + "\n" if note else "") + f"The requester asked to ship the replacement to: {address}"
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
    if ticket.get("caller_id") != employee["sys_id"]:  # fail closed, as in _apply_changes
        return {"status": "not_permitted", "message": (
            f"Only the person who reported {number} ({ticket.get('caller') or 'someone else'}) can cancel it. "
            "The user follows it. Offer to add a note instead, e.g. that it is working again.")}
    reason = f"Canceled by the requester. Reason: {reason.strip()}"
    return await _apply_changes(tool_context, number, [_state_change("canceled", reason) | {"policy": "cancel"}], reason)


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
    problem = f"{ISSUE_LABELS.get(issue.get('category'), '')}: {issue.get('description', '')}" if issue else ""
    if ticket.get("caller_id") == employee["sys_id"]:
        # Their own open ticket for this device (D9): add the update, no following needed.
        note = " ".join(filter(None, ["Update from the reporter.", problem])) or "The reporter says it is still a problem."
        saved = await servicenow.update_incident(employee["sys_id"], ticket["number"], {"comments": note}) or ticket
        draft["submitted_number"] = saved["number"]
        _save(tool_context, draft)
        result = await _show_ticket(tool_context, saved, "Your update was added to your open ticket.", "change")
        return result | {"status": "ok", "following": False, "own_ticket": True, "note_added": note}
    note = " ".join(filter(None, [
        f"Also reported by {employee.get('name')} ({employee.get('department') or 'no department'}).", problem]))
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
async def unfollow_ticket(number: str, tool_context: ToolContext) -> dict:
    """Stops following a ticket someone else reported ("stop following INC..."): the user no longer
    gets its updates or sees it in their tickets. Their own tickets can't be unfollowed (cancel instead).

    Args:
        number: The incident number.
    """
    employee, ticket = await _own_ticket(tool_context, number)
    if not employee:
        return ticket
    if not ticket.get("caller_id") or ticket["caller_id"] == employee["sys_id"]:
        return {"status": "error", "message": f"{number} is the user's own ticket, so there is nothing to unfollow. "
                "Offer to cancel it instead if they no longer need it."}
    saved = await servicenow.unfollow_incident(employee["sys_id"], ticket["sys_id"])
    if employee["sys_id"] in saved.get("watch_list", []):
        await servicenow.update_incident(employee["sys_id"], number, {
            "comments": f"{employee.get('name') or 'A follower'} asked to stop following this ticket."})
        return {"status": "not_permitted", "message": "ServiceNow didn't let the account change the followers; "
                "a note asks the service desk to remove them."}
    tickets = await servicenow.my_incidents(employee["sys_id"])
    for t in tickets:
        t["following"] = bool(t.get("caller_id")) and t["caller_id"] != employee["sys_id"]
    _show(tool_context, cards.ticket_list(tickets, False))
    return {"status": "ok", "unfollowed": number}


@servicenow_errors
async def report_separately(tool_context: ToolContext) -> dict:
    """The user wants their own ticket even though the equipment already has an open one."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    draft["duplicates_checked"] = True
    return await _next_step(tool_context, draft, employee)
