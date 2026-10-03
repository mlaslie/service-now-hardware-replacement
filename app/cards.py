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

from app.messages import M
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

    def field(self, label: str, value, missing: str = "") -> str:
        """A 'Label: value' line. Empty values say so in words, never blank or "-"."""
        return self.row([self.text(f"{label}:", "caption"), self.text(value or missing or M("common.not_provided"), "body")])

    def header(self, step: int | None, title: str, subtitle: str = "") -> list[str]:
        ids = []
        if step:
            ids.append(self.text(M("common.step", step=step, total=TOTAL_STEPS), "caption"))
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
    parts = [asset.get("model") or asset.get("device_type") or "Device", M("device.tag", tag=asset.get("asset_tag") or "none")]
    if asset.get("serial_number"):
        parts.append(M("device.serial", serial=asset["serial_number"]))
    return "  |  ".join(parts)


# --- Step 1: which device --------------------------------------------------------


def device_picker(employee: dict, assets: list[dict]) -> list[dict]:
    c = Card()
    first = (employee.get("name") or "there").split(" ")[0]
    kids = c.header(1, M("picker.title", first_name=first), M("picker.subtitle"))
    for asset in assets:
        kids.append(c.button(_device_line(asset), "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button(M("picker.other_device"), "different_device"))
    kids.append(c.button(M("common.view_tickets"), "list_tickets"))
    kids += [c.divider(), c.text(M("picker.requesting_as", name=employee.get("name", ""), email=employee.get("email", ""),
                                   location=employee.get("location", "")), "caption")]
    if employee.get("is_admin"):
        kids.append(c.text(M("admin.warning", name=employee.get("name", "")), "body"))
    return c.build(kids)


def device_choices(title: str, assets: list[dict]) -> list[dict]:
    """Several devices matched what the user described: let them pick."""
    c = Card()
    kids = c.header(1, title, M("choices.subtitle"))
    for asset in assets:
        where = asset.get("location") or asset.get("department") or ""
        label = _device_line(asset) + (f"  |  {where}" if where else "")
        kids.append(c.button(label, "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button(M("common.none_of_these"), "different_device", primary=False))
    return c.build(kids)


def confirm_device(device: dict, note: str = "") -> list[dict]:
    """Before anything else: is this the device in front of you?"""
    c = Card()
    kids = c.header(1, M("confirm_device.title"), M("confirm_device.subtitle"))
    kids += [c.field(M("field.model"), maker_model(device)),
             c.field(M("field.asset_tag"), device.get("asset_tag")),
             c.field(M("field.serial"), device.get("serial_number"))]
    if is_equipment(device):
        kids += [c.field(M("field.location"), device.get("location")),
                 c.field(M("field.department"), device.get("department"))]
    kids.append(c.field(M("field.belongs_to"), device.get("relation_text")))
    if note:
        kids.append(c.text(note, "caption"))
    kids.append(c.row([c.button(M("confirm_device.yes"), "confirm_device", {"correct": "yes"}),
                       c.button(M("confirm_device.no"), "confirm_device", {"correct": "no"}, primary=False)]))
    return c.build(kids)


def existing_tickets(device: dict, tickets: list[dict], own: bool = False) -> list[dict]:
    """Shared equipment is often reported by several people: offer to join the open ticket. For the
    user's own device (own=True): they already reported it, so offer to add to that ticket."""
    c = Card()
    if own:
        kids = c.header(None, M("reported.own_title"), M("reported.own_subtitle", device=_device_label(device)))
    else:
        kids = c.header(None, M("reported.title"), M("reported.subtitle", device=_device_label(device)))
    for t in tickets:
        kids += [c.divider(), c.text(f"{t['number']}  |  {t['state']}", "h5"), c.text(t["short_description"], "body"),
                 c.text(" | ".join(filter(None, [M("reported.by", name=t.get("caller") or "someone", date=t["opened"][:10]),
                                                 M("reported.assigned", group=t["assignment_group"])
                                                 if t.get("assignment_group") else M("reported.unassigned")])),
                        "caption")]
    first = tickets[0]["number"]
    kids.append(c.row([c.button(M("reported.own_add" if own else "reported.follow", number=first), "follow_ticket",
                                {"number": first}),
                       c.button(M("reported.own_new" if own else "reported.separately"), "report_separately",
                                primary=False)]))
    return c.build(kids)


# --- Step 2: what is wrong ---------------------------------------------------------


def issue_picker(device: dict, suggestion: str = "") -> list[dict]:
    c = Card()
    equipment = is_equipment(device)
    where = M("issue.where", location=device["location"]) if equipment and device.get("location") else ""
    kids = c.header(2, M("issue.title"), M("issue.subtitle", device=_device_label(device), where=where))
    if suggestion and suggestion in ISSUE_LABELS:
        kids.append(c.text(M("issue.from_photo", issue=ISSUE_LABELS[suggestion]), "caption"))
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
    whose = M("photo.whose_equipment") if is_equipment(device) else M("photo.whose_personal")
    kids = c.header(3, M("photo.title"), M("photo.subtitle", issue=issue_label, whose=whose,
                                           device=device.get("model") or "device"))
    kids.append(c.text(M("photo.what", what=what_to_shoot), "body"))
    kids.append(c.text(M("common.attach_hint"), "caption"))
    if required:
        kids.append(c.text(M("photo.required"), "caption"))
    else:
        kids.append(c.button(M("photo.skip"), "skip_photo", primary=False))
    return c.build(kids)


def label_photo_request() -> list[dict]:
    c = Card()
    kids = c.header(1, M("label.title"), M("label.subtitle"))
    kids.append(c.text(M("common.attach_hint"), "caption"))
    kids.append(c.button(M("label.none"), "no_label", primary=False))
    return c.build(kids)


# --- Photo findings (shown when the photo arrives before the problem is known) -----


def photo_findings(findings: dict, assets: list[dict], note: str) -> list[dict]:
    """Shown when a photo arrives before the device is known: what the photo
    showed, then the user's devices, the likely match first."""
    c = Card()
    kids = c.header(None, M("findings.title"))
    kids += [c.field(M("field.device"), " ".join(filter(None, [findings.get("manufacturer"), findings.get("model")]))
                     or findings.get("device_type"), M("findings.not_recognized")),
             c.field(M("field.serial"), findings.get("serial_number"), M("findings.not_visible")),
             c.field(M("field.asset_tag"), findings.get("asset_tag"), M("findings.not_visible"))]
    if findings.get("damage_present"):
        kids.append(c.field(M("findings.damage"), findings.get("damage_description")))
    if note:
        kids.append(c.text(note, "caption"))
    kids += [c.divider(), c.text(M("findings.which"), "h5")]
    maker = (findings.get("manufacturer") or "").lower()
    kind = (findings.get("device_type") or "").lower()

    def likely(asset: dict) -> bool:
        return bool(kind and asset.get("device_type") == kind
                    and (not maker or maker in (asset.get("manufacturer") or "").lower()))

    for asset in sorted(assets, key=lambda a: not likely(a)):  # the likely match first
        kids.append(c.button(_device_label(asset), "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button(M("common.none_of_these"), "different_device", primary=False))
    return c.build(kids)


# --- Step 4: review and submit ---------------------------------------------------------


def review(draft: dict, employee: dict) -> list[dict]:
    c = Card()
    device, issue = draft.get("device") or {}, draft.get("issue") or {}
    equipment = is_equipment(device)
    kids = c.header(4, M("review.title"), M("review.subtitle"))

    kids += [c.text(M("review.section_device"), "h5"),
             c.field(M("field.model"), device.get("model")),
             c.field(M("field.asset_tag"), device.get("asset_tag")),
             c.field(M("field.serial"), device.get("serial_number"))]
    if equipment:
        kids += [c.field(M("field.location"), device.get("location")),
                 c.field(M("field.department"), device.get("department"))]
    else:
        kids.append(c.field(M("review.coverage"), (draft.get("eligibility") or {}).get("summary")))
    if device.get("relation_text") and device.get("relation") != "yours":
        kids.append(c.field(M("field.belongs_to"), device.get("relation_text")))

    kids += [c.divider(), c.text(M("review.section_problem"), "h5"),
             c.field(M("review.issue"), ISSUE_LABELS.get(issue.get("category"), issue.get("category"))),
             c.field(M("review.details"), issue.get("description")),
             c.field(M("review.priority"), draft.get("priority", "3 - Moderate"))]
    evidence = draft.get("evidence")
    if evidence:
        kids.append(c.field(M("review.evidence"), evidence.get("summary")))

    if equipment:
        kids += [c.divider(), c.text(M("review.section_repair"), "h5"),
                 c.field(M("review.service"), draft.get("recommendation")),
                 c.field(M("review.handled_by"), device.get("support_group"), M("review.service_desk")),
                 c.field(M("review.bill_to"), device.get("cost_center") or device.get("department"),
                         M("review.bill_to_desk")),
                 c.field(M("review.reported_by"), f"{employee.get('name', '')} ({employee.get('email', '')})")]
    else:
        delivery = draft.get("delivery") or {}
        ship_to = delivery.get("address") or employee.get("location_address") or employee.get("location")
        if delivery.get("label"):
            ship_to = f"{delivery['label']}: {ship_to}"
        kids += [c.divider(), c.text(M("review.section_fulfilment"), "h5"),
                 c.field(M("review.recommended"), draft.get("recommendation")),
                 c.field(M("review.ship_to"), ship_to),
                 c.field(M("review.bill_to"), " / ".join(filter(None, [employee.get("cost_center"),
                                                                       employee.get("department")]))),
                 c.field(M("review.requested_by"), f"{employee.get('name', '')} ({employee.get('email', '')})")]
    if PROFILE.is_safety(issue.get("category", "")):
        kids.append(c.text(M("review.safety", text=SAFETY_TEXT), "body"))
        if PROFILE.service.safety_event_url:
            kids.append(c.text(M("safety.event", url=PROFILE.service.safety_event_url), "body"))
    for warning in draft.get("warnings") or []:
        kids.append(c.text(M("review.note", text=warning), "caption"))
    if employee.get("is_admin"):
        kids.append(c.text(M("admin.warning", name=employee.get("name", "")), "body"))

    kids.append(c.row([
        c.button(M("review.submit"), "submit_ticket"),
        c.button(M("review.change"), "edit_request", primary=False),
    ]))
    if not equipment:
        # Other places only on request: a click, never "looks good".
        delivery = draft.get("delivery") or {}
        for saved in draft.get("saved_addresses") or []:
            if saved["address"] != delivery.get("address"):
                kids.append(c.button(M("review.ship_saved", label=saved.get("label") or M("review.ship_saved_label"),
                                       address=saved["address"]),
                                     "choose_ship_to", {"address": saved["address"]}, primary=False))
        if delivery.get("address"):
            kids.append(c.button(M("review.ship_on_file"), "choose_ship_to", {"address": ""}, primary=False))
    suggested = draft.get("suggested_device") or {}
    if suggested and suggested.get("asset_tag") != device.get("asset_tag"):
        kids.append(c.button(M("review.use_device", device=_device_label(suggested)), "select_device",
                             {"asset_tag": suggested["asset_tag"]}))
    return c.build(kids)


def confirmation(number: str, draft: dict) -> list[dict]:
    c = Card()
    device = draft.get("device") or {}
    equipment = is_equipment(device)
    safety = PROFILE.is_safety((draft.get("issue") or {}).get("category", ""))
    kids = c.header(None, M("done.title", number=number),
                    M("done.equipment", group=device.get("support_group") or M("done.equipment_group")) if equipment
                    else M("done.personal"))
    kids += [
        c.field(M("done.ticket"), number),
        c.field(M("done.status"), M("done.status_new")),
        c.field(M("done.priority"), draft.get("assigned_priority") or draft.get("priority")),
        *([c.text(draft["priority_note"], "caption")] if draft.get("priority_note") else []),
        c.field(M("field.device"), (draft.get("device") or {}).get("model")),
        c.field(M("review.recommended"), draft.get("recommendation")),
        c.field(M("done.expected"), draft.get("sla")),
        c.divider(),
        c.text(SAFETY_TEXT if safety else M("done.equipment_tip") if equipment else M("done.personal_tip"), "caption"),
        *([c.text(M("safety.event", url=PROFILE.service.safety_event_url), "body")]
          if safety and PROFILE.service.safety_event_url else []),
        c.row([c.button(M("common.view_tickets"), "list_tickets"), c.button(M("done.another"), "start_over")]),
    ]
    return c.build(kids)


# --- Existing tickets ------------------------------------------------------------------


def ticket_list(tickets: list[dict], include_closed: bool, more: bool = False) -> list[dict]:
    c = Card()
    kids = c.header(None, M("tickets.title_all") if include_closed else M("tickets.title_open"))
    if not tickets:
        kids.append(c.text(M("tickets.none") if include_closed else M("tickets.none_open"), "body"))
    for t in tickets:
        kids += [c.divider(),
                 c.text(f"{t['number']}  |  {t['state']}", "h5"),
                 c.text(t["short_description"], "body"),
                 c.text(" | ".join(filter(None, [
                     M("tickets.opened", date=t["opened"][:10]),
                     M("tickets.priority", priority=PRIORITY_LABELS.get(t["priority"], t["priority"])),
                     M("tickets.following", name=t.get("caller") or "someone else") if t.get("following") else ""])),
                     "caption"),
                 *([c.text(M("tickets.latest", note=t["latest_note"]), "body")] if t.get("latest_note") else []),
                 c.button(M("tickets.details"), "view_ticket", {"number": t["number"]})]
    if more:
        kids += [c.divider(), c.text(M("tickets.more", count=len(tickets)), "caption")]
    buttons = [c.button(M("tickets.new_request"), "start_over")]
    if not include_closed:
        buttons.insert(0, c.button(M("tickets.include_closed"), "list_all_tickets", primary=False))
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
    kids += [c.field(M("ticket.status"), t["state"]),
             c.field(M("ticket.priority"), PRIORITY_LABELS.get(t["priority"], t["priority"])),
             c.field(M("ticket.opened"), t["opened"][:16]),
             c.field(M("ticket.updated"), t["updated"][:16]),
             c.field(M("ticket.assigned"), " / ".join(filter(None, [t.get("assignment_group"), t.get("assigned_to")])),
                     M("ticket.unassigned"))]
    if note:
        kids += [c.divider(), c.text(note, "body")]

    notes = notes or []

    def show_notes(entries: list[dict]) -> None:
        for n in entries:
            header = " ".join(filter(None, [n["when"][:16], n["who"], M("ticket.work_note") if n["kind"] != "note" else ""]))
            if header:  # an empty Text renders as "(empty)" in Gemini Enterprise
                kids.append(c.text(header, "caption"))
            kids.append(c.text(n["text"] or "-", "body"))

    if view == "last_note":
        kids += [c.divider(), c.text(M("ticket.latest_note"), "h5")]
        show_notes(notes[:1]) if notes else kids.append(c.text(M("ticket.no_notes"), "caption"))
    elif view in ("notes", "details"):
        if view == "details" and t.get("description"):
            kids += [c.divider(), c.text(M("ticket.details"), "h5")]
            kids += [c.text(line, "caption") for line in t["description"].splitlines() if line.strip()]
        kids += [c.divider(), c.text(M("ticket.notes_count", count=len(notes)), "h5")]
        show_notes(notes) if notes else kids.append(c.text(M("ticket.no_notes"), "caption"))

    if view in ("status", "change"):
        kids.append(c.row([c.button(M("ticket.notes"), "view_notes", {"number": t["number"]}),
                           c.button(M("ticket.all_details"), "view_details", {"number": t["number"]})]))
    if t.get("state_code") in ("1", "2", "3"):
        kids.append(c.row([c.button(M("ticket.add_note"), "add_note", {"number": t["number"]}),
                           c.button(M("ticket.change_shipping"), "change_shipping", {"number": t["number"]})]))
        kids.append(c.row([c.button(M("ticket.urgent"), "request_urgent", {"number": t["number"]}),
                           c.button(M("ticket.cancel"), "cancel_ticket", {"number": t["number"]}, primary=False)]))
    kids.append(c.button(M("ticket.back"), "list_tickets", primary=False))
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
        blocks.append(("para", f"_{M('text.reply_hint')}_"))
    out = ""
    for i, (kind, md) in enumerate(blocks):
        if i:
            # Items of one list stay together; everything else is a new paragraph.
            out += "\n" if kind in ("field", "option") and blocks[i - 1][0] == kind else "\n\n"
        out += md
    return out, options
