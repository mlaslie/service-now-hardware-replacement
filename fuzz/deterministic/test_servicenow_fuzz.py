"""S1/S2: the ServiceNow client against a mocked HTTP layer (`servicenow._http` returns an
httpx.AsyncClient on a MockTransport), and its row and journal parsing on any JSON value."""

import asyncio
import json

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import servicenow
from fuzz.invariants import check, fail
from fuzz.strategies import examples, hostile_text, json_value

pytestmark = pytest.mark.fuzz

_TRANSPORT_ERRORS = [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                     httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
                     httpx.LocalProtocolError, httpx.ProxyError, httpx.UnsupportedProtocol, httpx.CloseError]
_STATUSES = [200, 201, 204, 301, 302, 400, 401, 403, 404, 409, 429, 500, 502, 503, 504]
_TYPES = ["application/json", "application/json; charset=utf-8", "text/html; charset=utf-8", "text/plain", "",
          "application/xml", "application/vnd.api+json"]

# A row as ServiceNow returns it: always a sys_id; other fields text or a reference {"value", "link"}
# (a field the user can't read comes back empty, so any subset of fields may be blank or missing).
# Reference fields may come as {"value", "link"}; dot-walked and plain fields are always text.
_REF = st.one_of(st.text(max_size=10), st.fixed_dictionaries({"value": st.text(max_size=10), "link": st.just("https://x")}))
_ROW = st.builds(
    lambda sid, plain, refs: {"sys_id": sid, **plain, **refs}, st.text("abcdef0123456789", min_size=1, max_size=32),
    st.dictionaries(st.sampled_from(["number", "short_description", "state", "priority", "watch_list", "asset_tag",
                                     "serial_number", "user_sys_id", "group.name", "element", "value", "sys_created_on",
                                     "comments", "work_notes", "model_category.name", "model.display_name",
                                     "display_name", "assigned_to.name", "caller_id.name"]), st.text(max_size=20),
                    max_size=8),
    st.dictionaries(st.sampled_from(["caller_id", "assigned_to", "ci", "group", "asset", "department", "location",
                                     "support_group", "managed_by", "assignment_group", "cmdb_ci"]), _REF, max_size=4))
# What ServiceNow (or a proxy in front of it) really sends, status and body together:
# - 200/201 JSON with rows: ("rows", rows) is answered as ServiceNow would for the request, a list for a
#   table query and one record otherwise; ("truncated", rows) is the same cut short;
# - 4xx/5xx with ServiceNow's {"error": {...}} body;
# - any status with a page that isn't JSON (HTML login or hibernation page, plain text, nothing, bytes).
_ROWS = st.one_of(st.lists(_ROW, max_size=3).map(lambda rows: ("rows", rows)),
                  st.lists(_ROW, min_size=1, max_size=2).map(lambda rows: ("truncated", rows)))
_ERROR = st.builds(lambda m, d: json.dumps({"error": {"message": m, "detail": d}, "status": "failure"}),
                   st.text(max_size=30), st.text(max_size=30))
_PAGE = st.one_of(st.sampled_from(["", "<html><body>Your instance is hibernating</body></html>", "<html>Login</html>",
                                   "Service Unavailable", "{", "{\"result\": "]),
                  st.binary(max_size=40).map(lambda b: b.decode("latin-1")))
_JSON_TYPES = ["application/json", "application/json; charset=utf-8"]
_ANY_STATUS = st.one_of(st.sampled_from(_STATUSES), st.integers(100, 599))


def _real_response(encodings=False):
    headers = st.just({}) if not encodings else st.sampled_from([{}, {"content-encoding": "gzip"},
                                                                {"content-encoding": "br"}, {"content-encoding": "x"}])
    ok = st.tuples(st.just("resp"), st.sampled_from([200, 201]), st.sampled_from(_JSON_TYPES), _ROWS, headers)
    err = st.tuples(st.just("resp"), st.sampled_from([s for s in _STATUSES if s >= 400]), st.sampled_from(_JSON_TYPES),
                    _ERROR, headers)
    # A page that isn't JSON is labelled as such; one labelled JSON is only ever cut short (never "1" or "null").
    page = st.one_of(
        st.tuples(st.just("resp"), _ANY_STATUS, st.sampled_from([t for t in _TYPES if "json" not in t]), _PAGE, headers),
        st.tuples(st.just("resp"), _ANY_STATUS, st.sampled_from(_JSON_TYPES),
                  st.sampled_from(["{", "{\"result\": ", "<html>Login</html>"]), headers))
    # An empty body labelled JSON: test_an_empty_json_answer (so that finding can't hide others here).
    exc = st.sampled_from(_TRANSPORT_ERRORS).map(lambda e: ("exc", e))
    return st.one_of(ok, err, page, exc)


