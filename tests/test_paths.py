"""Every way a user can identify a device and describe a problem, through the
real wizard tools and the real ServiceNow client, against an in-memory Table API.

Only the HTTP call (`servicenow._request`), the photo model, photo upload to the
ticket and Memory Bank are faked. Row numbers match docs/BACKLOG.md A1.
"""

import itertools
from types import SimpleNamespace

import pytest

from app import cards, memory, servicenow, tools, vision
from app.tools import filing
from app.vision import PhotoFindings

import re

JANE, JOHN, ANA = "u_jane", "u_john", "u_ana"
RADIOLOGY, IT, ED = "d_rad", "d_it", "d_ed"
IMAGING, CLINICAL_ENG, SERVICE_DESK = "g_img", "g_ce", "g_sd"
HOSPITAL = "Riverside Medical Center"
NAMES = {RADIOLOGY: "Radiology", IT: "IT", ED: "Emergency Department",
         IMAGING: "Imaging Engineering", CLINICAL_ENG: "Clinical Engineering", SERVICE_DESK: "Service Desk",
         JANE: "Jane Doe", JOHN: "John Doe", ANA: "Ana Ruiz"}


def _hw(sys_id, tag, serial, model, maker, owner="", category="Computer", dept="", location="", group=""):
    return {"sys_id": sys_id, "asset_tag": tag, "serial_number": serial, "display_name": model,
            "model.display_name": model, "model.manufacturer.name": maker, "model_category.name": category,
            "purchase_date": "2025-01-15", "warranty_expiration": "2028-01-15", "install_status": "1",
            "assigned_to": {"value": owner}, "assigned_to.name": NAMES.get(owner, ""), "ci": {"value": f"ci_{sys_id}"},
            "department": {"value": dept}, "department.name": NAMES.get(dept, ""),
            "location": {"value": f"loc_{sys_id}" if location else ""}, "location.name": location,
            "support_group": {"value": group}, "support_group.name": NAMES.get(group, ""),
            "cost_center.name": NAMES.get(dept, ""), "managed_by": {"value": ""}}


class FakeTableAPI:
    """The asset, incident and attachment calls servicenow.py makes, with encoded
    queries (^, ^OR, =, !=, LIKE, IN, ISEMPTY) evaluated like ServiceNow does."""

    def __init__(self):
        self.tables = {
            "alm_hardware": [
                _hw("a1", "123456", "FCPJ2GJTHC", "MacBook Air 13", "Apple", JANE),
                _hw("a2", "200001", "CN0ABC123", "Dell P2723DE", "Dell", JANE, "Computer Monitor"),
                _hw("a3", "IT-300001", "PF4XYZ99", "ThinkPad X1 Carbon Gen 11", "Lenovo", JOHN),
                _hw("a4", "300002", "C02ANA0001", "MacBook Pro 14", "Apple", ANA, dept=ED),
                _hw("a10", "CE-10421", "GEMR15E0421", "SIGNA Explorer 1.5T MRI", "GE HealthCare",
                    category="Imaging Equipment", dept=RADIOLOGY, group=IMAGING,
                    location=f"{HOSPITAL} - Radiology - MRI Suite 1 (Room 104)"),
                _hw("a11", "CE-20457", "BD8015PC0457", "Alaris 8015 PC Unit Infusion Pump", "BD",
                    category="Patient Care Equipment", dept=ED, group=CLINICAL_ENG,
                    location=f"{HOSPITAL} - Emergency Department - Bay 7"),
                _hw("a12", "610204", "DPR3680RW2", "Precision 3680 Reading Workstation", "Dell",
                    dept=RADIOLOGY, group=SERVICE_DESK, location=f"{HOSPITAL} - Radiology - Reading Room 2"),
            ],
            "incident": [],
            # The pump's category has no CI class, so ServiceNow keeps the link on the CI only.
            "cmdb_ci": [{"sys_id": "ci_a11", "asset": {"value": "a11"}}],
        }
        self.tables["alm_hardware"][5]["ci"] = {"value": ""}
        self.numbers = itertools.count(10001)
        self.refuse: set[str] = set()

    _COND = re.compile(r"^([a-z_.]+?)(ISEMPTY|ISNOTEMPTY|!=|LIKE|IN|=)(.*)$")

    def _test(self, row, cond):
        field, op, want = self._COND.match(cond).groups()
        have = str(servicenow._value(row.get(field, "")))
        return {"ISEMPTY": not have, "ISNOTEMPTY": bool(have), "!=": have != want, "=": have == want,
                "LIKE": want.lower() in have.lower(), "IN": have in want.split(",")}[op]

    def _match(self, row, query):
        clauses: list[list[str]] = []
        for cond in query.split("^"):
            if not cond or cond.startswith("ORDERBY"):
                continue
            if cond.startswith("OR") and clauses:
                clauses[-1].append(cond[2:])
            else:
                clauses.append([cond])
        return all(any(self._test(row, c) for c in alts) for alts in clauses)

    def _row(self, table, sys_id):
        return next(r for r in self.tables[table] if r["sys_id"] == sys_id)

    async def __call__(self, method, path, *, params=None, json=None, **_):
        params = params or {}
        parts = path.split("/api/now/table/")[-1].split("/")
        table, sys_id = parts[0], (parts[1] if len(parts) > 1 else "")
        if method == "GET" and sys_id:
            return {"result": dict(self._row(table, sys_id))}
        if method == "GET":
            # A table this fake doesn't model (e.g. the journal) has no rows, as for a user who can't see it.
            rows = [r for r in self.tables.get(table, []) if self._match(r, params.get("sysparm_query", ""))]
            return {"result": [dict(r) for r in rows[: int(params.get("sysparm_limit", 1000))]]}
        if method == "POST" and table == "incident":
            json = {k: v for k, v in json.items() if k not in self.refuse}  # as ServiceNow drops fields silently
            impact, urgency = int(json.get("impact", 2)), int(json.get("urgency", 2))
            row = {**json, "sys_id": f"i{len(self.tables['incident']) + 1}",
                   "number": f"INC00{next(self.numbers)}", "state": "1", "sys_created_on": "2026-09-26 10:00:00",
                   "priority": str(impact + urgency - 1), "cmdb_ci": {"value": json.get("cmdb_ci", "")},
                   "caller_id": {"value": json["caller_id"]}, "caller_id.name": NAMES.get(json["caller_id"], ""),
                   "assignment_group": {"value": json.get("assignment_group", "")},
                   "assignment_group.name": NAMES.get(json.get("assignment_group", ""), ""),
                   "watch_list": json.get("watch_list", ""), "comments_log": []}
            self.tables["incident"].append(row)
            return {"result": dict(row)}
        if method == "PATCH":
            row = self._row(table, sys_id)
            comments = json.get("comments")
            row.update({k: v for k, v in json.items() if k != "comments"})
            if comments:
                row["comments_log"].append(comments)
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

    async def saved_addresses(email):
        return list(api.saved.get(email, []))

    async def save_address(email, label, address):
        api.saved.setdefault(email, []).append({"label": label, "address": address})
        api.saved_calls.append((email, label, address))

    api.saved, api.saved_calls = {}, []
    monkeypatch.setattr(memory, "recall", recall)
    monkeypatch.setattr(memory, "remember_conversation", remember)
    monkeypatch.setattr(memory, "saved_addresses", saved_addresses)
    monkeypatch.setattr(memory, "save_address", save_address)
    monkeypatch.setattr(filing, "_attach_photos", attach)
    return api


