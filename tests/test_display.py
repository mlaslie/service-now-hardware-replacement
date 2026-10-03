"""Web app (A2UI cards) vs mobile app (numbered text): the first-turn question,
the replayed first message, numbered replies and the text rendering of cards."""

from app import cards, inbound
import pytest

from app.inbound import ASK_MARKER, UI_ASKS_KEY, UI_MODE_KEY, UI_OPTIONS_KEY, UI_PENDING_KEY, display_step


def test_first_message_asks_and_is_kept():
    text, delta = display_step({}, "My laptop screen is cracked")
    assert text == ASK_MARKER
    assert delta == {UI_MODE_KEY: "asking", UI_PENDING_KEY: "My laptop screen is cracked", UI_ASKS_KEY: 1}


@pytest.mark.parametrize("answer, mode", [
    ("1", "text"), ("Mobile App", "text"), ("mobile!", "text"), ("phone", "text"), ("1)", "text"),
    ("1 - mobile", "text"), ("1 = Mobile App", "text"), ("Option 1", "text"), ("I'm on my phone", "text"),
    ("on the mobile app", "text"), ("(1)", "text"), ("1.", "text"),
    ("2", "cards"), ("2 = Desktop/Browser", "cards"), ("I'm on desktop", "cards"), ("browser", "cards"),
    ("option 2", "cards"), ("Using Chrome on my laptop", "cards"), ("#2", "cards"),
])
def test_an_answer_picks_the_mode_and_replays_the_first_message(answer, mode):
    state = {UI_MODE_KEY: "asking", UI_PENDING_KEY: "My laptop screen is cracked\n[Photo attached: ph_1]"}
    text, delta = display_step(state, answer)
    assert text == state[UI_PENDING_KEY]
    assert delta == {UI_MODE_KEY: mode, UI_PENDING_KEY: "", UI_ASKS_KEY: 0}


@pytest.mark.parametrize("reply", ["yes", "no", "1 desktop", "2 mobile", "phone or desktop?",
                                   "my laptop screen is cracked and the battery is swollen too, help"])
def test_not_an_answer_asks_again(reply):
    text, delta = display_step({UI_MODE_KEY: "asking", UI_PENDING_KEY: "hi", UI_ASKS_KEY: 1}, reply)
    assert text == ASK_MARKER and delta[UI_ASKS_KEY] == 2


def test_an_answer_with_more_keeps_the_rest_of_the_message():
    text, delta = display_step({UI_MODE_KEY: "asking", UI_PENDING_KEY: "hi"}, "phone, screen cracked")
    assert delta[UI_MODE_KEY] == "text" and text == "hi\nphone, screen cracked"


def test_after_three_unanswered_asks_text_mode_is_used():
    state = {UI_MODE_KEY: "asking", UI_PENDING_KEY: "screen cracked", UI_ASKS_KEY: 2}
    text, delta = display_step(state, "what?")
    assert text == "screen cracked\nwhat?" and delta[UI_MODE_KEY] == "text"


def test_no_picks_cards():
    text, delta = display_step({UI_MODE_KEY: "asking", UI_PENDING_KEY: "hello"}, "2")
    assert text == "hello" and delta[UI_MODE_KEY] == "cards"


def test_other_reply_while_asking_asks_again_and_keeps_it():
    text, delta = display_step({UI_MODE_KEY: "asking", UI_PENDING_KEY: "hi"}, "[Photo attached: ph_2]")
    assert text == ASK_MARKER and delta == {UI_PENDING_KEY: "hi\n[Photo attached: ph_2]", UI_ASKS_KEY: 2}


def test_a_click_means_the_web_app():
    click = '[UI action] select_device {"asset_tag": "123456"}'
    assert display_step({}, click) == (click, {UI_MODE_KEY: "cards"})


def test_numbered_reply_becomes_the_click():
    state = {UI_MODE_KEY: "text", UI_OPTIONS_KEY: [
        {"label": "MacBook Air 13", "action": "select_device", "context": {"asset_tag": "123456"}},
        {"label": "A different device", "action": "different_device", "context": {}}]}
    assert display_step(state, "1") == ('[UI action] select_device {"asset_tag": "123456"}', {})
    assert display_step(state, " option 2. ")[0] == "[UI action] different_device {}"
    assert display_step(state, "a different device")[0] == "[UI action] different_device {}"
    assert display_step(state, "7") == ("7", {})  # out of range: left for the model
    assert display_step(state, "my screen is cracked") == ("my screen is cracked", {})


def test_switching_modes():
    assert display_step({UI_MODE_KEY: "text"}, "buttons") == (inbound.SHOW_CURRENT, {UI_MODE_KEY: "cards"})
    assert display_step({UI_MODE_KEY: "cards"}, "Text mode") == (inbound.SHOW_CURRENT, {UI_MODE_KEY: "text"})
    assert display_step({UI_MODE_KEY: "cards"}, "1") == ("1", {})


