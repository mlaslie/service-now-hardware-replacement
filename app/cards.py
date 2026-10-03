"""A2UI v0.9 cards for each wizard step, built in Python.

Gemini Enterprise negotiates the A2UI version from the agent card (it declares
v0.9, see server.py) and renders the v0.9 basic catalog in the web app. The
cards are deterministic on purpose: the model decides which step comes next,
but never writes UI JSON, so a card cannot mix versions, invent a catalog, or
show a hallucinated value.

Rules the renderer enforces, all handled here:
- every message carries "version": "v0.9"; `createSurface` (with the catalog) comes first
- components are flat (`{"id", "component": "Text", "text": ...}`), referenced by
  id, and one of them has id "root"
- no markdown in text (kept out so the mobile text rendering stays clean too)
- a fresh surfaceId per card, or GE rewrites the previous card in place
"""

import itertools
import re
import uuid

from app.profile import current as _current_profile

PROFILE = _current_profile()

A2UI_VERSION = "v0.9"
BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
# Baptist Health South Florida palette (baptisthealth.net): the WCAG-safe brand
# green (v0.9 `theme.primaryColor`). The typeface is left to GE.
PRIMARY_COLOR = PROFILE.branding.primary_color
FONT = "Poppins, Helvetica, Arial, sans-serif"
TOTAL_STEPS = 4
PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}

# Problem choices from the organization profile (config/organization.yaml).
ISSUE_CATEGORIES = [(i.key, i.label) for i in PROFILE.issues.personal]
# Shared and clinical equipment: repaired on site, so no laptop-style choices.
EQUIPMENT_ISSUES = [(i.key, i.label) for i in PROFILE.issues.equipment]
ISSUE_LABELS = PROFILE.issue_labels
SAFETY_TEXT = PROFILE.service.safety_text


def is_equipment(device: dict) -> bool:
    return (device or {}).get("kind") in ("shared", "clinical")


def _plain(text: str) -> str:
    """GE shows markdown characters literally, so none may reach a card."""
    return text.replace("**", "").replace("__", "").replace("`", "").lstrip("#").strip()


class Card:
    """Accumulates components for one surface and emits the v0.9 message pair."""

    def __init__(self) -> None:
        self.surface_id = f"hw_{uuid.uuid4().hex[:8]}"
        self._ids = itertools.count()
        self.components: list[dict] = []

    def _add(self, kind: str, props: dict, cid: str = "") -> str:
        cid = cid or f"c{next(self._ids)}"
        self.components.append({"id": cid, "component": kind, **props})
        return cid

    def text(self, value: str, hint: str = "body") -> str:
        return self._add("Text", {"text": _plain(str(value)), "variant": hint})

    def divider(self) -> str:
        return self._add("Divider", {"axis": "horizontal"})

    def column(self, children: list[str]) -> str:
        return self._add("Column", {"children": children, "align": "stretch"})

    def row(self, children: list[str]) -> str:
        return self._add("Row", {"children": children, "align": "center"})

    def button(self, label: str, action: str, context: dict | None = None, primary: bool = True) -> str:
        """Green (primary, theme colour) by default: every step forward. Pass
        primary=False for the ways back or out, which render grey."""
        label_id = self.text(label)
        props = {"child": label_id,
                 "action": {"event": {"name": action, "context": {k: str(v) for k, v in (context or {}).items()}}}}
        if primary:
            props["variant"] = "primary"
        return self._add("Button", props)

    def field(self, label: str, value, missing: str = "Not provided") -> str:
        """A 'Label: value' line. Empty values say so in words, never blank or "-"."""
        return self.row([self.text(f"{label}:", "caption"), self.text(value or missing, "body")])

    def header(self, step: int | None, title: str, subtitle: str = "") -> list[str]:
        ids = []
        if step:
            ids.append(self.text(f"Step {step} of {TOTAL_STEPS}", "caption"))
        ids.append(self.text(title, "h3"))
        if subtitle:
            ids.append(self.text(subtitle, "body"))
        return ids

    def build(self, children: list[str]) -> list[dict]:
        self._add("Card", {"child": self.column(children)}, cid="root")
        return [
            {"version": A2UI_VERSION, "createSurface": {
                "surfaceId": self.surface_id, "catalogId": BASIC_CATALOG_ID,
                "theme": {"primaryColor": PRIMARY_COLOR}}},
            {"version": A2UI_VERSION, "updateComponents": {"surfaceId": self.surface_id, "components": self.components}},
        ]


