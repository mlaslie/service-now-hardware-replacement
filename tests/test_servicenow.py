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


@pytest.mark.parametrize("number", ["INC0000001^NQnumber=INC0000999", "INC0000001^ORnumber=INC0000999",
                                    "INC0000001,INC0000999", "number=INC0000999", "^NQ"])
async def test_a_ticket_number_cannot_add_query_operators(calls, number):
    assert await servicenow.my_incident("me", number) is None
    assert await servicenow.update_incident("me", number, {"state": "8"}) is None
    assert not calls  # rejected before anything is sent


@pytest.mark.parametrize("tag, serial", [("P1000^NQinstall_status!=7", ""), ("", "X^NQinstall_status!=7"),
                                         ("", "C02 XL0^ORserial_numberISNOTEMPTY")])
async def test_tags_and_serials_cannot_add_query_operators(monkeypatch, tag, serial):
    queries = []

    async def fake_assets(query, limit):
        queries.append(query)
        return []

    monkeypatch.setattr(servicenow, "_assets", fake_assets)
    await servicenow.find_asset(tag, serial)
    assert queries and all("^" not in q and q.count("=") == 1 for q in queries), queries


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


# --- transport and error pages ------------------------------------------------------------

import httpx  # noqa: E402


def _serve(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(servicenow, "_http", lambda: client)
    monkeypatch.setattr(servicenow.config, "SN_INSTANCE_URL", "https://example.service-now.com")


@pytest.mark.parametrize("handler, error", [
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), servicenow.Unavailable),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), servicenow.Unavailable),
    (lambda r: httpx.Response(403, html="<html>Forbidden</html>"), servicenow.ServiceNowError),
    (lambda r: httpx.Response(502, html="<html>Bad gateway</html>"), servicenow.ServiceNowError),
    (lambda r: httpx.Response(503, html="<html>Your instance is hibernating</html>"), servicenow.Hibernating),
    (lambda r: httpx.Response(200, html="<html>Waking up</html>"), servicenow.Hibernating),
    (lambda r: httpx.Response(500, headers={"content-type": "application/json"}, content=b""), servicenow.ServiceNowError),
    (lambda r: httpx.Response(200, headers={"content-type": "application/json"}, content=b"{not json"),
     servicenow.Hibernating),
    (lambda r: httpx.Response(401, json={}), servicenow.NotSignedIn),
])
async def test_every_failure_is_a_servicenow_error(monkeypatch, handler, error):
    _serve(monkeypatch, handler)
    with pytest.raises(error):
        await servicenow._request("GET", "/api/now/table/incident", token="t")


async def test_a_403_is_not_reported_as_hibernating(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(403, html="<html>Forbidden</html>"))
    with pytest.raises(servicenow.ServiceNowError) as err:
        await servicenow._request("GET", "/api/now/table/incident", token="t")
    assert not isinstance(err.value, servicenow.Hibernating) and "403" in str(err.value)