PROFILES = {
    JANE: {"name": "Jane Doe", "email": "jane.doe@example.com", "title": "MRI Technologist",
          "department": "Radiology", "department_id": RADIOLOGY, "group_ids": []},
    JOHN: {"name": "John Doe", "email": "john.doe@example.com", "title": "IT Systems Analyst",
           "department": "IT", "department_id": IT, "group_ids": [SERVICE_DESK]},
}


def ctx_for(user=JANE, session="sess-1"):
    profile = {"sys_id": user, "location": HOSPITAL, "location_address": "1200 Harbor Health Way",
               "cost_center": "Radiology", **PROFILES[user]}
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


async def pick(ctx, tag):
    """Choose a device from the list or by typing it, then say yes to "Is this the right device?"."""
    result = await tools.select_device(tag, ctx)
    assert result["step"] == "confirm_device", result
    return await tools.confirm_device(True, ctx)


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
    assert result["step"] == "confirm_device"
    assert draft(ctx)["device"]["asset_tag"] == "123456"
    assert "Is this the right device?" in card_text(ctx) and "FCPJ2GJTHC" in card_text(ctx)
    assert (await tools.confirm_device(True, ctx))["step"] == "describe_issue"


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


async def test_6_someone_elses_device_can_be_reported_and_says_so(sn):
    ctx = ctx_for()
    await pick(ctx, "IT-300001")
    assert draft(ctx)["device"]["relation"] == "unconfirmed"
    assert "John Doe, not you" in draft(ctx)["device"]["relation_text"]
    result = await tools.set_issue("wont_power_on", "Won't turn on", "normal", ctx)
    assert result["step"] == "review"
    assert "Belongs to:** John Doe, not you" in card_text(ctx) and card_text(ctx).count("not you") == 1
    await tools.submit_ticket(ctx)
    ticket = sn.tables["incident"][0]
    assert ticket["watch_list"] == JOHN  # the owner hears about it
    assert "Ownership could not be confirmed: assigned to John Doe" in ticket["description"]


async def test_typed_tag_and_problem_in_one_go_confirms_then_reviews(sn):
    ctx = ctx_for()
    await tools.start_request(ctx)
    await tools.select_device("123456", ctx)
    result = await tools.set_issue("wont_power_on", "Dead since this morning", "high", ctx)
    assert result["step"] == "confirm_device"  # the problem is kept while they check the device
    result = await tools.confirm_device(True, ctx)
    assert result["step"] == "review" and result["priority"] == "2 - High"


async def test_saying_no_to_the_device_goes_back_to_the_list(sn):
    ctx = ctx_for()
    await tools.select_device("123456", ctx)
    result = await tools.confirm_device(False, ctx)
    assert result["step"] == "choose_device" and "device" not in draft(ctx)


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


