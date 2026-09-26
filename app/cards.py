"""A2UI v0.8 cards for each wizard step, built in Python.

Gemini Enterprise renders A2UI v0.8 (`beginRendering` + `surfaceUpdate`), and
only the basic catalog. The cards are deterministic on purpose: the model
decides which step comes next, but never writes UI JSON, so a card cannot
drift into v0.9 message names, invent a catalog, or show a hallucinated value.

Rules the renderer enforces, all handled here:
- components are a flat list referenced by id; `beginRendering` comes first
- every text is `{"literalString": ...}` and contains no markdown
- a fresh surfaceId per card, or GE rewrites the previous card in place
"""

import itertools
import re
import uuid

BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_8/basic_catalog.json"
# Baptist Health South Florida palette (baptisthealth.net): the WCAG-safe brand
# green, and the site's Poppins typeface. GE may apply its own theme on top.
PRIMARY_COLOR = "#22873B"
FONT = "Poppins, Helvetica, Arial, sans-serif"
TOTAL_STEPS = 4
PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}

ISSUE_CATEGORIES = [
    ("cracked_screen", "Cracked or broken screen"),
    ("wont_power_on", "Won't turn on"),
    ("battery", "Battery or charging"),
    ("keyboard_trackpad", "Keyboard or trackpad"),
    ("liquid_damage", "Liquid spill"),
    ("physical_damage", "Other physical damage"),
    ("performance", "Slow or freezing"),
    ("other", "Something else"),
]
ISSUE_LABELS = dict(ISSUE_CATEGORIES)


def _lit(value) -> dict:
    return {"literalString": _plain(str(value))}


def _plain(text: str) -> str:
    """GE shows markdown characters literally, so none may reach a card."""
    return text.replace("**", "").replace("__", "").replace("`", "").lstrip("#").strip()


class Card:
    """Accumulates components for one surface and emits the v0.8 message pair."""

    def __init__(self) -> None:
        self.surface_id = f"hw_{uuid.uuid4().hex[:8]}"
        self._ids = itertools.count()
        self.components: list[dict] = []

    def _add(self, kind: str, props: dict) -> str:
        cid = f"c{next(self._ids)}"
        self.components.append({"id": cid, "component": {kind: props}})
        return cid

    def text(self, value: str, hint: str = "body") -> str:
        return self._add("Text", {"text": _lit(value), "usageHint": hint})

    def divider(self) -> str:
        return self._add("Divider", {"axis": "horizontal"})

    def column(self, children: list[str]) -> str:
        return self._add("Column", {"children": {"explicitList": children}, "alignment": "stretch"})

    def row(self, children: list[str]) -> str:
        return self._add("Row", {"children": {"explicitList": children}, "alignment": "center"})

    def button(self, label: str, action: str, context: dict | None = None, primary: bool = False) -> str:
        label_id = self.text(label)
        return self._add("Button", {
            "child": label_id,
            "primary": primary,
            "action": {
                "name": action,
                "context": [{"key": k, "value": _lit(v)} for k, v in (context or {}).items()],
            },
        })

    def field(self, label: str, value, missing: str = "Not provided") -> str:
        """A 'Label: value' line. Empty values say so in words: a bare "-" is
        rendered by GE as a markdown bullet."""
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
        column = self.column(children)
        root = self._add("Card", {"child": column})
        return [
            {"beginRendering": {"surfaceId": self.surface_id, "root": root,
                                "styles": {"primaryColor": PRIMARY_COLOR, "font": FONT}}},
            {"surfaceUpdate": {"surfaceId": self.surface_id, "components": self.components}},
        ]


def _device_label(asset: dict) -> str:
    return f"{asset.get('model') or asset.get('device_type', 'Device')} ({asset.get('asset_tag', 'no tag')})"


# --- Step 1: which device --------------------------------------------------------


def device_picker(employee: dict, assets: list[dict]) -> list[dict]:
    c = Card()
    first = (employee.get("name") or "there").split(" ")[0]
    kids = c.header(1, f"Hi {first}, which device needs replacing?",
                    "Choose your device. Not listed? Send a photo of its asset tag or serial label and I'll look it up.")
    for asset in assets:
        kids.append(c.button(_device_label(asset), "select_device", {"asset_tag": asset["asset_tag"]}))
    kids.append(c.button("A different device", "different_device"))
    kids.append(c.button("View my tickets", "list_tickets"))
    kids += [c.divider(), c.text(f"Requesting as {employee.get('name', '')} ({employee.get('email', '')})  |  "
                                 f"{employee.get('location', '')}", "caption")]
    return c.build(kids)


# --- Step 2: what is wrong ---------------------------------------------------------


