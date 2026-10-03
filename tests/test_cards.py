"""Every card must validate against the A2UI v0.9 schema and basic catalog that
Gemini Enterprise renders (the agent card declares v0.9)."""

import json
from pathlib import Path

import a2ui
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app import cards

_ASSETS = Path(a2ui.__path__[0]) / "assets" / "0.9"
_DOCS = {n: json.loads((_ASSETS / n).read_text()) for n in ("server_to_client.json", "common_types.json", "catalog.json")}
_REGISTRY = Registry()
for _name, _doc in _DOCS.items():
    _res = Resource.from_contents(_doc)
    _REGISTRY = _REGISTRY.with_resource(_doc["$id"], _res).with_resource(
        f"https://a2ui.org/specification/v0_9/{_name}", _res)
VALIDATOR = Draft202012Validator(_DOCS["server_to_client.json"], registry=_REGISTRY)
CATALOG_ID = _DOCS["catalog.json"]["catalogId"]

EMPLOYEE = {"email": "a@example.com", "name": "Alex Morgan", "location": "Chicago HQ",
            "location_address": "100 Main St, Chicago IL 60601", "cost_center": "CC-100 Sales", "department": "Sales"}
TICKET = {"number": "INC0010001", "short_description": "Cracked screen: ThinkPad", "state": "New", "state_code": "1",
          "priority": "2", "opened": "2026-09-25 10:00:00", "updated": "2026-09-25 10:05:00",
          "assignment_group": "Hardware", "assigned_to": "", "description": ""}
ASSET = {"asset_tag": "IT-1", "serial_number": "S1", "model": "ThinkPad X1", "device_type": "laptop",
         "manufacturer": "Lenovo"}
DRAFT = {"device": ASSET, "issue": {"category": "cracked_screen", "description": "**cracked**"},
         "eligibility": {"summary": "Under warranty"}, "priority": "2 - High",
         "recommendation": "Warranty replacement", "sla": "Next business day",
         "evidence": {"summary": "Spiderweb cracks"}, "warnings": ["Check owner"]}

ALL_CARDS = {
    "device_picker": cards.device_picker(EMPLOYEE, [ASSET]),
    "issue_picker": cards.issue_picker(ASSET, "cracked_screen"),
    "photo_request": cards.photo_request(ASSET, "Cracked screen", "the screen", required=True),
    "label_photo": cards.label_photo_request(),
    "photo_findings": cards.photo_findings({"manufacturer": "Lenovo", "device_type": "laptop",
                                            "damage_present": True, "damage_description": "cracks"},
                                           [ASSET], "note"),
    "review": cards.review(DRAFT, EMPLOYEE),
    "confirmation": cards.confirmation("INC0010001", DRAFT),
    "ticket_list": cards.ticket_list([TICKET], include_closed=False),
    "ticket_list_empty": cards.ticket_list([], include_closed=True),
    "ticket_detail": cards.ticket_detail(dict(TICKET, description="Problem: Cracked\nShip to: 1 Main St"),
                                         [{"when": "2026-09-25 17:55:00", "who": "John Doe", "kind": "note",
                                           "text": "Ship to home"}], note="Your note was added.", view="notes"),
    "ticket_detail_no_notes": cards.ticket_detail(TICKET),
    "ticket_last_note": cards.ticket_detail(TICKET, [{"when": "2026-09-25 18:00:00", "who": "M", "kind": "note",
                                                      "text": "a"}], view="last_note"),
    "ticket_details": cards.ticket_detail(dict(TICKET, description="Ship to: x"), [], view="details"),
    "ticket_change": cards.ticket_detail(TICKET, note="Done: status set to In Progress.", view="change"),
    "ticket_detail_raw_note": cards.ticket_detail(TICKET, [{"when": "", "who": "", "kind": "note", "text": "raw"}]),
    "confirm_device": cards.confirm_device(dict(ASSET, kind="clinical", location="MRI 1", department="Radiology",
                                                relation_text="Radiology (your department)"), "Matched by model"),
    "existing_tickets": cards.existing_tickets(ASSET, [dict(TICKET, caller="Jane Doe")]),
    "device_choices": cards.device_choices("Which one?", [ASSET, dict(ASSET, asset_tag="IT-2", location="Room 2")]),
}


def _texts(messages):
    return [c["text"] for c in cards.components_of(messages) if c["component"] == "Text"]