async def test_9_maker_label_without_serial_matches_my_device_by_model(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(manufacturer="Apple", model="MacBook Air", part_number="MGN63LL/A"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "confirm_device"  # a guess, so the user checks it
    assert draft(ctx)["device"]["asset_tag"] == "123456"
    assert "by its model" in card_text(ctx)


async def test_9_unknown_model_still_offers_my_devices(sn, monkeypatch):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(manufacturer="HP", model="EliteBook 840"))
    result = await tools.analyze_photos(ctx)
    assert result["step"] == "device_unknown"
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
    await pick(ctx, "123456")
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
    await pick(ctx, "123456")
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    send_photos(ctx, monkeypatch, label(asset_tag="200001"))
    result = await tools.analyze_photos(ctx)
    assert any("shows asset 200001" in w for w in result["warnings"])
    assert "Use Dell P2723DE" in card_text(ctx)

    option = next(o for o in cards.to_text(ctx.state[tools.CARD_KEY])[1] if o["label"].startswith("Use "))
    await tools.select_device(option["context"]["asset_tag"], ctx)
    result = await tools.confirm_device(True, ctx)
    assert draft(ctx)["device"]["asset_tag"] == "200001"
    assert not any("shows asset" in w for w in result["warnings"])


async def test_15_unreadable_photo_leaves_the_draft_alone(sn, monkeypatch):
    ctx = ctx_for()
    await pick(ctx, "123456")
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
    await pick(ctx, tag)
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    return await tools.submit_ticket(ctx)


async def test_a_failing_note_after_filing_still_reports_the_ticket(sn, monkeypatch):
    """Notes are added after the ticket exists; if one fails (refused, timeout), the user is still
    told the ticket number, and submitting again never files a second ticket."""
    async def broken(*a, **kw):
        raise servicenow.ServiceNowError("comments refused")

    monkeypatch.setattr(servicenow, "update_incident", broken)
    monkeypatch.setattr(servicenow, "dropped_fields", broken)
    ctx = ctx_for()
    await pick(ctx, "123456")
    await tools.set_issue("wont_power_on", "Dead", "critical", ctx)
    result = await tools.submit_ticket(ctx)
    assert result["status"] == "submitted" and result["ticket"] == sn.tables["incident"][0]["number"]
    again = await tools.submit_ticket(ctx)
    assert again["status"] == "already_submitted" and len(sn.tables["incident"]) == 1


async def test_submit_links_the_device_and_the_caller(sn):
    result = await _file(ctx_for())
    ticket = sn.tables["incident"][0]
    assert result["status"] == "submitted"
    assert ticket["caller_id"] == {"value": JANE} and ticket["cmdb_ci"] == {"value": "ci_a1"}
    assert "Ownership" not in ticket["description"]  # nothing to explain for your own device


async def test_submit_twice_files_once(sn):
    ctx = ctx_for()
    await _file(ctx)
    again = await tools.submit_ticket(ctx)
    assert again["status"] == "already_submitted" and len(sn.tables["incident"]) == 1


async def test_retried_turn_finds_the_ticket_it_already_filed(sn):
    ctx = ctx_for()
    await pick(ctx, "123456")
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
    await pick(ctx, "123456")
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    result = await tools.submit_ticket(ctx)
    assert result["step"] == "photo" and not sn.tables["incident"]


# --- fuzzy matching against the user's own devices (A3) ----------------------------


@pytest.mark.parametrize("read", ["SFCPJ2GJTHC", "FCPJZGJTHC", "FCPJ2GJ7HC", "FCPJ2GJTH", "fcpj-2gjt-hc"])
async def test_18_misread_serial_matches_my_device(sn, monkeypatch, read):
    ctx = ctx_for()
    send_photos(ctx, monkeypatch, label(serial_number=read))
    await tools.analyze_photos(ctx)
    assert draft(ctx)["device"]["asset_tag"] == "123456"
    assert draft(ctx)["device"]["in_inventory"] is True
    assert not draft(ctx).get("photo_warnings")  # no "serial differs" noise for a fuzzy match


async def test_typed_serial_with_a_typo_matches_and_says_so(sn):
    ctx = ctx_for()
    await tools.select_device("FCPJ2GJ7HC", ctx)
    assert "by its serial number" in card_text(ctx)
    await tools.confirm_device(True, ctx)
    result = await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    assert draft(ctx)["device"]["asset_tag"] == "123456"
    assert any("by its serial number" in w for w in result["warnings"])


async def test_exact_match_has_no_match_note(sn):
    ctx = ctx_for()
    await pick(ctx, "123456")
    result = await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    assert not any("Matched to" in w for w in result["warnings"])


async def test_fuzzy_match_never_reaches_someone_elses_device(sn):
    ctx = ctx_for()  # Jane; PF4XYZ99 belongs to John, outside Jane's department
    result = await tools.select_device("PF4XYZ98", ctx)
    assert result["status"] == "not_found"


async def test_fuzzy_match_does_not_replace_a_chosen_device(sn, monkeypatch):
    ctx = ctx_for()
    await pick(ctx, "200001")
    send_photos(ctx, monkeypatch, label(serial_number="FCPJ2GJ7HC"))
    await tools.analyze_photos(ctx)
    assert draft(ctx)["device"]["asset_tag"] == "200001" and "suggested_device" not in draft(ctx)


def test_match_own_asset_needs_a_single_candidate():
    macs = [{"asset_tag": "1", "serial_number": "C02AAA111", "model": "MacBook Air 13", "manufacturer": "Apple"},
            {"asset_tag": "2", "serial_number": "C02AAA112", "model": "MacBook Air 15", "manufacturer": "Apple"}]
    assert tools.match_own_asset(macs, model="MacBook Air") == (None, "")
    assert tools.match_own_asset(macs, identifier="C02AAA11X") == (None, "")  # one edit from both
    assert tools.match_own_asset(macs, model="MacBook Air 15")[0]["asset_tag"] == "2"
    assert tools.match_own_asset(macs, model="MacBook Air 15", manufacturer="Dell") == (None, "")
    assert tools.match_own_asset(macs, identifier="C02") == (None, "")  # too short to guess


# --- hospital equipment ---------------------------------------------------------------


async def _report_mri(ctx):
    await pick(ctx, "CE-10421")
    await tools.set_issue("error_alarm", "Gradient coil error on the console", "normal", ctx)
    await tools.skip_photo(ctx)
    return await tools.submit_ticket(ctx)


async def test_department_equipment_by_tag(sn):
    ctx = ctx_for(JANE)
    result = await tools.select_device("ce-10421", ctx)
    assert result["step"] == "confirm_device" and result["belongs_to"] == "Radiology (your department)"
    text = card_text(ctx)
    assert "MRI Suite 1 (Room 104)" in text and "GEMR15E0421" in text
    result = await tools.confirm_device(True, ctx)
    assert result["step"] == "describe_issue"
    assert "Error message or alarm" in card_text(ctx) and "Cracked" not in card_text(ctx)  # equipment choices


async def test_department_equipment_is_routed_and_located(sn):
    ctx = ctx_for(JANE)
    await pick(ctx, "CE-10421")
    await tools.set_issue("error_alarm", "Gradient coil error on the console", "normal", ctx)
    result = await tools.skip_photo(ctx)
    assert result["step"] == "review"
    text = card_text(ctx)
    assert "**Service:** On-site repair" in text and "**Handled by:** Imaging Engineering" in text
    assert "Ship to" not in text
    await tools.submit_ticket(ctx)
    ticket = sn.tables["incident"][0]
    assert ticket["assignment_group"] == {"value": IMAGING} and ticket["location"] == "loc_a10"
    assert ticket["cmdb_ci"] == {"value": "ci_a10"}
    assert ticket["short_description"].endswith("MRI Suite 1 (Room 104)")
    assert "Department equipment (Radiology); the reporter is in this department" in ticket["description"]
    assert not ticket["comments_log"]  # nothing for the desk to sort out


async def test_find_equipment_by_what_people_call_it(sn):
    ctx = ctx_for(JANE)
    result = await tools.find_device("the MRI in room 104", ctx)
    assert result["step"] == "confirm_device" and draft(ctx)["device"]["asset_tag"] == "CE-10421"


async def test_vague_description_offers_the_matches(sn):
    ctx = ctx_for(JANE)
    result = await tools.find_device("radiology", ctx)
    assert result["step"] == "choose_device"
    assert {m["asset_tag"] for m in result["matches"]} == {"CE-10421", "610204"}


async def test_equipment_elsewhere_is_found_and_ownership_noted(sn):
    ctx = ctx_for(JANE)  # Radiology, reporting an ED pump
    result = await tools.find_device("infusion pump in ED bay 7", ctx)
    assert result["step"] == "confirm_device" and draft(ctx)["device"]["asset_tag"] == "CE-20457"
    assert draft(ctx)["device"]["relation"] == "unconfirmed"
    await tools.confirm_device(True, ctx)
    await tools.set_issue("damaged", "Door latch snapped off", "normal", ctx)
    await tools.skip_photo(ctx)
    await tools.submit_ticket(ctx)
    ticket = sn.tables["incident"][0]
    assert ticket["assignment_group"] == {"value": CLINICAL_ENG}
    assert ticket["cmdb_ci"] == {"value": "ci_a11"}  # found through the CI's asset reference
    assert "Ownership could not be confirmed: registered to Emergency Department" in ticket["comments_log"][0]


async def test_equipment_my_group_supports(sn):
    ctx = ctx_for(JOHN)  # IT, member of Service Desk
    await tools.select_device("610204", ctx)
    assert draft(ctx)["device"]["relation"] == "group"
    assert "supported by your group (Service Desk)" in card_text(ctx)
    assert draft(ctx)["device"]["kind"] == "shared"


async def test_nothing_found_asks_for_the_tag(sn):
    result = await tools.find_device("the thing", ctx_for(JANE))
    assert result["status"] == "not_found"


async def test_safety_concern_is_critical_and_says_what_to_do(sn):
    ctx = ctx_for(JANE)
    await pick(ctx, "CE-10421")
    result = await tools.set_issue("safety_concern", "Table moved on its own with a patient on it", "normal", ctx)
    assert result["step"] == "review"  # no photo step for a safety concern
    assert result["priority"] == "1 - Critical"
    assert "take it out of service" in card_text(ctx).lower()
    await tools.submit_ticket(ctx)
    assert "SAFETY CONCERN" in sn.tables["incident"][0]["description"]


async def test_second_reporter_joins_the_open_ticket_and_gets_updates(sn):
    jane = ctx_for(JANE)
    first = await _report_mri(jane)
    john = ctx_for(JOHN, session="sess-2")
    await tools.select_device("CE-10421", john)
    result = await tools.confirm_device(True, john)
    assert result["step"] == "already_reported" and result["open_tickets"][0]["number"] == first["ticket"]
    assert "Reported by Jane Doe" in card_text(john)

    result = await tools.follow_ticket(first["ticket"], john)
    assert result["following"] is True and len(sn.tables["incident"]) == 1
    assert sn.tables["incident"][0]["watch_list"] == JOHN
    assert "Also reported by John Doe" in sn.tables["incident"][0]["comments_log"][-1]

    listed = await tools.list_my_tickets(john)
    assert listed["tickets"][0]["number"] == first["ticket"]
    assert "you're following" in card_text(john)
    assert (await tools.get_ticket(first["ticket"], john))["ticket"]["number"] == first["ticket"]
    cancel = await tools.cancel_ticket(first["ticket"], "works now", john)
    assert cancel["status"] == "not_permitted"  # only Jane, who reported it, can cancel


@pytest.mark.parametrize("change", [{"status": "Resolved"}, {"urgency": "low"},
                                    {"ship_to": "500 Warehouse Ave, Austin, TX 78701"}])
async def test_a_follower_cannot_change_someone_elses_ticket(sn, change):
    first = await _report_mri(ctx_for(JANE))
    john = ctx_for(JOHN, session="sess-2")
    await tools.select_device("CE-10421", john)
    await tools.confirm_device(True, john)
    await tools.follow_ticket(first["ticket"], john)
    before = dict(sn.tables["incident"][0])

    result = await tools.update_ticket(first["ticket"], john, note="please hurry", **change)
    after = sn.tables["incident"][0]
    assert result["status"] == "not_permitted" and result["changed"] == []
    assert {k: after[k] for k in ("state", "urgency", "impact", "description")} == \
        {k: before[k] for k in ("state", "urgency", "impact", "description")}
    assert "please hurry" in after["comments_log"][-1] and "who follows this ticket" in after["comments_log"][-1]
    assert "Only Jane Doe can change" in card_text(john)
    # Notes alone are still fine for followers.
    assert (await tools.add_ticket_note(first["ticket"], "still broken", john))["status"] == "ok"


@pytest.mark.parametrize("status", ["Canceled", "Closed", "cancelled"])
async def test_update_ticket_cannot_cancel_or_close(sn, status):
    filed = await _file(ctx_for())
    result = await tools.update_ticket(filed["ticket"], ctx_for(), status=status)
    assert result["status"] == "error" and sn.tables["incident"][0]["state"] != "8"


async def test_second_reporter_can_still_report_separately(sn):
    await _report_mri(ctx_for(JANE))
    john = ctx_for(JOHN, session="sess-2")
    await pick(john, "CE-10421")
    result = await tools.report_separately(john)
    assert result["step"] == "describe_issue"


async def test_a_second_request_for_the_same_laptop_offers_the_open_ticket(sn):
    """D9: someone who already reported their laptop is offered to add to that ticket."""
    ctx = ctx_for(JANE)
    first = await _file(ctx)
    await tools.start_request(ctx)
    result = await pick(ctx, "123456")
    assert result["step"] == "already_reported" and result["open_tickets"][0]["number"] == first["ticket"]
    assert "You already have an open ticket" in card_text(ctx)
    await tools.set_issue("wont_power_on", "Still dead after charging overnight", "normal", ctx)
    added = await tools.follow_ticket(first["ticket"], ctx)
    assert added["own_ticket"] and len(sn.tables["incident"]) == 1
    assert "Still dead after charging overnight" in sn.tables["incident"][0]["comments_log"][-1]
    assert not sn.tables["incident"][0].get("watch_list")  # no self-following


async def test_someone_elses_ticket_on_a_personal_device_is_not_offered(sn):
    await _file(ctx_for(JANE))
    sn.tables["incident"][0]["caller_id"] = {"value": JOHN}
    ctx = ctx_for(JANE, session="sess-2")
    await tools.start_request(ctx)
    result = await pick(ctx, "123456")
    assert result["step"] == "describe_issue"


async def test_a_new_request_can_still_be_filed(sn):
    ctx = ctx_for(JANE)
    await _file(ctx)
    await tools.start_request(ctx)
    await pick(ctx, "123456")
    result = await tools.report_separately(ctx)
    assert result["step"] == "describe_issue"


def test_maker_is_not_doubled():
    assert cards.maker_model({"manufacturer": "GE HealthCare", "model": "GE HealthCare SIGNA Explorer"}) == \
        "GE HealthCare SIGNA Explorer"
    assert cards.maker_model({"manufacturer": "Apple", "model": "MacBook Air 13"}) == "Apple MacBook Air 13"


async def test_equipment_tag_photo_needs_no_confirmation(sn, monkeypatch):
    ctx = ctx_for(JANE)
    send_photos(ctx, monkeypatch, label(asset_tag="CE-20457", serial_number="BD8015PC0457"))
    await tools.analyze_photos(ctx)
    result = await tools.set_issue("safety_concern", "Door cracked, could free-flow", "normal", ctx)
    assert result["step"] == "review" and draft(ctx)["device"]["relation"] == "unconfirmed"
    assert "Clinical Engineering" in card_text(ctx)



# --- delivery addresses ----------------------------------------------------------------

HOME = "742 Evergreen Terrace, Kansas City, MO 64110"
JANE_EMAIL = "jane.doe@example.com"


async def _to_review(ctx):
    await pick(ctx, "123456")
    return await tools.set_issue("wont_power_on", "Dead", "normal", ctx)


async def test_saved_address_is_offered_but_not_applied(sn):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    result = await _to_review(ctx)
    assert result["ship_to"] == "1200 Harbor Health Way"  # the address on file
    assert f"Ship to Home instead: {HOME}" in card_text(ctx)
    await tools.submit_ticket(ctx)  # "everything looks good" = submit as shown
    assert "Ship to: 1200 Harbor Health Way" in sn.tables["incident"][0]["description"]


async def test_clicking_the_saved_address_uses_the_full_address(sn):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    await _to_review(ctx)
    result = await tools.choose_ship_to(HOME, ctx)
    assert result["ship_to"] == HOME and "Ship to my address on file instead" in card_text(ctx)
    await tools.submit_ticket(ctx)
    assert f"Ship to: {HOME}" in sn.tables["incident"][0]["description"]
    assert sn.saved_calls == []  # already saved


async def test_ship_to_my_house_resolves_to_the_saved_address(sn):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    await _to_review(ctx)
    result = await tools.update_request(ctx, delivery_location="my house")
    assert result["ship_to"] == HOME


@pytest.mark.parametrize("typed", [
    "500 Warehouse Ave, Austin, TX 78701",   # "house"
    "77 Homestead Rd, Austin, TX 78702",     # "home"
    "1 Network Way, Austin, TX 78703",       # "work"
    "9 Home St, Austin, TX 78704",           # a street named Home
])
async def test_a_typed_address_is_never_swapped_for_a_saved_one(sn, typed):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}, {"label": "Office", "address": "1 Main St, Denver, CO"}]
    ctx = ctx_for()
    await _to_review(ctx)
    result = await tools.update_request(ctx, delivery_location=typed)
    assert result["ship_to"] == typed