# Any JSON value at all, with any status and content type: shapes ServiceNow itself doesn't send.
def _odd_response():
    return st.tuples(st.just("resp"), _ANY_STATUS, st.sampled_from(_TYPES), json_value.map(json.dumps), st.just({}))


def _call_all():
    """Every public client function, with plausible arguments."""
    s = servicenow
    return [
        ("current_user", lambda: s.current_user("tok-fuzz")),
        ("my_assets", lambda: s.my_assets("u_jane")),
        ("department_assets", lambda: s.department_assets("d_rad")),
        ("search_assets", lambda: s.search_assets("pump")),
        ("find_asset", lambda: s.find_asset("123456", "FCPJ2GJTHC")),
        ("open_incidents_for_ci", lambda: s.open_incidents_for_ci("ci_a10")),
        ("follow_incident", lambda: s.follow_incident("u_jane", "i1", "Also reported")),
        ("create_incident", lambda: s.create_incident({"caller_id": "u_jane", "short_description": "x"})),
        ("dropped_fields", lambda: s.dropped_fields("i1", {"subcategory": "cpu"})),
        ("find_open_by_correlation", lambda: s.find_open_by_correlation("u_jane", "sess:abc")),
        ("my_incidents", lambda: s.my_incidents("u_jane")),
        ("my_incident", lambda: s.my_incident("u_jane", "INC0010001")),
        ("update_incident", lambda: s.update_incident("u_jane", "INC0010001", {"comments": "hi"})),
        ("notes", lambda: s.notes("i1")),
        ("attach", lambda: s.attach("i1", "a.jpg", b"jpeg", "image/jpeg")),
    ]


_NAMES = [n for n, _ in _call_all()]