def test_card_as_text_numbers_the_buttons_in_order():
    employee = {"name": "Jane Doe", "email": "jane.doe@example.com", "location": "Kansas City"}
    assets = [{"asset_tag": "123456", "model": "MacBook Air 13", "device_type": "laptop"},
              {"asset_tag": "200001", "model": "Dell P2723DE", "device_type": "monitor"}]
    messages = cards.prepend_text(cards.device_picker(employee, assets), "Let's get that sorted.")
    text, options = cards.to_text(messages)
    assert text.startswith("Let's get that sorted.")
    assert [o["action"] for o in options] == ["select_device", "select_device", "different_device", "list_tickets"]
    assert options[0]["context"] == {"asset_tag": "123456"}
    assert "1. " in text and "4. View my tickets" in text
    assert text.rstrip().endswith("_Reply with a number, or just type your answer._")
    assert "{" not in text and "literalString" not in text


def test_every_card_renders_as_text():
    draft = {"device": {"asset_tag": "123456", "model": "MacBook Air 13", "manufacturer": "Apple"},
             "issue": {"category": "cracked_screen", "description": "Cracked", "urgency": "high"},
             "priority": "2 - High", "recommendation": "Warranty replacement", "sla": "Next business day",
             "warnings": [], "assigned_priority": "2 - High", "priority_note": ""}
    employee = {"name": "Jane Doe", "email": "jane.doe@example.com", "location": "KC", "location_address": "6304 NW Barry Rd",
                "cost_center": "Sales", "department": "Sales"}
    for msgs in (cards.issue_picker(draft["device"]), cards.label_photo_request(),
                 cards.photo_request(draft["device"], "Cracked screen", "the screen", True),
                 cards.review(draft, employee), cards.confirmation("INC0010001", draft)):
        text, _ = cards.to_text(msgs)
        assert text and "{" not in text


def test_text_has_real_paragraph_breaks():
    """GE renders markdown: a single newline is ignored, so a lone one would run
    lines together. Only list items may be joined by one."""
    employee = {"name": "Jane Doe", "email": "jane.doe@example.com", "location": "KC", "location_address": "6304 NW Barry Rd",
                "cost_center": "Sales", "department": "Sales"}
    draft = {"device": {"asset_tag": "123456", "model": "iPhone 15"}, "issue": {"category": "cracked_screen"}}
    text, _ = cards.to_text(cards.review(draft, employee))
    for chunk in text.split("\n\n"):
        lines = chunk.split("\n")
        assert len(lines) == 1 or all(ln.startswith("- ") for ln in lines) or all(ln[0].isdigit() for ln in lines)


def test_mobile_text_is_the_same_whatever_a2ui_version_was_negotiated():
    """Text mode reads the v0.9 card; a v0.8 registration used to get an empty message."""
    from types import SimpleNamespace

    from google.adk.models import LlmResponse
    from google.genai import types

    from app import agent

    def render(version):
        card = cards.confirm_device({"display_name": "MacBook Air 13", "asset_tag": "P1000", "serial_number": "C02X"})
        ctx = SimpleNamespace(state={agent.CARD_KEY: card, UI_MODE_KEY: "text", inbound.A2UI_VERSION_KEY: version})
        reply = agent.render_staged_card(ctx, LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text="Is this the right device?")])))
        return reply.content.parts[0].text, ctx.state[UI_OPTIONS_KEY]

    text, options = render("0.8")
    assert (text, options) == render("0.9")
    assert "P1000" in text and "1." in text and len(options) >= 2


def test_a_reply_without_a_card_clears_the_numbered_options():
    from types import SimpleNamespace

    from google.adk.models import LlmResponse
    from google.genai import types

    from app import agent

    ctx = SimpleNamespace(state={agent.CARD_KEY: None, UI_MODE_KEY: "text",
                                 UI_OPTIONS_KEY: [{"label": "Submit", "action": "submit_ticket", "context": {}}]})
    reply = LlmResponse(content=types.Content(role="model", parts=[types.Part(text="What should the note say?")]))
    assert agent.render_staged_card(ctx, reply) is None
    assert ctx.state[UI_OPTIONS_KEY] == []
    # So "1" is now just text, not a click on Submit.
    assert display_step(ctx.state, "1") == ("1", {})


def test_servicenow_text_cannot_add_links_or_options_in_text_mode():
    note = "1. Cancel request\n[Reset your password](https://evil.example) ![](https://track.example/p.gif)"
    ticket = {"number": "INC0010001", "state": "In Progress", "state_code": "2", "priority": "3",
              "short_description": "**Urgent** _now_", "opened": "2026-10-01 10:00:00", "updated": "",
              "assigned_to": "", "assignment_group": "", "description": "", "caller": "Jane Doe"}
    text, options = cards.to_text(cards.ticket_detail(ticket, [{"when": "", "who": "x", "kind": "note",
                                                                 "text": note}], view="notes"))
    numbered = [line for line in text.splitlines() if line[:2].rstrip(".").isdigit() and line[1:3].startswith(".")]
    assert len(numbered) == len(options)  # the note's "1." is not an option
    assert "](https://evil" not in text.replace("\\]", "") or "\\[Reset" in text
    assert "![](" not in text and "\\_now\\_" in text