async def test_a_saved_address_typed_out_is_recognised(sn):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    await _to_review(ctx)
    await tools.update_request(ctx, delivery_location=HOME.upper().replace(",", ""))
    assert draft(ctx)["delivery"] == {"address": HOME, "label": "Home", "kind": "permanent", "saved": True}


async def test_a_place_name_is_not_an_address(sn):
    ctx = ctx_for()
    await _to_review(ctx)
    result = await tools.update_request(ctx, delivery_location="the Marriott in Chicago")
    assert result["status"] == "need_address" and "delivery" not in draft(ctx)


async def test_hotel_is_used_once_and_never_saved(sn):
    ctx = ctx_for()
    await _to_review(ctx)
    hotel = "Chicago Marriott Downtown, 540 N Michigan Ave, Chicago, IL 60611"
    result = await tools.update_request(ctx, delivery_location=hotel)
    assert result["ship_to"] == hotel and draft(ctx)["delivery"]["kind"] == "temporary"
    await tools.submit_ticket(ctx)
    assert f"Ship to: {hotel}" in sn.tables["incident"][0]["description"]
    assert sn.saved_calls == []


async def test_new_home_address_is_saved_verbatim(sn):
    ctx = ctx_for()
    await _to_review(ctx)
    await tools.update_request(ctx, delivery_location=HOME, delivery_label="Home", delivery_kind="permanent")
    await tools.submit_ticket(ctx)
    assert sn.saved_calls == [(JANE_EMAIL, "Home", HOME)]