async def test_soft_fail_readers_survive_a_timeout(monkeypatch):
    _serve(monkeypatch, lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    servicenow.user_token.set("t")
    assert await servicenow.open_incidents_for_ci("ci1") == []
    assert await servicenow.dropped_fields("s1", {"subcategory": "cpu"}) == {}


async def test_json_error_message_is_kept(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(403, json={"error": {"message": "ACL denied"}}))
    with pytest.raises(servicenow.ServiceNowError, match="ACL denied"):
        await servicenow._request("GET", "/api/now/table/incident", token="t")


def test_notes_sort_by_time_not_by_text():
    entries = [{"when": "09/25/2026 05:55 PM", "text": "a"}, {"when": "10/01/2026 09:10 AM", "text": "b"},
               {"when": "09/25/2026 11:00 AM", "text": "c"}, {"when": "12/31/2025 11:59 PM", "text": "d"}]
    assert [e["text"] for e in servicenow.newest_first(entries)] == ["b", "a", "c", "d"]
    iso = [{"when": "2026-09-25 17:55:00", "text": "x"}, {"when": "2026-10-01 09:10:00", "text": "y"}]
    assert [e["text"] for e in servicenow.newest_first(iso)] == ["y", "x"]
    odd = [{"when": "yesterday", "text": "1"}, {"when": "2026-10-01 09:10:00", "text": "2"}]
    assert servicenow.newest_first(odd) == odd  # unknown format: ServiceNow's order kept


def test_category_types_override_the_guess(monkeypatch):
    assert servicenow._device_type("Computer", "Dell OptiPlex 7090") == "desktop"
    monkeypatch.setattr(servicenow.PROFILE.devices, "category_types", {"Computer": "laptop", "Handheld": "phone"})
    assert servicenow._device_type("computer", "Dell OptiPlex 7090") == "laptop"
    assert servicenow._device_type("Handheld", "Zebra TC52") == "phone"
    assert servicenow._device_type("Printer", "HP LaserJet") == "other"  # not listed: guessed


async def test_a_correlation_id_cannot_add_query_operators(calls):
    """R1: the id comes from the client's conversation id; it is cleaned before the query."""
    await servicenow.find_open_by_correlation("me", "sess^NQnumber=INC0000999:ab12")
    q = calls[-1][2]["params"]["sysparm_query"]
    assert "^NQ" not in q and q.endswith("^correlation_id=sessNQnumberINC0000999:ab12")
    calls.clear()
    assert await servicenow.find_open_by_correlation("me", "^^==") is None and not calls


# --- following: two people at once (R3) ------------------------------------------------------


class WatchListTicket:
    """One incident's watch list, with hooks to simulate other writers."""

    def __init__(self, watchers=""):
        self.watch_list, self.comments, self.patches, self.after_patch, self.refuse = watchers, [], 0, None, 0

    async def __call__(self, method, path, **kw):
        if method == "GET":
            return {"result": {"sys_id": "s1", "number": "INC0000001", "state": "2", "watch_list": self.watch_list}}
        body = kw.get("json") or {}
        if "watch_list" in body:
            if self.refuse:
                self.refuse -= 1
                raise servicenow.ServiceNowError("aborted by business rule")
            self.watch_list = body["watch_list"]
            self.patches += 1
            if self.after_patch:
                hook, self.after_patch = self.after_patch, None
                hook(self)
        if "comments" in body:
            self.comments.append(body["comments"])
        return {"result": {"sys_id": "s1"}}


@pytest.fixture
def ticket(monkeypatch):
    t = WatchListTicket("a1")
    monkeypatch.setattr(servicenow, "_request", t)
    monkeypatch.setattr(servicenow, "WATCH_RECHECK_SECONDS", 0.01)
    return t


async def test_following_survives_a_write_from_another_server(ticket):
    # Right after our write, another instance writes the list it read before ours: we're dropped.
    ticket.after_patch = lambda t: setattr(t, "watch_list", "a1,b2")
    saved = await servicenow.follow_incident("me", "s1", "also broken here")
    assert "me" in saved["watch_list"] and "b2" in saved["watch_list"]  # re-added by the re-check
    assert ticket.comments == ["also broken here"]


async def test_a_refused_stale_write_is_retried(ticket):
    ticket.refuse = 1  # the follow-only business rule refused a list that was already out of date
    saved = await servicenow.follow_incident("me", "s1", "note")
    assert "me" in saved["watch_list"] and ticket.comments == ["note"]


async def test_two_people_following_at_once_both_stay(ticket):
    import asyncio
    await asyncio.gather(servicenow.follow_incident("u1", "s1", "one"), servicenow.follow_incident("u2", "s1", "two"))
    assert set(ticket.watch_list.split(",")) == {"a1", "u1", "u2"}


async def test_unfollowing_removes_only_the_user(ticket):
    ticket.watch_list = "a1,me,b2"
    saved = await servicenow.unfollow_incident("me", "s1")
    assert saved["watch_list"] == ["a1", "b2"]
