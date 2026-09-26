"""Every way a user can identify a device and describe a problem, through the
real wizard tools and the real ServiceNow client, against an in-memory Table API.

Only the HTTP call (`servicenow._request`), the photo model, photo upload to the
ticket and Memory Bank are faked. Row numbers match docs/BACKLOG.md A1.
"""

import itertools
from types import SimpleNamespace

import pytest

from app import cards, memory, servicenow, tools, vision
from app.vision import PhotoFindings

JANE, JOHN = "u_joe", "u_matt"


def _hw(sys_id, tag, serial, model, maker, owner, category="Computer"):
    return {"sys_id": sys_id, "asset_tag": tag, "serial_number": serial, "display_name": model,
            "model.display_name": model, "model.manufacturer.name": maker, "model_category.name": category,
            "purchase_date": "2025-01-15", "warranty_expiration": "2028-01-15", "install_status": "1",
            "assigned_to": {"value": owner}, "ci": {"value": f"ci_{sys_id}"}}


class FakeTableAPI:
    """The alm_hardware, incident and attachment calls servicenow.py makes."""

    def __init__(self):
        self.tables = {
            "alm_hardware": [
                _hw("a1", "123456", "FCPJ2GJTHC", "MacBook Air 13", "Apple", JANE),
                _hw("a2", "200001", "CN0ABC123", "Dell P2723DE", "Dell", JANE, "Computer Monitor"),
                _hw("a3", "IT-300001", "PF4XYZ99", "ThinkPad X1 Carbon Gen 11", "Lenovo", JOHN),
            ],
            "incident": [],
        }
        self.numbers = itertools.count(10001)

    @staticmethod
    def _match(row, query):
        for cond in query.split("^"):
            if not cond or cond.startswith("ORDERBY"):
                continue
            for op in ("!=", "IN", "="):
                if op in cond:
                    key, want = cond.split(op, 1)
                    have = servicenow._value(row.get(key, ""))
                    ok = {"!=": have != want, "IN": have in want.split(","), "=": have == want}[op]
                    if not ok:
                        return False
                    break
        return True

    async def __call__(self, method, path, *, params=None, json=None, **_):
        params = params or {}
        table = path.split("/api/now/table/")[-1].split("/")[0]
        if method == "GET":
            rows = [r for r in self.tables[table] if self._match(r, params.get("sysparm_query", ""))]
            return {"result": rows[: int(params.get("sysparm_limit", 1000))]}
        if method == "POST" and table == "incident":
            impact, urgency = int(json.get("impact", 2)), int(json.get("urgency", 2))
            row = {**json, "sys_id": f"i{len(self.tables['incident']) + 1}",
                   "number": f"INC00{next(self.numbers)}", "state": "1",
                   "priority": str(impact + urgency - 1), "cmdb_ci": {"value": json.get("cmdb_ci", "")}}
            self.tables["incident"].append(row)
            return {"result": dict(row)}
        if method == "PATCH":
            sys_id = path.rsplit("/", 1)[-1]
            row = next(r for r in self.tables["incident"] if r["sys_id"] == sys_id)
            comments = json.get("comments")
            row.update({k: v for k, v in json.items() if k != "comments"})
            if comments:
                row.setdefault("comments_log", []).append(comments)
            return {"result": dict(row)}
        return {"result": {}}


@pytest.fixture
def sn(monkeypatch):
    api = FakeTableAPI()
    monkeypatch.setattr(servicenow, "_request", api)

    async def recall(ctx, email):
        return []

    async def remember(ctx, email):
        return None

    async def attach(ctx, sys_id, draft):
        return None

    monkeypatch.setattr(memory, "recall", recall)
    monkeypatch.setattr(memory, "remember_conversation", remember)
    monkeypatch.setattr(tools, "_attach_photos", attach)
    return api


def ctx_for(user=JANE, session="sess-1"):
    profile = {"sys_id": user, "name": "Jane Doe", "email": "jane.doe@example.com",
               "location": "Kansas City", "location_address": "6304 Northwest Barry Road",
               "cost_center": "Sales", "department": "Sales"}
    return SimpleNamespace(state={"end_user": {"verified": True, "profile": profile}},
                           session=SimpleNamespace(id=session))


def send_photos(ctx, monkeypatch, *findings):
    """Stages photos as the A2A layer does and makes the photo model return `findings`."""
    by_uri = {}
    for i, f in enumerate(findings):
        pid = f"ph{len(ctx.state)}_{i}"
        uri = f"gs://bucket/{pid}.jpg"
        ctx.state[f"photo:{pid}"] = {"photo_id": pid, "uri": uri, "mime_type": "image/jpeg"}
        by_uri[uri] = f
    ctx.state["last_photo_ids"] = [p.rsplit("/", 1)[-1][:-4] for p in by_uri]

    async def analyze(uri, mime, hint=""):
        result = by_uri[uri]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(vision, "analyze_photo", analyze)


