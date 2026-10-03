"""Card wording is configuration: config/messages.yaml overrides the defaults, safely."""

import pytest
import yaml

from app import messages


def test_shipped_file_matches_the_defaults_and_loads():
    assert messages.load() == messages.DEFAULTS


def _write(tmp_path, data):
    path = tmp_path / "messages.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_a_reworded_message_is_used(tmp_path):
    texts = messages.load(_write(tmp_path, {"review.submit": "Send it", "done.title": "Ticket {number} is in"}))
    assert texts["review.submit"] == "Send it" and texts["done.title"].format(number="INC1") == "Ticket INC1 is in"
    assert texts["review.change"] == messages.DEFAULTS["review.change"]  # untouched keys keep the default


@pytest.mark.parametrize("data, error", [
    ({"review.sumbit": "Send"}, "unknown message"),
    ({"done.title": "Ticket {ticket} is in"}, "not allowed"),
    ({"done.title": "Ticket {} is in"}, "not allowed"),
    ({"done.title": "Ticket {number"}, "brace"),
    ({"review.submit": ""}, "non-empty"),
    (["not", "a", "mapping"], "key: text"),
])
def test_mistakes_are_named(tmp_path, data, error):
    with pytest.raises(messages.MessagesError, match=error):
        messages.load(_write(tmp_path, data))


def test_every_card_renders_with_reworded_messages(tmp_path, monkeypatch):
    """Swap every text for a marked version: no card may still contain a built-in English string."""
    from app import cards
    marked = {k: "X " + v for k, v in messages.DEFAULTS.items()}
    monkeypatch.setattr(messages, "current", lambda: marked)
    device = {"model": "MacBook Air", "asset_tag": "P1", "serial_number": "S1", "kind": "personal"}
    ticket = {"number": "INC1", "short_description": "x", "state": "New", "state_code": "1", "priority": "3",
              "opened": "2026-10-01 10:00:00", "updated": "2026-10-01 10:00:00", "description": "d"}
    rendered = [cards.device_picker({"name": "Jane Doe"}, [device]), cards.confirm_device(device),
                cards.issue_picker(device), cards.photo_request(device, "Cracked", "the screen", False),
                cards.label_photo_request(), cards.review({"device": device, "issue": {"category": "other"}}, {}),
                cards.confirmation("INC1", {"device": device}), cards.ticket_list([ticket], False, True),
                cards.ticket_detail(ticket, [], view="notes")]
    for messages_ in rendered:
        texts = [c["text"] for c in cards.components_of(messages_) if c["component"] == "Text"]
        assert any(t.startswith("X ") for t in texts)
        for english in ("Review your request", "Submit request", "Is this the right device?", "View my tickets",
                        "Back to my tickets", "What's wrong with it?"):
            assert english not in texts
