"""Every user-facing text on the cards, by key: wording is configuration, not code.

The English defaults are below. `config/messages.yaml` (or the file named by MESSAGES_FILE)
overrides any of them. Each text may use only the {placeholders} its default uses, so a reworded
message can never break a card. Check with: uv run python -m app.messages

    from app.messages import M
    M("review.title")                         -> "Review your request"
    M("confirm.title", number="INC0010001")   -> "Request INC0010001 submitted"
"""

from __future__ import annotations

import functools
import os
import string
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "config" / "messages.yaml"

DEFAULTS: dict[str, str] = {
    # The first-turn question (mobile app vs desktop/browser) and the text-mode hint.
    "display.ask": "Are you on the Gemini Enterprise Desktop or Mobile App?\n\n1 = Mobile App\n\n2 = Desktop/Browser",
    "text.reply_hint": "Reply with a number, or just type your answer.",

    # Shared pieces.
    "common.step": "Step {step} of {total}",
    "common.not_provided": "Not provided",
    "common.attach_hint": "Use the attach (+) button in the chat to add the photo.",
    "common.none_of_these": "None of these",
    "common.view_tickets": "View my tickets",
    "field.model": "Model",
    "field.asset_tag": "Asset tag",
    "field.serial": "Serial",
    "field.location": "Location",
    "field.department": "Department",
    "field.belongs_to": "Belongs to",
    "field.device": "Device",
    "device.tag": "Tag {tag}",
    "device.serial": "SN {serial}",

    # Step 1: which device.
    "picker.title": "Hi {first_name}, what needs fixing?",
    "picker.subtitle": "Choose one of your devices. For equipment or anything not listed, tell me its asset tag or "
                       "serial number, send a photo of its sticker, or just say what it is and where.",
    "picker.other_device": "Equipment or another device",
    "picker.requesting_as": "Requesting as {name} ({email})  |  {location}",
    "admin.warning": "You're signed in to ServiceNow as an administrator ({name}), so requests are filed as that "
                     "account. If that isn't you, reconnect ServiceNow for this agent with your own account.",
    "choices.subtitle": "Pick the one you mean, or tell me its asset tag or serial number.",
    "confirm_device.title": "Is this the right device?",
    "confirm_device.subtitle": "Please check these details against the device itself.",
    "confirm_device.yes": "Yes, that's it",
    "confirm_device.no": "No, it's a different one",
    "reported.title": "This is already reported",
    "reported.subtitle": "{device} has an open ticket. Add what you're seeing to it and follow it, so you get the "
                         "same updates, or report it separately if it's a different problem.",
    "reported.by": "Reported by {name} on {date}",
    "reported.assigned": "Assigned to {group}",
    "reported.unassigned": "Not yet assigned",
    "reported.follow": "Add my note to {number}",
    "reported.separately": "Report separately",
    "reported.own_title": "You already have an open ticket for this",
    "reported.own_subtitle": "{device} has an open ticket from you. Add what's happening now to it, or file a new "
                             "request if it's a different problem.",
    "reported.own_add": "Add to {number}",
    "reported.own_new": "File a new request",

    # Step 2: what is wrong.
    "issue.title": "What's wrong with it?",
    "issue.subtitle": "{device}{where}. Pick the closest match, or just describe it in your own words.",
    "issue.where": " in {location}",
    "issue.from_photo": "From your photo it looks like: {issue}",

    # Step 3: photos.
    "photo.title": "Show me the problem",
    "photo.subtitle": "{issue} on {whose} {device}. A photo lets the team fix it without a follow-up call.",
    "photo.whose_equipment": "the",
    "photo.whose_personal": "your",
    "photo.what": "Take a photo of: {what}",
    "photo.required": "A photo is required for this kind of damage.",
    "photo.skip": "Skip the photo",
    "label.title": "Let's identify the device",
    "label.subtitle": "Take a photo of the sticker with the asset tag or serial number. It's usually on the bottom "
                      "of a laptop, the back of a monitor, or in Settings on a phone.",
    "label.none": "I can't find a label",
    "findings.title": "Here's what I found in your photo",
    "findings.not_recognized": "Not recognized",
    "findings.not_visible": "Not visible in photo",
    "findings.damage": "Damage",
    "findings.which": "Which of your devices is this?",

    # Step 4: review.
    "review.title": "Review your request",
    "review.subtitle": "Everything below was filled in for you. Submit, or tell me what to change.",
    "review.section_device": "Device",
    "review.coverage": "Coverage",
    "review.section_problem": "Problem",
    "review.issue": "Issue",
    "review.details": "Details",
    "review.priority": "Requested priority",
    "review.evidence": "Photo evidence",
    "review.section_repair": "Repair",
    "review.service": "Service",
    "review.handled_by": "Handled by",
    "review.service_desk": "Service desk",
    "review.bill_to": "Bill to",
    "review.bill_to_desk": "Set by the service desk",
    "review.reported_by": "Reported by",
    "review.section_fulfilment": "Fulfilment",
    "review.recommended": "Recommended",
    "review.ship_to": "Ship to",
    "review.requested_by": "Requested by",
    "review.safety": "Safety: {text}",
    "review.note": "Note: {text}",
    "safety.event": "If anyone was harmed, also file a safety event report: {url}",
    "review.submit": "Submit request",
    "review.change": "Change something",
    "review.ship_saved": "Ship to {label} instead: {address}",
    "review.ship_saved_label": "saved address",
    "review.ship_on_file": "Ship to my address on file instead",
    "review.use_device": "Use {device} instead",

    # Confirmation.
    "done.title": "Request {number} submitted",
    "done.equipment": "{group} will pick this up. Ask me for updates any time.",
    "done.equipment_group": "The service desk",
    "done.personal": "You'll get an email from the service desk. Ask me for updates any time.",
    "done.ticket": "Ticket",
    "done.status": "Status",
    "done.status_new": "New",
    "done.priority": "Priority",
    "done.expected": "Expected",
    "done.equipment_tip": "If others report this equipment, they'll be offered to follow your ticket.",
    "done.personal_tip": "Before your replacement arrives, make sure your files are synced to cloud storage.",
    "done.another": "Start another request",

    # Existing tickets.
    "tickets.title_all": "Your hardware tickets",
    "tickets.title_open": "Your open hardware tickets",
    "tickets.none_open": "You have no open hardware tickets.",
    "tickets.none": "You have no hardware tickets yet.",
    "tickets.opened": "Opened {date}",
    "tickets.priority": "Priority {priority}",
    "tickets.following": "Reported by {name}, you're following",
    "tickets.details": "Details",
    "tickets.latest": "Latest: {note}",
    "tickets.more": "Showing your {count} most recent. Ask for any other ticket by its number.",
    "tickets.new_request": "Start a new request",
    "tickets.include_closed": "Include closed",
    "ticket.status": "Status",
    "ticket.priority": "Priority",
    "ticket.opened": "Opened",
    "ticket.updated": "Last updated",
    "ticket.assigned": "Assigned to",
    "ticket.unassigned": "Not yet assigned",
    "ticket.work_note": "(work note)",
    "ticket.latest_note": "Latest note",
    "ticket.no_notes": "No notes yet.",
    "ticket.details": "Details",
    "ticket.notes_count": "Notes ({count})",
    "ticket.notes": "Notes",
    "ticket.all_details": "All details",
    "ticket.add_note": "Add a note",
    "ticket.change_shipping": "Change ship-to",
    "ticket.urgent": "Request urgent handling",
    "ticket.cancel": "Cancel request",
    "ticket.back": "Back to my tickets",
}