def components_of(messages: list[dict]) -> list[dict]:
    return next((m["updateComponents"]["components"] for m in messages if "updateComponents" in m), [])


V08_CATALOG_ID = "https://a2ui.org/specification/v0_8/basic_catalog.json"


def to_v08(messages: list[dict]) -> list[dict]:
    """The same card in A2UI v0.8, for a client that negotiated v0.8 (e.g. an
    agent registration made before the card declared v0.9)."""
    create = next(m["createSurface"] for m in messages if "createSurface" in m)
    out = []
    for comp in components_of(messages):
        kind = comp["component"]
        if kind == "Text":
            props = {"text": {"literalString": comp["text"]}, "usageHint": comp.get("variant", "body")}
        elif kind in ("Column", "Row"):
            props = {"children": {"explicitList": list(comp["children"])}, "alignment": comp.get("align", "stretch")}
        elif kind == "Button":
            event = comp["action"]["event"]
            props = {"child": comp["child"], "primary": comp.get("variant") == "primary", "action": {
                "name": event["name"],
                "context": [{"key": k, "value": {"literalString": v}} for k, v in (event.get("context") or {}).items()]}}
        elif kind == "Divider":
            props = {"axis": comp.get("axis", "horizontal")}
        else:  # Card
            props = {"child": comp["child"]}
        out.append({"id": comp["id"], "component": {kind: props}})
    return [
        {"beginRendering": {"surfaceId": create["surfaceId"], "root": "root",
                            "styles": {"primaryColor": PRIMARY_COLOR, "font": FONT}}},
        {"surfaceUpdate": {"surfaceId": create["surfaceId"], "components": out}},
    ]


def _device_label(asset: dict) -> str:
    return f"{asset.get('model') or asset.get('device_type', 'Device')} ({asset.get('asset_tag', 'no tag')})"


def maker_model(device: dict) -> str:
    """"GE HealthCare SIGNA Explorer", without doubling a maker the model name already has."""
    maker, model = (device.get("manufacturer") or "").strip(), (device.get("model") or "").strip()
    return model if maker and model.lower().startswith(maker.lower()) else " ".join(filter(None, [maker, model]))


def _device_line(asset: dict) -> str:
    """Model, asset tag and serial: enough to check against the device itself."""
    parts = [asset.get("model") or asset.get("device_type") or "Device", f"Tag {asset.get('asset_tag') or 'none'}"]
    if asset.get("serial_number"):
        parts.append(f"SN {asset['serial_number']}")
    return "  |  ".join(parts)


# --- Step 1: which device --------------------------------------------------------