def label(**kw):
    return PhotoFindings(image_kind="label", **kw)


def damage(**kw):
    return PhotoFindings(image_kind="damage", damage_present=True, damage_description="Screen is cracked",
                         damage_severity="severe", issue_category="cracked_screen", supports_replacement=True, **kw)


def draft(ctx):
    return ctx.state["draft"]


def card_text(ctx):
    return cards.to_text(ctx.state[tools.CARD_KEY])[0]


# --- typed identification ------------------------------------------------------------


async def test_2_start_shows_only_my_devices(sn):
    ctx = ctx_for()
    result = await tools.start_request(ctx)
    assert result["step"] == "choose_device"
    assert [a["asset_tag"] for a in result["assets"]] == ["123456", "200001"]


@pytest.mark.parametrize("typed", ["123456", "#123456", " 123456 ", "Asset 123456"[6:]])
async def test_3_typed_asset_tag(sn, typed):
    ctx = ctx_for()
    result = await tools.select_device(typed, ctx)
    assert result["step"] == "describe_issue"
    assert draft(ctx)["device"]["asset_tag"] == "123456"


async def test_3_typed_asset_tag_is_case_insensitive(sn):
    ctx = ctx_for(JOHN)
    await tools.select_device("it-300001", ctx)
    assert draft(ctx)["device"]["asset_tag"] == "IT-300001"


@pytest.mark.parametrize("typed", ["FCPJ2GJTHC", "fcpj2gjthc", "FCPJ 2GJ THC"])
async def test_4_typed_serial(sn, typed):
    ctx = ctx_for()
    await tools.select_device(typed, ctx)
    assert draft(ctx)["device"]["serial_number"] == "FCPJ2GJTHC"


async def test_5_unknown_tag_asks_for_a_label_photo(sn):
    ctx = ctx_for()
    result = await tools.select_device("999999", ctx)
    assert result["status"] == "not_found"
    assert "device" not in (ctx.state.get("draft") or {})


async def test_6_someone_elses_device_is_flagged(sn):
    ctx = ctx_for()
    await tools.select_device("IT-300001", ctx)
    result = await tools.set_issue("wont_power_on", "Won't turn on", "normal", ctx)
    assert result["step"] == "review"
    assert any("assigned to someone else" in w for w in result["warnings"])


async def test_typed_tag_and_problem_in_one_go_reaches_review(sn):
    ctx = ctx_for()
    await tools.start_request(ctx)
    await tools.select_device("123456", ctx)
    result = await tools.set_issue("wont_power_on", "Dead since this morning", "high", ctx)
    assert result["step"] == "review" and result["priority"] == "2 - High"


# --- photos ---------------------------------------------------------------------------