class MessagesError(ValueError):
    """config/messages.yaml has a key or placeholder the cards don't know."""


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def load(path: Path | str | None = None) -> dict[str, str]:
    """The defaults with the file's overrides applied, validated."""
    path = Path(path or os.environ.get("MESSAGES_FILE") or DEFAULT_FILE)
    texts = dict(DEFAULTS)
    if not path.exists():
        return texts
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise MessagesError(f"{path}: expected 'key: text' lines")
    problems = []
    for key, text in data.items():
        if key not in DEFAULTS:
            problems.append(f"{key}: unknown message (see app/messages.py for the keys)")
            continue
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{key}: must be non-empty text")
            continue
        try:
            used = _placeholders(text)
        except ValueError as exc:
            problems.append(f"{key}: {exc} (write a literal brace as {{{{ or }}}})")
            continue
        extra = used - _placeholders(DEFAULTS[key])
        if extra or any(n == "" or n.isdigit() for n in used):
            allowed = sorted(_placeholders(DEFAULTS[key])) or "none"
            problems.append(f"{key}: placeholder(s) {sorted(extra) or sorted(used)} not allowed here (allowed: {allowed})")
            continue
        texts[key] = text
    if problems:
        raise MessagesError(f"{path}:\n  " + "\n  ".join(problems))
    return texts


@functools.lru_cache(maxsize=1)
def current() -> dict[str, str]:
    return load()


def M(key: str, **values) -> str:
    """The text for `key`, with its placeholders filled in."""
    text = current()[key]
    return text.format(**values) if values or _placeholders(text) else text


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]
    try:
        texts = load(args[0] if args else None)
    except MessagesError as exc:
        print(exc)
        return 1
    changed = sum(texts[k] != DEFAULTS[k] for k in DEFAULTS)
    print(f"{len(texts)} messages OK ({changed} reworded from the defaults)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