def _body_for(request, body):
    if not isinstance(body, tuple):
        return body
    kind, rows = body
    parts = request.url.path.strip("/").split("/")
    collection = request.method == "GET" and parts[:3] == ["api", "now", "table"] and len(parts) == 4
    text = json.dumps({"result": rows if collection else (rows[0] if rows else {})})
    return text if kind == "rows" else text[: max(1, len(text) // 2)]


def _run_with(responses, name):
    """Runs one client function with every HTTP call answered from `responses` in turn."""
    turn = iter(range(10**6))
    requests = []

    def handler(request):
        kind, *rest = responses[next(turn) % len(responses)]
        requests.append(f"{request.method} {request.url.path}")
        if kind == "exc":
            raise rest[0]("fuzz", request=request)
        status, ctype, body, headers = rest
        body = _body_for(request, body)
        hdrs = {**({"content-type": ctype} if ctype else {}), **headers}
        return httpx.Response(status, headers=hdrs, content=body.encode("utf-8", "surrogatepass"), request=request)

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        old_http, old_token = servicenow._http, servicenow.user_token.set("tok-fuzz")
        servicenow._http = lambda: client
        servicenow._user_cache.clear()
        try:
            return await dict(_call_all())[name]()
        finally:
            servicenow._http = old_http
            servicenow.user_token.reset(old_token)
            await client.aclose()

    try:
        asyncio.run(go())
        return None, requests
    except (servicenow.NotSignedIn, servicenow.ServiceNowError) as exc:
        return exc, requests
    except Exception as exc:  # noqa: BLE001
        fail("S1", f"{name} raised {type(exc).__name__}: {exc}", function=name, requests=requests,
             responses=[r if r[0] == "resp" else ("exc", r[1].__name__) for r in responses])


@pytest.mark.invariants("S1")
@settings(max_examples=examples(400, 4000))
@given(name=st.sampled_from(_NAMES), responses=st.lists(_real_response(), min_size=1, max_size=4))
def test_client_raises_only_servicenow_errors(name, responses):
    """Statuses, content types, truncated JSON, HTML pages, empty bodies and transport errors."""
    exc, _ = _run_with(responses, name)
    first = responses[0]
    if first[0] == "resp" and first[1] == 401:
        check(isinstance(exc, servicenow.NotSignedIn), "S1", f"{name}: a 401 did not become NotSignedIn",
              raised=type(exc).__name__ if exc else None)


@pytest.mark.invariants("S1")
@settings(max_examples=examples(200, 2000))
@given(name=st.sampled_from(_NAMES), responses=st.lists(_odd_response(), min_size=1, max_size=3))
def test_client_with_odd_json_shapes(name, responses):
    """A 200 or an error status whose JSON body is any value: {"result": "x"}, {"error": "text"}, [1]..."""
    _run_with(responses, name)


@pytest.mark.invariants("S1")
@settings(max_examples=examples(100, 1000))
@given(name=st.sampled_from(_NAMES), responses=st.lists(_real_response(encodings=True), min_size=1, max_size=3))
def test_client_with_a_wrong_content_encoding(name, responses):
    """A proxy or error page labelled gzip/br that isn't: httpx raises DecodingError while reading it."""
    _run_with(responses, name)


@pytest.mark.invariants("S1")
@pytest.mark.parametrize("name", _NAMES)
def test_401_is_not_signed_in(name):
    exc, _ = _run_with([("resp", 401, "application/json", json.dumps({"error": {"message": "expired"}}), {})], name)
    check(isinstance(exc, servicenow.NotSignedIn), "S1", f"{name}: a 401 did not become NotSignedIn",
          raised=type(exc).__name__ if exc else None)


@pytest.mark.invariants("S2")
@settings(max_examples=examples(400, 4000))
@given(row=st.dictionaries(st.text(max_size=20), json_value, max_size=6) | st.dictionaries(
    st.sampled_from(["sys_id", "number", "state", "priority", "caller_id", "watch_list", "assigned_to", "ci",
                     "department", "location", "support_group", "managed_by", "model_category.name",
                     "model.display_name", "display_name", "sys_created_on", "urgency"]), json_value, max_size=10),
       value=json_value)
def test_row_parsing_accepts_any_json(row, value):
    for name, fn in (("_value", lambda: servicenow._value(value)), ("_incident", lambda: servicenow._incident(row)),
                     ("_asset", lambda: servicenow._asset(row))):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fail("S2", f"{name} raised {type(exc).__name__}: {exc}", row=row, value=value)


@pytest.mark.invariants("S2")
@settings(max_examples=examples(400, 4000))
@given(text=st.one_of(hostile_text, st.builds(
    lambda d, who, kind, body: f"{d} - {who} ({kind})\n{body}",
    st.sampled_from(["2026-09-25 17:55:00", "09/25/2026 05:55 PM", "25.09.2026 17:55:00", "2026-13-45 99:99"]),
    hostile_text, st.sampled_from(["Additional comments", "Work notes", "comments"]), hostile_text)))
def test_journal_parsing_accepts_any_text(text):
    try:
        parsed = servicenow.parse_journal(text)
    except Exception as exc:  # noqa: BLE001
        fail("S2", f"parse_journal raised {type(exc).__name__}: {exc}", text=text)
    check(isinstance(parsed, list) and (parsed or not text.strip()), "S2", "journal text parsed to no notes",
          text=text[:300])
    try:
        servicenow.newest_first(parsed)
    except Exception as exc:  # noqa: BLE001
        fail("S2", f"newest_first raised {type(exc).__name__}: {exc}", entries=parsed)


@pytest.mark.invariants("S2")
@settings(max_examples=examples(200, 2000))
@given(value=json_value)
def test_journal_parsing_accepts_any_json_value(value):
    """The journal fields come from a ServiceNow row: any JSON value, not only text."""
    try:
        servicenow.newest_first(servicenow.parse_journal(value))
    except Exception as exc:  # noqa: BLE001
        fail("S2", f"parse_journal({type(value).__name__}) raised {type(exc).__name__}: {exc}", value=value)


@pytest.mark.invariants("S1")
@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("status", [200, 201])
def test_an_empty_json_answer(name, status):
    """A 2xx labelled application/json with no body (a proxy or a dropped connection after the
    headers): the client treats a cut-off JSON body as an error, so an empty one must be one too."""
    _run_with([("resp", status, "application/json", "", {})], name)