async def test_7_asset_sticker_only(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(asset_tag="123456"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "describe_issue"
    assert draft(ctx)["device"]["model"] == "MacBook Air 13" and "evidence" not in draft(ctx)


async def test_8_maker_label_with_serial(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(manufacturer="Apple", model="MacBook Air", part_number="MGN63LL/A",
                                        serial_number="FCPJ2GJTHC"))
    await tools.analyze_photos(ctx)
    assert draft(ctx)["device"]["asset_tag"] == "123456"


async def test_9_maker_label_without_serial_offers_my_devices(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(manufacturer="Apple", model="MacBook Air", part_number="MGN63LL/A"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "device_unknown"  # backlog A3: auto-pick by model when only one fits
    assert [a["asset_tag"] for a in result["assets"]] == ["123456", "200001"]


async def test_10_damage_with_sticker_fills_device_and_evidence(sn, monkeypatch):
    ctx = ctx_for()
    await tools.start_request(ctx)
    await tools.set_issue("cracked_screen", "Dropped it", "normal", ctx)
    send_photos(ctx, monkeypatch, damage(asset_tag="123456", manufacturer="Apple", device_type="laptop"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "review"
    assert draft(ctx)["device"]["asset_tag"] == "123456"
    assert draft(ctx)["evidence"]["supports_replacement"] is True


async def test_11_damage_photo_of_a_different_make(sn, monkeypatch):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    send_photos(ctx, monkeypatch, damage(manufacturer="Lenovo", model="ThinkPad", device_type="laptop"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "review"
    assert any("Lenovo ThinkPad" in w for w in result["warnings"])


async def test_12_damage_photo_with_nothing_selected(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, damage(device_type="laptop"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "device_unknown"
    assert draft(ctx)["evidence"]["category"] == "cracked_screen"


async def test_13_label_serial_differs_from_inventory(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(asset_tag="123456", serial_number="FCPJ2GJTHX"))
    await tools.analyze_photos(ctx)
    assert any("differs from inventory" in w for w in draft(ctx)["photo_warnings"])


async def test_14_photo_of_another_of_my_devices_offers_a_switch(sn, monkeypatch):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    send_photos(ctx, monkeypatch, label(asset_tag="200001"))
    result = await tools.analyze_photos(ctx)
    assert any("shows asset 200001" in w for w in result["warnings"])
    assert "Use Dell P2723DE" in card_text(ctx)

    option = next(o for o in cards.to_text(ctx.state[tools.CARD_KEY])[1] if o["label"].startswith("Use "))
    result = await tools.select_device(option["context"]["asset_tag"], ctx)
    assert draft(ctx)["device"]["asset_tag"] == "200001"
    assert not any("shows asset" in w for w in result["warnings"])


async def test_15_unreadable_photo_leaves_the_draft_alone(sn, monkeypatch):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    before = dict(draft(ctx))
    send_photos(ctx, monkeypatch, RuntimeError("model failed"))
    result = await tools.analyze_photos(ctx)
    assert result["status"] == "error" and draft(ctx) == before


async def test_15_unrelated_photo_changes_nothing_important(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, PhotoFindings(image_kind="unrelated"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "device_unknown" and "evidence" not in draft(ctx)


async def test_16_label_and_damage_photos_together(sn, monkeypatch):
    ctx = ctx_for()
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    send_photos(ctx, monkeypatch, label(asset_tag="123456"), damage(device_type="laptop", manufacturer="Apple"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "review"
    assert draft(ctx)["device"]["asset_tag"] == "123456" and draft(ctx)["evidence"]
    assert len(draft(ctx)["photos"]) == 2


async def test_19_device_not_in_inventory_is_filed_without_a_ci(sn, monkeypatch):
    ctx = ctx_for()
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    send_photos(ctx, monkeypatch, label(asset_tag="999999", serial_number="ZZZ111", manufacturer="HP",
                                        model="EliteBook 840", device_type="laptop"))
    result = await tools.analyze_photos(ctx)
    assert draft(ctx)["device"]["in_inventory"] is False
    assert any("not found in inventory" in w for w in result["warnings"])
    await tools.submit_ticket(ctx)
    assert sn.tables["incident"][0]["cmdb_ci"] == {"value": ""}


# --- submitting -----------------------------------------------------------------------


async def _file(ctx, tag="123456"):
    await tools.select_device(tag, ctx)
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    return await tools.submit_ticket(ctx)


async def test_submit_links_the_device_and_the_caller(sn):
    result = await _file(ctx_for())
    ticket = sn.tables["incident"][0]
    assert result["status"] == "submitted"
    assert ticket["caller_id"] == JANE and ticket["cmdb_ci"] == {"value": "ci_a1"}


async def test_submit_twice_files_once(sn):
    ctx = ctx_for()
    await _file(ctx)
    again = await tools.submit_ticket(ctx)
    assert again["status"] == "already_submitted" and len(sn.tables["incident"]) == 1


async def test_retried_turn_finds_the_ticket_it_already_filed(sn):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    saved = dict(draft(ctx))
    first = await tools.submit_ticket(ctx)
    ctx.state["draft"] = saved  # the turn's state was lost, as when a reply fails and GE retries
    second = await tools.submit_ticket(ctx)
    assert second["ticket"] == first["ticket"] and len(sn.tables["incident"]) == 1


async def test_20_second_request_in_the_same_conversation_files_a_new_ticket(sn):
    ctx = ctx_for()
    first = await _file(ctx)
    await tools.start_request(ctx)  # "Start another request"
    second = await _file(ctx, "200001")
    assert second["status"] == "submitted" and second["ticket"] != first["ticket"]
    assert len(sn.tables["incident"]) == 2


async def test_17_photo_after_a_filed_request_starts_a_new_one(sn, monkeypatch):
    ctx = ctx_for()
    first = await _file(ctx)
    send_photos(ctx, monkeypatch, label(asset_tag="200001"))
    await tools.analyze_photos(ctx)
    assert "submitted_number" not in draft(ctx)
    assert draft(ctx)["device"]["asset_tag"] == "200001"
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    second = await tools.submit_ticket(ctx)
    assert second["status"] == "submitted" and second["ticket"] != first["ticket"]


async def test_typed_problem_after_a_filed_request_starts_a_new_one(sn):
    ctx = ctx_for()
    await _file(ctx)
    await tools.set_issue("battery", "Battery swelling", "normal", ctx)
    assert "submitted_number" not in draft(ctx) and "device" not in draft(ctx)


async def test_required_photo_blocks_submit(sn):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    result = await tools.submit_ticket(ctx)
    assert result["step"] == "photo" and not sn.tables["incident"]