async def test_back_to_the_address_on_file(sn):
    ctx = ctx_for()
    await _to_review(ctx)
    await tools.update_request(ctx, delivery_location=HOME, delivery_kind="permanent")
    result = await tools.update_request(ctx, delivery_location="my usual address")
    assert result["ship_to"] == "1200 Harbor Health Way" and "delivery" not in draft(ctx)


async def test_changing_a_filed_tickets_ship_to_needs_a_real_address(sn):
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    filed = await _file(ctx)
    result = await tools.update_ticket(filed["ticket"], ctx, ship_to="the hotel")
    assert result["status"] == "need_address"



# --- configured ticket fields (config/organization.yaml servicenow.ticket_fields) ----------


async def test_configured_fields_are_set_on_the_ticket(sn):
    await _file(ctx_for())  # MacBook Air: a laptop
    ticket = sn.tables["incident"][0]
    assert ticket["subcategory"] == "cpu" and ticket["contact_type"] == "self-service"


async def test_unmapped_device_type_leaves_the_field_unset(sn):
    ctx = ctx_for(JANE)
    await pick(ctx, "CE-10421")  # MRI: medical equipment, not in device_values
    await tools.set_issue("not_working", "Dead", "normal", ctx)
    await tools.submit_ticket(ctx)
    assert "subcategory" not in sn.tables["incident"][0]


