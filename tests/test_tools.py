from app.tools import _photo_mismatch, replace_ship_to

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

    monkeypatch.setattr(tools, "_own_ticket", own)
    monkeypatch.setattr(servicenow, "update_incident", update)
    monkeypatch.setattr(tools, "_show_ticket", show)

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