@pytest.mark.parametrize("name", ALL_CARDS)
def test_card_is_valid_v09(name):
    messages = ALL_CARDS[name]
    # createSurface must come first, naming the catalog, or GE has no surface to attach to.
    assert "createSurface" in messages[0] and "updateComponents" in messages[1]
    for message in messages:
        errors = [e.message for e in VALIDATOR.iter_errors(message)]
        assert not errors, errors[:3]
    create, update = messages[0]["createSurface"], messages[1]["updateComponents"]
    assert create["catalogId"] == CATALOG_ID == cards.BASIC_CATALOG_ID
    assert create["surfaceId"] == update["surfaceId"]
    ids = {c["id"] for c in update["components"]}
    assert "root" in ids and len(ids) == len(update["components"])
    for comp in update["components"]:  # every id reference resolves
        for ref in [comp.get("child")] + list(comp.get("children") or []):
            assert ref is None or ref in ids


def test_no_markdown_reaches_a_card():
    assert "**" not in json.dumps(ALL_CARDS["review"])


def test_surface_ids_are_fresh():
    assert (cards.label_photo_request()[0]["createSurface"]["surfaceId"]
            != cards.label_photo_request()[0]["createSurface"]["surfaceId"])


def test_prepend_text_goes_first_and_round_trips():
    messages = cards.prepend_text(cards.label_photo_request(), "Sorry about that! **Quick** photo please.")
    assert not list(VALIDATOR.iter_errors(messages[1]))
    by_id = {c["id"]: c for c in cards.components_of(messages)}
    assert by_id[by_id["root"]["child"]]["children"][0] == "intro"
    assert cards.intro_of(messages) == "Sorry about that! Quick photo please."


def test_no_empty_text_components():
    for name, messages in ALL_CARDS.items():
        assert all(t.strip() for t in _texts(messages)), f"empty Text in {name}"


def test_buttons_carry_their_action_and_context():
    buttons = [c for c in cards.components_of(ALL_CARDS["device_picker"]) if c["component"] == "Button"]
    assert buttons[0]["action"] == {"event": {"name": "select_device", "context": {"asset_tag": "IT-1"}}}


def test_status_view_has_no_notes_or_details():
    texts = _texts(cards.ticket_detail(dict(TICKET, description="Ship to: x"),
                                       [{"when": "t", "who": "w", "kind": "note", "text": "secret note"}],
                                       view="status"))
    assert "secret note" not in texts and "Ship to: x" not in texts


_V08 = Path(a2ui.__path__[0]) / "assets" / "0.8"
V08_SCHEMA = json.loads((_V08 / "server_to_client.json").read_text())
V08_CATALOG = json.loads((_V08 / "standard_catalog_definition.json").read_text())
V08_CATALOG = V08_CATALOG.get("components", V08_CATALOG)


@pytest.mark.parametrize("name", ALL_CARDS)
def test_v08_translation_is_valid(name):
    """A client that negotiated v0.8 gets the same card in v0.8."""
    import jsonschema
    messages = cards.to_v08(cards.prepend_text(ALL_CARDS[name], "Intro line."))
    assert list(messages[0]) == ["beginRendering"] and list(messages[1]) == ["surfaceUpdate"]
    for message in messages:
        jsonschema.validate(message, V08_SCHEMA)
    ids = {c["id"] for c in messages[1]["surfaceUpdate"]["components"]}
    assert messages[0]["beginRendering"]["root"] in ids
    for comp in messages[1]["surfaceUpdate"]["components"]:
        (kind, props), = comp["component"].items()
        jsonschema.validate(props, V08_CATALOG[kind])
        for ref in [props.get("child")] + props.get("children", {}).get("explicitList", []):
            assert ref is None or ref in ids


def test_v08_translation_keeps_actions_and_text():
    v08 = cards.to_v08(ALL_CARDS["device_picker"])
    comps = {c["id"]: c["component"] for c in v08[1]["surfaceUpdate"]["components"]}
    button = next(p["Button"] for p in comps.values() if "Button" in p)
    assert button["action"] == {"name": "select_device",
                                "context": [{"key": "asset_tag", "value": {"literalString": "IT-1"}}]}


def test_an_admin_account_is_warned_on_the_first_and_last_card():
    from app import cards
    admin = {"name": "System Administrator", "email": "admin@example.com", "is_admin": True}
    for msgs in (cards.device_picker(admin, []), cards.review({"device": {"model": "X"}, "issue": {}}, admin)):
        texts = " ".join(c.get("text", "") for c in cards.components_of(msgs))
        assert "signed in to ServiceNow as an administrator" in texts
    plain = cards.device_picker({"name": "Jane Doe"}, [])
    assert "administrator" not in " ".join(c.get("text", "") for c in cards.components_of(plain))