async def test_a_refused_field_is_noted_for_the_desk(sn):
    sn.refuse = {"subcategory"}
    await _file(ctx_for())
    assert any("Could not set subcategory = 'cpu'" in c for c in sn.tables["incident"][0]["comments_log"])


# --- after filing, the draft is closed (H1.6) -------------------------------------------------


async def test_editing_after_filing_points_to_the_ticket(sn):
    ctx = ctx_for()
    filed = await _file(ctx)
    for call in (tools.update_request(ctx, urgency="high"), tools.choose_ship_to("", ctx),
                 tools.skip_photo(ctx), tools.confirm_device(True, ctx)):
        result = await call
        assert result["status"] == "already_submitted" and result["ticket"] == filed["ticket"]
        assert "update_ticket" in result["message"]
    assert len(sn.tables["incident"]) == 1


# --- photo evidence (H1.7, H1.12) -----------------------------------------------------------


async def test_a_later_wide_shot_keeps_the_damage_close_up(sn, monkeypatch):
    ctx = ctx_for()
    await pick(ctx, "123456")
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    send_photos(ctx, monkeypatch, damage(), PhotoFindings(image_kind="device"))
    await tools.analyze_photos(ctx)
    assert draft(ctx)["evidence"]["supports_replacement"] is True
    assert not any("didn't clearly show" in w for w in draft(ctx)["photo_warnings"])
    # And in a later message too.
    send_photos(ctx, monkeypatch, PhotoFindings(image_kind="device"))
    await tools.analyze_photos(ctx)
    assert draft(ctx)["evidence"]["supports_replacement"] is True