def issue_picker(device: dict, suggestion: str = "") -> list[dict]:
    c = Card()
    kids = c.header(2, "What's wrong with it?",
                    f"{_device_label(device)}. Pick the closest match, or just describe it in your own words.")
    if suggestion:
        kids.append(c.text(f"From your photo it looks like: {ISSUE_LABELS.get(suggestion, suggestion)}", "caption"))
    buttons = [c.button(label, "select_issue", {"category": key}, primary=(key == suggestion))
               for key, label in ISSUE_CATEGORIES]
    # Two per row keeps eight options compact on a phone.
    for i in range(0, len(buttons), 2):
        kids.append(c.row(buttons[i:i + 2]))
    return c.build(kids)


# --- Step 3: show me ----------------------------------------------------------------


def photo_request(device: dict, issue_label: str, what_to_shoot: str, required: bool) -> list[dict]:
    c = Card()
    kids = c.header(3, "Show me the problem",
                    f"{issue_label} on your {device.get('model') or 'device'}. A photo lets the service desk approve this without a follow-up call.")
    kids.append(c.text(f"Take a photo of: {what_to_shoot}", "body"))
    kids.append(c.text("Use the attach (+) button in the chat to add the photo.", "caption"))
    if required:
        kids.append(c.text("A photo is required for this kind of damage.", "caption"))
    else:
        kids.append(c.button("Skip the photo", "skip_photo"))
    return c.build(kids)


def label_photo_request() -> list[dict]:
    c = Card()
    kids = c.header(1, "Let's identify the device",
                    "Take a photo of the sticker with the asset tag or serial number. It's usually on the bottom of a laptop, the back of a monitor, or in Settings on a phone.")
    kids.append(c.text("Use the attach (+) button in the chat to add the photo.", "caption"))
    kids.append(c.button("I can't find a label", "no_label"))
    return c.build(kids)


# --- Photo findings (shown when the photo arrives before the problem is known) -----


def photo_findings(findings: dict, assets: list[dict], note: str) -> list[dict]:
    """Shown when a photo arrives before the device is known: what the photo
    showed, then the user's devices, with the likely match highlighted."""
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
    for asset in assets:
        likely = bool(kind and asset.get("device_type") == kind
                      and (not maker or maker in (asset.get("manufacturer") or "").lower()))
        kids.append(c.button(_device_label(asset), "select_device", {"asset_tag": asset["asset_tag"]}, primary=likely))
    kids.append(c.button("None of these", "different_device"))
    return c.build(kids)


# --- Step 4: review and submit ---------------------------------------------------------


def review(draft: dict, employee: dict) -> list[dict]:
    c = Card()
    device, issue = draft.get("device") or {}, draft.get("issue") or {}
    kids = c.header(4, "Review your request", "Everything below was filled in for you. Submit, or tell me what to change.")

    kids += [c.text("Device", "h5"),
             c.field("Model", device.get("model")),
             c.field("Asset tag", device.get("asset_tag")),
             c.field("Serial", device.get("serial_number")),
             c.field("Coverage", (draft.get("eligibility") or {}).get("summary"))]

    kids += [c.divider(), c.text("Problem", "h5"),
             c.field("Issue", ISSUE_LABELS.get(issue.get("category"), issue.get("category"))),
             c.field("Details", issue.get("description")),
             c.field("Requested priority", draft.get("priority", "3 - Moderate"))]
    evidence = draft.get("evidence")
    if evidence:
        kids.append(c.field("Photo evidence", evidence.get("summary")))

    kids += [c.divider(), c.text("Fulfilment", "h5"),
             c.field("Recommended", draft.get("recommendation")),
             c.field("Ship to", draft.get("delivery_location") or employee.get("location_address")
                     or employee.get("location")),
             c.field("Bill to", " / ".join(filter(None, [employee.get("cost_center"), employee.get("department")]))),
             c.field("Requested by", f"{employee.get('name', '')} ({employee.get('email', '')})")]
    for warning in draft.get("warnings") or []:
        kids.append(c.text(f"Note: {warning}", "caption"))

    kids.append(c.row([
        c.button("Submit request", "submit_ticket", primary=True),
        c.button("Change something", "edit_request"),
    ]))
    suggested = draft.get("suggested_device") or {}
    if suggested and suggested.get("asset_tag") != device.get("asset_tag"):
        kids.append(c.button(f"Use {_device_label(suggested)} instead", "select_device",
                             {"asset_tag": suggested["asset_tag"]}))
    return c.build(kids)


