from app.tools import replace_ship_to, tickets
from app.tools.review import _photo_mismatch

THINKPAD = {"in_inventory": True, "manufacturer": "Lenovo", "model": "ThinkPad X1 Carbon Gen 11", "device_type": "laptop"}


def _draft(findings):
    return {"device": THINKPAD, "photos": [{"findings": findings}]}


def test_photo_of_a_different_make_is_flagged():
    warning = _photo_mismatch(_draft({"manufacturer": "Apple", "model": "MacBook Air", "device_type": "laptop"}))
    assert "Apple MacBook Air" in warning and "ThinkPad" in warning


def test_photo_of_a_different_kind_of_device_is_flagged():
    assert _photo_mismatch(_draft({"manufacturer": "", "model": "", "device_type": "monitor"}))


def test_matching_or_unrecognized_photo_is_not_flagged():
    assert _photo_mismatch(_draft({"manufacturer": "Lenovo", "model": "", "device_type": "laptop"})) == ""
    assert _photo_mismatch(_draft({"manufacturer": "", "model": "", "device_type": ""})) == ""


def test_ship_to_line_is_replaced():
    desc = "Problem: x\nShip to: 1 Old St, Kansas City MO\nBill to: IT"
    assert replace_ship_to(desc, "9 New Ave, Denver CO") == "Problem: x\nShip to: 9 New Ave, Denver CO\nBill to: IT"
    assert replace_ship_to("no address here", "x") is None


async def test_change_not_applied_is_noted_and_reported(monkeypatch):
    from app import servicenow, tools

    ticket = {"sys_id": "s1", "number": "INC1", "state": "Resolved", "state_code": "6", "priority": "3",
              "urgency": "3", "impact": "2", "description": "Ship to: 1 Old St", "short_description": "x",
              "opened": "", "updated": "", "assignment_group": "", "assigned_to": ""}
    patches = []

    async def own(ctx, number):
        return {"sys_id": "me"}, dict(ticket)

    async def update(user, number, fields):
        patches.append(fields)
        return dict(ticket)  # ServiceNow ignores the state change (policy)

    async def show(ctx, t, note=""):
        return {"note": note}

    monkeypatch.setattr(tickets, "_own_ticket", own)
    monkeypatch.setattr(servicenow, "update_incident", update)
    monkeypatch.setattr(tickets, "_show_ticket", show)

    result = await tools.update_ticket("INC1", object(), note="tracking number doesn't work", status="In Progress")
    assert result["changed"] == []
    assert result["not_permitted_note_added"] == [{"change": "Status", "value": "In Progress"}]
    assert patches[0]["state"] == "2" and patches[0]["comments"] == "tracking number doesn't work"
    assert "Status: In Progress" in patches[1]["comments"]  # the request is recorded for the desk
    assert "Not changed due to ServiceNow policy" in result["ticket"]["note"]



def test_extracted_delivery_memories_are_not_shown():
    from app import memory
    assert memory._DELIVERY_WORDS.search("Last time the laptop was shipped to a Marriott hotel in Chicago")
    assert not memory._DELIVERY_WORDS.search("Prefers to be contacted by text message")


def test_addresses_in_any_script_stay_distinct():
    from app import memory
    n = memory.normalize_address
    assert n("東京都港区1-2-3") != n("大阪府北区1-2-3")
    assert n("8 Avenue des Champs-Élysées, Paris") == n("8 avenue des champs élysées paris")
    assert n("742 Evergreen Terrace, Kansas City") == n("742  EVERGREEN TERRACE kansas city")


def test_a_suite_is_an_office_not_a_hotel():
    from app import tools
    assert not tools._TEMPORARY.search("100 Main St Suite 200, Denver, CO 80202")
    assert tools._TEMPORARY.search("Embassy Suites, 1 Hotel Way") and tools._TEMPORARY.search("Hilton Downtown")


def test_unsegmented_addresses_are_addresses():
    from app import tools
    assert tools._looks_like_street_address("東京都港区芝公園1-2-3")
    assert not tools._looks_like_street_address("my house") and not tools._looks_like_street_address("Room 12")


async def test_recall_filters_before_limiting(monkeypatch):
    from types import SimpleNamespace

    from app import memory

    def mem(text):
        return SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))

    deliveries = [mem(f"Shipped to the hotel in city {i}") for i in range(10)]
    service = SimpleNamespace(search_memory=None)

    async def search_memory(**kw):
        return SimpleNamespace(memories=deliveries + [mem("Prefers text messages"), mem("Has a MacBook Air")])

    service.search_memory = search_memory
    monkeypatch.setattr(memory, "_service", lambda ctx: service)
    facts = await memory.recall(SimpleNamespace(), "jane.doe@example.com")
    assert facts == ["Prefers text messages", "Has a MacBook Air"]