async def test_changing_the_device_drops_the_old_devices_photos(sn, monkeypatch):
    ctx = ctx_for()
    await pick(ctx, "123456")
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    send_photos(ctx, monkeypatch, damage(serial_number="WRONG123"))
    await tools.analyze_photos(ctx)
    assert draft(ctx).get("evidence")
    other = next(a for a in sn.tables["alm_hardware"]
                 if a["asset_tag"] != "123456" and a.get("assigned_to", {}).get("value") == JANE)
    await pick(ctx, other["asset_tag"])
    d = draft(ctx)
    assert d["device"]["asset_tag"] == other["asset_tag"]
    assert not d.get("evidence") and not d.get("photos") and not d.get("photo_warnings")


async def test_two_submits_at_once_file_one_ticket(sn, monkeypatch):
    """A double click or a retried turn: two submits of the same request run concurrently, each
    with its own copy of the session state (H1.11)."""
    import asyncio
    import copy

    real = servicenow._request

    async def slow(*a, **kw):  # yield like real network I/O, so the two submits interleave
        await asyncio.sleep(0.01)
        return await real(*a, **kw)

    monkeypatch.setattr(servicenow, "_request", slow)

    ctx = ctx_for(session="sess-race")
    await pick(ctx, "123456")
    await tools.set_issue("wont_power_on", "Dead", "normal", ctx)
    twin = SimpleNamespace(state=copy.deepcopy(ctx.state), session=ctx.session)
    a, b = await asyncio.gather(tools.submit_ticket(ctx), tools.submit_ticket(twin))
    assert len(sn.tables["incident"]) == 1
    assert a["ticket"] == b["ticket"] == sn.tables["incident"][0]["number"]


async def test_ship_to_on_equipment_is_explained_not_called_policy(sn):
    ctx = ctx_for(JANE)
    first = await _report_mri(ctx)
    result = await tools.update_ticket(first["ticket"], ctx, ship_to="500 Warehouse Ave, Austin, TX 78701")
    assert result["not_applicable"][0]["change"] == "Ship-to address" and not result["not_permitted_note_added"]
    assert "repaired on site" in card_text(ctx) and "policy" not in card_text(ctx)
    assert "asked to ship the replacement to: 500 Warehouse Ave" in sn.tables["incident"][0]["comments_log"][-1]


async def test_ship_to_change_on_own_ticket_still_applies(sn):
    ctx = ctx_for()
    filed = await _file(ctx)
    result = await tools.update_ticket(filed["ticket"], ctx, ship_to="500 Warehouse Ave, Austin, TX 78701")
    assert result["changed"] == [{"change": "Ship-to address", "value": "500 Warehouse Ave, Austin, TX 78701"}]


# --- organization switches (profile features / requester_changes) ---------------------------


@pytest.fixture
def switch(monkeypatch):
    def set_(group, name, value):
        monkeypatch.setattr(getattr(cards.PROFILE, group), name, value)
    return set_


async def test_equipment_reporting_off_keeps_to_own_devices(sn, switch):
    switch("features", "equipment_reporting", False)
    ctx = ctx_for(JANE)
    result = await tools.select_device("CE-10421", ctx)
    assert result["status"] == "not_supported" and not draft(ctx).get("device")
    found = await tools.find_device("the MRI in radiology", ctx)
    assert found["status"] == "not_found"


async def test_follow_open_tickets_off_files_separately(sn, switch):
    await _report_mri(ctx_for(JANE))
    switch("features", "follow_open_tickets", False)
    john = ctx_for(JOHN, session="sess-2")
    await tools.select_device("CE-10421", john)
    result = await tools.confirm_device(True, john)
    assert result["step"] != "already_reported"