def device_picker(employee: dict, assets: list[dict]) -> list[dict]:
    c = Card()
    first = (employee.get("name") or "there").split(" ")[0]
    kids = c.header(1, f"Hi {first}, what needs fixing?",
                    "Choose one of your devices. For equipment or anything not listed, tell me its asset tag or "
                    "serial number, send a photo of its sticker, or just say what it is and where.")
    for asset in assets:
        kids.append(c.button(_device_line(asset), "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button("Equipment or another device", "different_device"))
    kids.append(c.button("View my tickets", "list_tickets"))
    kids += [c.divider(), c.text(f"Requesting as {employee.get('name', '')} ({employee.get('email', '')})  |  "
                                 f"{employee.get('location', '')}", "caption")]
    return c.build(kids)


def device_choices(title: str, assets: list[dict]) -> list[dict]:
    """Several devices matched what the user described: let them pick."""
    c = Card()
    kids = c.header(1, title, "Pick the one you mean, or tell me its asset tag or serial number.")
    for asset in assets:
        where = asset.get("location") or asset.get("department") or ""
        label = _device_line(asset) + (f"  |  {where}" if where else "")
        kids.append(c.button(label, "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button("None of these", "different_device", primary=False))
    return c.build(kids)


def confirm_device(device: dict, note: str = "") -> list[dict]:
    """Before anything else: is this the device in front of you?"""
    c = Card()
    kids = c.header(1, "Is this the right device?", "Please check these details against the device itself.")
    kids += [c.field("Model", maker_model(device)),
             c.field("Asset tag", device.get("asset_tag")),
             c.field("Serial", device.get("serial_number"))]
    if is_equipment(device):
        kids += [c.field("Location", device.get("location")), c.field("Department", device.get("department"))]
    kids.append(c.field("Belongs to", device.get("relation_text")))
    if note:
        kids.append(c.text(note, "caption"))
    kids.append(c.row([c.button("Yes, that's it", "confirm_device", {"correct": "yes"}),
                       c.button("No, it's a different one", "confirm_device", {"correct": "no"}, primary=False)]))
    return c.build(kids)


def existing_tickets(device: dict, tickets: list[dict]) -> list[dict]:
    """Shared equipment is often reported by several people: offer to join the open ticket."""
    c = Card()
    kids = c.header(None, "This is already reported",
                    f"{_device_label(device)} has an open ticket. Add what you're seeing to it and follow it, "
                    "so you get the same updates, or report it separately if it's a different problem.")
    for t in tickets:
        kids += [c.divider(), c.text(f"{t['number']}  |  {t['state']}", "h5"), c.text(t["short_description"], "body"),
                 c.text(" | ".join(filter(None, [f"Reported by {t.get('caller') or 'someone'} on {t['opened'][:10]}",
                                                 f"Assigned to {t['assignment_group']}" if t.get("assignment_group")
                                                 else "Not yet assigned"])), "caption")]
    first = tickets[0]["number"]
    kids.append(c.row([c.button(f"Add my note to {first}", "follow_ticket", {"number": first}),
                       c.button("Report separately", "report_separately", primary=False)]))
    return c.build(kids)


# --- Step 2: what is wrong ---------------------------------------------------------


def issue_picker(device: dict, suggestion: str = "") -> list[dict]:
    c = Card()
    equipment = is_equipment(device)
    where = f" in {device['location']}" if equipment and device.get("location") else ""
    kids = c.header(2, "What's wrong with it?",
                    f"{_device_label(device)}{where}. Pick the closest match, or just describe it in your own words.")
    if suggestion and suggestion in ISSUE_LABELS:
        kids.append(c.text(f"From your photo it looks like: {ISSUE_LABELS[suggestion]}", "caption"))
    choices = EQUIPMENT_ISSUES if equipment else ISSUE_CATEGORIES
    buttons = [c.button(label, "select_issue", {"category": key})
               for key, label in choices]
    # Two per row keeps eight options compact on a phone.
    for i in range(0, len(buttons), 2):
        kids.append(c.row(buttons[i:i + 2]))
    return c.build(kids)


# --- Step 3: show me ----------------------------------------------------------------


def photo_request(device: dict, issue_label: str, what_to_shoot: str, required: bool) -> list[dict]:
    c = Card()
    kids = c.header(3, "Show me the problem",
                    f"{issue_label} on {'the' if is_equipment(device) else 'your'} {device.get('model') or 'device'}. "
                    "A photo lets the team fix it without a follow-up call.")
    kids.append(c.text(f"Take a photo of: {what_to_shoot}", "body"))
    kids.append(c.text("Use the attach (+) button in the chat to add the photo.", "caption"))
    if required:
        kids.append(c.text("A photo is required for this kind of damage.", "caption"))
    else:
        kids.append(c.button("Skip the photo", "skip_photo", primary=False))
    return c.build(kids)


def label_photo_request() -> list[dict]:
    c = Card()
    kids = c.header(1, "Let's identify the device",
                    "Take a photo of the sticker with the asset tag or serial number. It's usually on the bottom of a laptop, the back of a monitor, or in Settings on a phone.")
    kids.append(c.text("Use the attach (+) button in the chat to add the photo.", "caption"))
    kids.append(c.button("I can't find a label", "no_label", primary=False))
    return c.build(kids)


# --- Photo findings (shown when the photo arrives before the problem is known) -----


def photo_findings(findings: dict, assets: list[dict], note: str) -> list[dict]:
    """Shown when a photo arrives before the device is known: what the photo
    showed, then the user's devices, the likely match first."""
    c = Card()
    kids = c.header(None, "Here's what I found in your photo")
    kids += [c.field("Device", " ".join(filter(None, [findings.get("manufacturer"), findings.get("model")]))
                     or findings.get("device_type"), "Not recognized"),
             c.field("Serial", findings.get("serial_number"), "Not visible in photo"),
             c.field("Asset tag", findings.get("asset_tag"), "Not visible in photo")]
    if findings.get("damage_present"):
        kids.append(c.field("Damage", findings.get("damage_description")))
    if note:
        kids.append(c.text(note, "caption"))
    kids += [c.divider(), c.text("Which of your devices is this?", "h5")]
    maker = (findings.get("manufacturer") or "").lower()
    kind = (findings.get("device_type") or "").lower()

    def likely(asset: dict) -> bool:
        return bool(kind and asset.get("device_type") == kind
                    and (not maker or maker in (asset.get("manufacturer") or "").lower()))

    for asset in sorted(assets, key=lambda a: not likely(a)):  # the likely match first
        kids.append(c.button(_device_label(asset), "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button("None of these", "different_device", primary=False))
    return c.build(kids)


# --- Step 4: review and submit ---------------------------------------------------------


def review(draft: dict, employee: dict) -> list[dict]:
    c = Card()
    device, issue = draft.get("device") or {}, draft.get("issue") or {}
    equipment = is_equipment(device)
    kids = c.header(4, "Review your request", "Everything below was filled in for you. Submit, or tell me what to change.")

    kids += [c.text("Device", "h5"),
             c.field("Model", device.get("model")),
             c.field("Asset tag", device.get("asset_tag")),
             c.field("Serial", device.get("serial_number"))]
    if equipment:
        kids += [c.field("Location", device.get("location")), c.field("Department", device.get("department"))]
    else:
        kids.append(c.field("Coverage", (draft.get("eligibility") or {}).get("summary")))
    if device.get("relation_text") and device.get("relation") != "yours":
        kids.append(c.field("Belongs to", device.get("relation_text")))

    kids += [c.divider(), c.text("Problem", "h5"),
             c.field("Issue", ISSUE_LABELS.get(issue.get("category"), issue.get("category"))),
             c.field("Details", issue.get("description")),
             c.field("Requested priority", draft.get("priority", "3 - Moderate"))]
    evidence = draft.get("evidence")
    if evidence:
        kids.append(c.field("Photo evidence", evidence.get("summary")))

    if equipment:
        kids += [c.divider(), c.text("Repair", "h5"),
                 c.field("Service", draft.get("recommendation")),
                 c.field("Handled by", device.get("support_group"), "Service desk"),
                 c.field("Bill to", device.get("cost_center") or device.get("department"), "Set by the service desk"),
                 c.field("Reported by", f"{employee.get('name', '')} ({employee.get('email', '')})")]
    else:
        delivery = draft.get("delivery") or {}
        ship_to = delivery.get("address") or employee.get("location_address") or employee.get("location")
        if delivery.get("label"):
            ship_to = f"{delivery['label']}: {ship_to}"
        kids += [c.divider(), c.text("Fulfilment", "h5"),
                 c.field("Recommended", draft.get("recommendation")),
                 c.field("Ship to", ship_to),
                 c.field("Bill to", " / ".join(filter(None, [employee.get("cost_center"), employee.get("department")]))),
                 c.field("Requested by", f"{employee.get('name', '')} ({employee.get('email', '')})")]
    if PROFILE.is_safety(issue.get("category", "")):
        kids.append(c.text(f"Safety: {SAFETY_TEXT}", "body"))
    for warning in draft.get("warnings") or []:
        kids.append(c.text(f"Note: {warning}", "caption"))

    kids.append(c.row([
        c.button("Submit request", "submit_ticket"),
        c.button("Change something", "edit_request", primary=False),
    ]))
    if not equipment:
        # Other places only on request: a click, never "looks good".
        delivery = draft.get("delivery") or {}
        for saved in draft.get("saved_addresses") or []:
            if saved["address"] != delivery.get("address"):
                kids.append(c.button(f"Ship to {saved.get('label') or 'saved address'} instead: {saved['address']}",
                                     "choose_ship_to", {"address": saved["address"]}, primary=False))
        if delivery.get("address"):
            kids.append(c.button("Ship to my address on file instead", "choose_ship_to", {"address": ""},
                                 primary=False))
    suggested = draft.get("suggested_device") or {}
    if suggested and suggested.get("asset_tag") != device.get("asset_tag"):
        kids.append(c.button(f"Use {_device_label(suggested)} instead", "select_device",
                             {"asset_tag": suggested["asset_tag"]}))
    return c.build(kids)


def confirmation(number: str, draft: dict) -> list[dict]:
    c = Card()
    device = draft.get("device") or {}
    equipment = is_equipment(device)
    kids = c.header(None, f"Request {number} submitted",
                    (f"{device.get('support_group') or 'The service desk'} will pick this up. " if equipment else
                     "You'll get an email from the service desk. ") + "Ask me for updates any time.")
    kids += [
        c.field("Ticket", number),
        c.field("Status", "New"),
        c.field("Priority", draft.get("assigned_priority") or draft.get("priority")),
        *([c.text(draft["priority_note"], "caption")] if draft.get("priority_note") else []),
        c.field("Device", (draft.get("device") or {}).get("model")),
        c.field("Recommended", draft.get("recommendation")),
        c.field("Expected", draft.get("sla")),
        c.divider(),
        c.text(SAFETY_TEXT if PROFILE.is_safety((draft.get("issue") or {}).get("category", "")) else
               "If others report this equipment, they'll be offered to follow your ticket." if equipment else
               "Before your replacement arrives, make sure your files are synced to cloud storage.", "caption"),
        c.row([c.button("View my tickets", "list_tickets"), c.button("Start another request", "start_over")]),
    ]
    return c.build(kids)


# --- Existing tickets ------------------------------------------------------------------


def ticket_list(tickets: list[dict], include_closed: bool, more: bool = False) -> list[dict]:
    c = Card()
    title = "Your hardware tickets" if include_closed else "Your open hardware tickets"
    kids = c.header(None, title)
    if not tickets:
        kids.append(c.text("You have no open hardware tickets." if not include_closed
                           else "You have no hardware tickets yet.", "body"))
    for t in tickets:
        kids += [c.divider(),
                 c.text(f"{t['number']}  |  {t['state']}", "h5"),
                 c.text(t["short_description"], "body"),
                 c.text(" | ".join(filter(None, [
                     f"Opened {t['opened'][:10]}", f"Priority {PRIORITY_LABELS.get(t['priority'], t['priority'])}",
                     f"Reported by {t.get('caller') or 'someone else'}, you're following" if t.get("following") else ""])),
                     "caption"),
                 c.button("Details", "view_ticket", {"number": t["number"]})]
    if more:
        kids += [c.divider(), c.text(f"Showing your {len(tickets)} most recent. Ask for any other ticket by its number.",
                                     "caption")]
    buttons = [c.button("Start a new request", "start_over")]
    if not include_closed:
        buttons.insert(0, c.button("Include closed", "list_all_tickets", primary=False))
    kids.append(c.row(buttons))
    return c.build(kids)


TICKET_VIEWS = ("status", "last_note", "notes", "details", "change")


def ticket_detail(t: dict, notes: list[dict] | None = None, note: str = "", view: str = "status") -> list[dict]:
    """A ticket card: the status header always, plus only what the user asked for.

    view: "status" (header only), "last_note", "notes" (all notes), "details"
    (description and all notes), or "change" (header plus what was just changed).
    Showing everything every time makes the card huge, so each view adds one section.
    """
    c = Card()
    kids = c.header(None, f"{t['number']}: {t['short_description']}")
    kids += [c.field("Status", t["state"]),
             c.field("Priority", PRIORITY_LABELS.get(t["priority"], t["priority"])),
             c.field("Opened", t["opened"][:16]),
             c.field("Last updated", t["updated"][:16]),
             c.field("Assigned to", " / ".join(filter(None, [t.get("assignment_group"), t.get("assigned_to")])),
                     "Not yet assigned")]
    if note:
        kids += [c.divider(), c.text(note, "body")]

    notes = notes or []

    def show_notes(entries: list[dict]) -> None:
        for n in entries:
            header = " ".join(filter(None, [n["when"][:16], n["who"], "(work note)" if n["kind"] != "note" else ""]))
            if header:  # an empty Text renders as "(empty)" in Gemini Enterprise
                kids.append(c.text(header, "caption"))
            kids.append(c.text(n["text"] or "-", "body"))

    if view == "last_note":
        kids += [c.divider(), c.text("Latest note", "h5")]
        show_notes(notes[:1]) if notes else kids.append(c.text("No notes yet.", "caption"))
    elif view in ("notes", "details"):
        if view == "details" and t.get("description"):
            kids += [c.divider(), c.text("Details", "h5")]
            kids += [c.text(line, "caption") for line in t["description"].splitlines() if line.strip()]
        kids += [c.divider(), c.text(f"Notes ({len(notes)})", "h5")]
        show_notes(notes) if notes else kids.append(c.text("No notes yet.", "caption"))

    if view in ("status", "change"):
        kids.append(c.row([c.button("Notes", "view_notes", {"number": t["number"]}),
                           c.button("All details", "view_details", {"number": t["number"]})]))
    if t.get("state_code") in ("1", "2", "3"):
        kids.append(c.row([c.button("Add a note", "add_note", {"number": t["number"]}),
                           c.button("Change ship-to", "change_shipping", {"number": t["number"]})]))
        kids.append(c.row([c.button("Request urgent handling", "request_urgent", {"number": t["number"]}),
                           c.button("Cancel request", "cancel_ticket", {"number": t["number"]}, primary=False)]))
    kids.append(c.button("Back to my tickets", "list_tickets", primary=False))
    return c.build(kids)


def prepend_text(messages: list[dict], text: str) -> list[dict]:
    """Puts the model's short intro line at the top of a card.

    The model's sentence travels inside the card as its first Text component,
    so every reply is one card and nothing else.
    """
    text = _plain(" ".join(text.split()))
    if not text:
        return messages
    components = components_of(messages)
    by_id = {c["id"]: c for c in components}
    column = by_id[by_id["root"]["child"]]
    components.append({"id": "intro", "component": "Text", "text": text, "variant": "body"})
    column["children"].insert(0, "intro")
    return messages


def intro_of(messages: list[dict]) -> str:
    """The model's intro line inside a card, if `prepend_text` added one."""
    return next((c["text"] for c in components_of(messages) if c.get("id") == "intro"), "")


# --- Text rendering (Gemini Enterprise mobile app: no A2UI) ------------------------


_MD_INLINE = re.compile(r"([\\`*_\[\]<>!|~])")
_MD_LINE_START = re.compile(r"^(\s*)(\d+)([.)])|^(\s*)([#>+\-])", re.M)


def md_escape(text: str) -> str:
    """Text from ServiceNow or the user, shown as markdown in the mobile app: shown literally, so a
    note can't add links, images, emphasis, or a fake numbered option ("1. Cancel request")."""
    text = _MD_INLINE.sub(r"\\\1", text)
    return _MD_LINE_START.sub(lambda m: f"{m.group(1)}{m.group(2)}\\{m.group(3)}" if m.group(2)
                              else f"{m.group(4)}\\{m.group(5)}", text)


def to_text(messages: list[dict]) -> tuple[str, list[dict]]:
    """Renders a card as markdown text with numbered options, for clients that
    can't show A2UI (the Gemini Enterprise mobile app). Returns the text and the
    options in number order, each {"label", "action", "context"}, so a reply of
    "2" can act like a click.

    GE renders replies as markdown, where a single newline is ignored: so every
    line is its own paragraph, 'Label: value' rows are a bulleted list and the
    buttons a numbered list.
    """
    by_id = {c["id"]: c for c in components_of(messages)}
    blocks: list[tuple[str, str]] = []  # (kind, markdown): kind is para, field or option
    options: list[dict] = []

    def text_of(cid: str) -> str:
        comp = by_id.get(cid, {})
        return md_escape(str(comp.get("text", "")).strip()) if comp.get("component") == "Text" else ""

    def walk(cid: str) -> None:
        props = by_id.get(cid, {})
        kind = props.get("component")
        if kind == "Card":
            walk(props["child"])
        elif kind in ("Column", "Row"):
            kids = props["children"]
            if kind == "Row" and kids and all(by_id.get(k, {}).get("component") == "Text" for k in kids):
                label, *rest = [text_of(k) for k in kids]
                blocks.append(("field", f"- **{label}** {' '.join(rest)}".rstrip()))
            else:
                for child in kids:
                    walk(child)
        elif kind == "Text":
            value = text_of(cid)
            if value:
                heading = (props.get("variant") or "").startswith("h")
                blocks.append(("para", f"**{value}**" if heading else value))
        elif kind == "Button":
            event = (props.get("action") or {}).get("event") or {}
            options.append({"label": text_of(props["child"]), "action": event.get("name", ""),
                            "context": dict(event.get("context") or {})})
            blocks.append(("option", f"{len(options)}. {options[-1]['label']}"))

    walk("root")
    if options:
        blocks.append(("para", "_Reply with a number, or just type your answer._"))
    out = ""
    for i, (kind, md) in enumerate(blocks):
        if i:
            # Items of one list stay together; everything else is a new paragraph.
            out += "\n" if kind in ("field", "option") and blocks[i - 1][0] == kind else "\n\n"
        out += md
    return out, options