def confirmation(number: str, draft: dict) -> list[dict]:
    c = Card()
    kids = c.header(None, f"Request {number} submitted",
                    "You'll get an email from the service desk. Nothing else is needed from you right now.")
    kids += [
        c.field("Ticket", number),
        c.field("Status", "New"),
        c.field("Priority", draft.get("assigned_priority") or draft.get("priority")),
        *([c.text(draft["priority_note"], "caption")] if draft.get("priority_note") else []),
        c.field("Device", (draft.get("device") or {}).get("model")),
        c.field("Recommended", draft.get("recommendation")),
        c.field("Expected", draft.get("sla")),
        c.divider(),
        c.text("Before your replacement arrives, make sure your files are synced to cloud storage.", "caption"),
        c.row([c.button("View my tickets", "list_tickets"), c.button("Start another request", "start_over")]),
    ]
    return c.build(kids)


# --- Existing tickets ------------------------------------------------------------------


def ticket_list(tickets: list[dict], include_closed: bool) -> list[dict]:
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
                 c.text(f"Opened {t['opened'][:10]}  |  Priority {PRIORITY_LABELS.get(t['priority'], t['priority'])}", "caption"),
                 c.button("Details", "view_ticket", {"number": t["number"]})]
    buttons = [c.button("Start a new request", "start_over", primary=not tickets)]
    if not include_closed:
        buttons.insert(0, c.button("Include closed", "list_all_tickets"))
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
                           c.button("Cancel request", "cancel_ticket", {"number": t["number"]})]))
    kids.append(c.button("Back to my tickets", "list_tickets"))
    return c.build(kids)


def prepend_text(messages: list[dict], text: str) -> list[dict]:
    """Puts the model's short intro line at the top of a card.

    Prose and A2UI in one response usually render as neither, so the model's
    sentence travels inside the card as its first Text component.
    """
    text = _plain(" ".join(text.split()))
    if not text:
        return messages
    begin = messages[0]["beginRendering"]
    components = messages[1]["surfaceUpdate"]["components"]
    by_id = {c["id"]: c for c in components}
    column = by_id[by_id[begin["root"]]["component"]["Card"]["child"]]["component"]["Column"]
    components.append({"id": "intro", "component": {"Text": {"text": _lit(text), "usageHint": "body"}}})
    column["children"]["explicitList"].insert(0, "intro")
    return messages


def intro_of(messages: list[dict]) -> str:
    """The model's intro line inside a card, if `prepend_text` added one."""
    for m in messages:
        for comp in (m.get("surfaceUpdate") or {}).get("components", []):
            if comp.get("id") == "intro":
                return comp["component"]["Text"]["text"]["literalString"]
    return ""


# --- Text rendering (Gemini Enterprise mobile app: no A2UI) ------------------------


def to_text(messages: list[dict]) -> tuple[str, list[dict]]:
    """Renders a card as markdown text with numbered options, for clients that
    can't show A2UI (the Gemini Enterprise mobile app). Returns the text and the
    options in number order, each {"label", "action", "context"}, so a reply of
    "2" can act like a click.

    GE renders replies as markdown, where a single newline is ignored: so every
    line is its own paragraph, 'Label: value' rows are a bulleted list and the
    buttons a numbered list.
    """
    begin = next(m["beginRendering"] for m in messages if "beginRendering" in m)
    by_id = {c["id"]: c["component"] for m in messages for c in (m.get("surfaceUpdate") or {}).get("components", [])}
    blocks: list[tuple[str, str]] = []  # (kind, markdown): kind is para, field or option
    options: list[dict] = []

    def text_of(cid: str) -> str:
        comp = by_id.get(cid, {})
        return comp["Text"]["text"].get("literalString", "").strip() if "Text" in comp else ""

    def walk(cid: str) -> None:
        comp = by_id.get(cid, {})
        kind, props = next(iter(comp.items()), (None, {}))
        if kind == "Card":
            walk(props["child"])
        elif kind in ("Column", "Row"):
            kids = props["children"]["explicitList"]
            if kind == "Row" and kids and all("Text" in by_id.get(k, {}) for k in kids):
                label, *rest = [text_of(k) for k in kids]
                blocks.append(("field", f"- **{label}** {' '.join(rest)}".rstrip()))
            else:
                for child in kids:
                    walk(child)
        elif kind == "Text":
            value = text_of(cid)
            if value:
                heading = (props.get("usageHint") or "").startswith("h")
                blocks.append(("para", f"**{value}**" if heading else value))
        elif kind == "Button":
            action = props.get("action") or {}
            context = {c["key"]: c["value"].get("literalString", "") for c in action.get("context", [])}
            options.append({"label": text_of(props["child"]), "action": action.get("name", ""), "context": context})
            blocks.append(("option", f"{len(options)}. {options[-1]['label']}"))

    walk(begin["root"])
    if options:
        blocks.append(("para", "_Reply with a number, or just type your answer._"))
    out = ""
    for i, (kind, md) in enumerate(blocks):
        if i:
            # Items of one list stay together; everything else is a new paragraph.
            out += "\n" if kind in ("field", "option") and blocks[i - 1][0] == kind else "\n\n"
        out += md
    return out, options
