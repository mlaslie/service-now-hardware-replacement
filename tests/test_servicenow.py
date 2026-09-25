"""Ticket reads and writes must always be scoped to the caller's own hardware incidents."""

import pytest

from app import servicenow


@pytest.fixture
def calls(monkeypatch):
    log = []

    async def fake_request(method, path, **kw):
        log.append((method, path, kw))
        if method == "GET" and path == "/api/now/table/incident":
            q = kw["params"]["sysparm_query"]
            # Only the caller's own INC0000001 exists.
            if "caller_id=me" in q and "number=INC0000001" in q:
                return {"result": [{"sys_id": "s1", "number": "INC0000001", "state": "1"}]}
            return {"result": []}
        return {"result": {"sys_id": "s1", "number": "INC0000001", "state": "2"}}

    monkeypatch.setattr(servicenow, "_request", fake_request)
    return log


async def test_every_query_is_scoped_to_caller_and_hardware(calls):
    await servicenow.my_incidents("me")
    await servicenow.my_incident("me", "INC0000001")
    for method, path, kw in calls:
        q = kw["params"]["sysparm_query"]
        assert "caller_id=me" in q and "category=hardware" in q


async def test_someone_elses_ticket_is_not_found_and_not_updated(calls):
    assert await servicenow.my_incident("me", "INC0000999") is None
    assert await servicenow.update_incident("me", "INC0000999", {"comments": "x"}) is None
    assert not [c for c in calls if c[0] == "PATCH"]


async def test_own_ticket_is_updated_by_sys_id(calls):
    ticket = await servicenow.update_incident("me", "inc0000001", {"comments": "new address"})
    assert ticket["number"] == "INC0000001"
    assert ("PATCH", "/api/now/table/incident/s1") in [(m, p) for m, p, _ in calls]


def test_asset_type_from_model_category():
    asset = servicenow._asset({"model_category.name": "Computer Laptop", "asset_tag": "P1",
                               "assigned_to": {"value": "u1", "link": "x"}})
    assert asset["device_type"] == "laptop" and asset["assigned_to"] == "u1"


def test_laptops_filed_under_computer_are_laptops():
    assert servicenow._device_type("Computer", 'Apple MacBook Pro 15"') == "laptop"
    assert servicenow._device_type("Computer", "Dell OptiPlex 7090") == "desktop"
    assert servicenow._device_type("Computer Monitor", "Dell U2723QE") == "monitor"
    assert servicenow._device_type("Mobile Device", "Apple iPhone 15") == "phone"


def test_journal_is_split_into_entries_newest_first():
    text = ("2026-09-25 18:10:00 - John Doe (Additional comments)\nShip to home please\n\n"
            "2026-09-25 17:55:00 - System Administrator (Additional comments)\nRequested priority 2\n")
    entries = servicenow.parse_journal(text)
    assert [e["who"] for e in entries] == ["John Doe", "System Administrator"]
    assert entries[0]["text"] == "Ship to home please" and entries[0]["kind"] == "note"


def test_journal_tolerates_crlf_and_12_hour_times():
    text = "09/25/2026 06:10:00 PM - John Doe (Additional comments)\r\nCall me first\r\n"
    entries = servicenow.parse_journal(text)
    assert len(entries) == 1 and entries[0]["text"] == "Call me first"


def test_unrecognized_journal_still_shows_text():
    assert servicenow.parse_journal("something unexpected")[0]["text"] == "something unexpected"
    assert servicenow.parse_journal("") == []


def test_journal_with_comments_label():
    text = ("2026-09-25 13:16:22 - John Doe (Comments)\nhurry up and ship me a new laptop\n\n"
            "2026-09-25 13:14:18 - John Doe (Comments)\nRequested priority 1 - Critical; please review.\n")
    entries = servicenow.parse_journal(text)
    assert [e["text"] for e in entries] == ["hurry up and ship me a new laptop",
                                            "Requested priority 1 - Critical; please review."]
    assert entries[0]["who"] == "John Doe" and entries[0]["kind"] == "note"