async def test_photo_analysis_off_attaches_photos_unread(sn, switch, monkeypatch):
    switch("features", "photo_analysis", False)
    ctx = ctx_for()
    await pick(ctx, "123456")
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)

    async def never(*a, **kw):
        raise AssertionError("the photo model must not be called")

    send_photos(ctx, monkeypatch, damage())
    monkeypatch.setattr(vision, "analyze_photo", never)
    result = await tools.analyze_photos(ctx)
    assert result["analyzed"] is False and result["step"] == "review"
    assert draft(ctx)["photos"] and "attached for the service desk" in draft(ctx)["evidence"]["summary"]


async def test_saved_addresses_off(sn, switch):
    switch("features", "saved_addresses", False)
    sn.saved[JANE_EMAIL] = [{"label": "Home", "address": HOME}]
    ctx = ctx_for()
    await _to_review(ctx)
    assert "Ship to Home instead" not in card_text(ctx)


@pytest.mark.parametrize("change, policy", [({"urgency": "high"}, "urgency"), ({"status": "On Hold"}, "status"),
                                            ({"ship_to": "500 Warehouse Ave, Austin, TX 78701"}, "ship_to")])
async def test_changes_left_to_the_desk_become_a_request(sn, switch, change, policy):
    switch("requester_changes", policy, False)
    ctx = ctx_for()
    filed = await _file(ctx)
    before = dict(sn.tables["incident"][0])
    result = await tools.update_ticket(filed["ticket"], ctx, **change)
    after = sn.tables["incident"][0]
    assert result["requested_from_desk"] and not result["changed"]
    assert {k: after[k] for k in ("urgency", "state", "description")} == {k: before[k] for k in ("urgency", "state", "description")}
    assert "made by the service desk here" in after["comments_log"][-1]


async def test_cancel_left_to_the_desk(sn, switch):
    switch("requester_changes", "cancel", False)
    ctx = ctx_for()
    filed = await _file(ctx)
    result = await tools.cancel_ticket(filed["ticket"], "found a spare", ctx)
    assert sn.tables["incident"][0]["state"] != "8" and result["requested_from_desk"]


async def test_the_same_photo_twice_is_read_once(sn, monkeypatch):
    ctx = ctx_for()
    await pick(ctx, "123456")
    await tools.set_issue("cracked_screen", "Cracked", "normal", ctx)
    calls = []

    async def analyze(uri, mime, hint=""):
        calls.append(uri)
        return damage()

    monkeypatch.setattr(vision, "analyze_photo", analyze)
    for pid in ("ph_a", "ph_b"):  # the same bytes, attached twice
        ctx.state[f"photo:{pid}"] = {"photo_id": pid, "uri": f"gs://b/{pid}.jpg", "mime_type": "image/jpeg",
                                     "sha256": "same"}
        ctx.state["last_photo_ids"] = [pid]
        result = await tools.analyze_photos(ctx)
        assert result["step"] == "review"
    assert len(calls) == 1 and len(draft(ctx)["photos"]) == 1


async def test_each_filed_ticket_logs_its_path(sn, caplog):
    import json as _json
    import logging
    caplog.set_level(logging.INFO, logger="app.tools.filing")
    await _file(ctx_for())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ticket_filed "))
    data = _json.loads(line.split(" ", 1)[1])
    assert data["source"] == "tag" and data["device_kind"] == "personal" and data["issue"] == "wont_power_on"
    assert "jane" not in line.lower()  # no personal data in the path log


async def test_whats_happening_shows_the_latest_note_per_ticket(sn, monkeypatch):
    """D8: one digest card instead of opening each ticket."""
    ctx = ctx_for()
    filed = await _file(ctx)

    async def notes(sys_id):
        return [{"when": "2026-10-02 09:00:00", "who": "desk", "kind": "note", "text": "Replacement ships Monday"}]

    monkeypatch.setattr(servicenow, "notes", notes)
    result = await tools.list_my_tickets(ctx, latest_notes=True)
    assert result["tickets"][0]["latest_note"] == "Replacement ships Monday"
    assert "Latest: Replacement ships Monday" in card_text(ctx)
    plain = await tools.list_my_tickets(ctx)
    assert "Latest:" not in card_text(ctx) and plain["tickets"][0]["number"] == filed["ticket"]


async def test_a_follower_can_stop_following(sn):
    """F7: only the follower is removed; the reporter's own ticket can't be unfollowed."""
    jane = ctx_for(JANE)
    first = await _report_mri(jane)
    john = ctx_for(JOHN, session="sess-2")
    await tools.select_device("CE-10421", john)
    await tools.confirm_device(True, john)
    await tools.follow_ticket(first["ticket"], john)
    sn.tables["incident"][0]["watch_list"] = "someone_else," + JOHN
    result = await tools.unfollow_ticket(first["ticket"], john)
    assert result["status"] == "ok" and sn.tables["incident"][0]["watch_list"] == "someone_else"
    assert (await tools.list_my_tickets(john))["count"] == 0
    assert (await tools.unfollow_ticket(first["ticket"], jane))["status"] == "error"
