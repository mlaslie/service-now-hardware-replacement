"""Every card must validate against the A2UI v0.8 schema that Gemini Enterprise renders."""

import json
from pathlib import Path

import a2ui
import jsonschema
import pytest

from app import cards

_ASSETS = Path(a2ui.__path__[0]) / "assets" / "0.8"
SCHEMA = json.loads((_ASSETS / "server_to_client.json").read_text())
CATALOG = json.loads((_ASSETS / "standard_catalog_definition.json").read_text())
CATALOG = CATALOG.get("components", CATALOG)

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
}


@pytest.mark.parametrize("name", ALL_CARDS)
def test_card_is_valid_v08(name):
    messages = ALL_CARDS[name]
    # beginRendering must come first, or GE has no surface to attach to.
    assert list(messages[0]) == ["beginRendering"]
    assert list(messages[1]) == ["surfaceUpdate"]
    for message in messages:
        jsonschema.validate(message, SCHEMA)

    begin, update = messages[0]["beginRendering"], messages[1]["surfaceUpdate"]
    assert begin["surfaceId"] == update["surfaceId"]
    ids = {c["id"] for c in update["components"]}
    assert begin["root"] in ids
    for comp in update["components"]:
        (kind, props), = comp["component"].items()
        assert kind in CATALOG, kind
        jsonschema.validate(props, CATALOG[kind])
        # Every id reference must resolve.
        for ref in [props.get("child")] + props.get("children", {}).get("explicitList", []):
            assert ref is None or ref in ids


def test_no_markdown_reaches_a_card():
    blob = json.dumps(ALL_CARDS["review"])
    assert "**" not in blob


def test_surface_ids_are_fresh():
    assert (cards.label_photo_request()[0]["beginRendering"]["surfaceId"]
            != cards.label_photo_request()[0]["beginRendering"]["surfaceId"])


def test_prepend_text_goes_first_and_round_trips():
    messages = cards.prepend_text(cards.label_photo_request(), "Sorry about that! **Quick** photo please.")
    jsonschema.validate(messages[1], SCHEMA)
    update = messages[1]["surfaceUpdate"]
    by_id = {c["id"]: c["component"] for c in update["components"]}
    column = by_id[by_id[messages[0]["beginRendering"]["root"]]["Card"]["child"]]["Column"]
    assert column["children"]["explicitList"][0] == "intro"
    assert cards.intro_of(messages) == "Sorry about that! Quick photo please."


def test_no_empty_text_components():
    for name, messages in ALL_CARDS.items():
        for comp in messages[1]["surfaceUpdate"]["components"]:
            text = comp["component"].get("Text")
            if text:
                assert text["text"]["literalString"].strip(), f"empty Text in {name}"


def test_status_view_has_no_notes_or_details():
    texts = [c["component"]["Text"]["text"]["literalString"]
             for c in cards.ticket_detail(dict(TICKET, description="Ship to: x"),
                                          [{"when": "t", "who": "w", "kind": "note", "text": "secret note"}],
                                          view="status")[1]["surfaceUpdate"]["components"] if "Text" in c["component"]]
    assert "secret note" not in texts and "Ship to: x" not in texts
