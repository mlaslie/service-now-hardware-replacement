"""T1-T8: the real wizard and ticket tools, driven by a Hypothesis state machine against the
in-memory ServiceNow (fuzz/fakes.py), as two users (Jane and John) in two conversations, next to
tickets of a third user (Ana). Every tool has a rule; the invariants are checked after every step.

Three machines share the rules and differ in what they stress:
- HostileText: hostile strings in every free-text argument (descriptions, notes, reasons, photo text).
- Access: plain free text; hostile tags, ticket numbers and addresses; other users' tickets.
- Chaos: plain free text; ServiceNow errors, timeouts, malformed rows, dropped fields and lost
  responses in 10% of calls.
Plus: two concurrent submits, a retried submit after a lost response, and wrong-type arguments to
every tool.
"""

import asyncio
import copy
import inspect
from types import SimpleNamespace

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from app import cards, servicenow, tools
from fuzz import strategies as S
from fuzz.fakes import ANA, JANE, JOHN, AuditedTableAPI, World, clear_filing_memory, ctx_for
from fuzz.invariants import (check, check_filed, check_queries, check_reported_changes, check_result_shape,
                             check_ship_to, check_unique_tickets, check_visibility, check_writes, collapse,
                             equipment_exceptions, fail, shown_numbers)

pytestmark = pytest.mark.fuzz

CARD_KEY = tools.CARD_KEY
HOME = S.HOME
EMAILS = {JANE: "jane.doe@example.com", JOHN: "john.doe@example.com"}
USERS = st.sampled_from([JANE, JOHN])


def _caller(row):
    v = row.get("caller_id")
    return v.get("value", "") if isinstance(v, dict) else str(v or "")


def _watchers(row):
    return [w for w in str(row.get("watch_list") or "").split(",") if w]


def _update_without_problem(draft, which, category):
    """update_request changing the description or urgency (not the category) of a draft with no problem
    yet: it stores a problem without a category, and the next step that reads it raises KeyError."""
    has_problem = bool((draft.get("issue") or {}).get("category"))
    sets_problem = "category" in which and category in cards.ISSUE_LABELS
    return not has_problem and not sets_problem and bool({"description", "urgency"} & set(which)) \
        and not draft.get("submitted_number")


# App crashes found by this harness that have their own failing test below. The machines step
# around exactly these triggers, so one known crash doesn't stop them exploring everything else.
KNOWN_CRASHES = {"update_request_without_problem": _update_without_problem}


class ToolsMachine(RuleBasedStateMachine):
    TEXT = S.hostile_text
    CHAOS = 0.0

    def __init__(self):
        super().__init__()
        clear_filing_memory()
        self.api = AuditedTableAPI(chaos=0.0)
        self.world = World(self.api, saved={EMAILS[JANE]: [{"label": "Home", "address": HOME}]}).install()
        self.loop = asyncio.new_event_loop()
        self.ctxs = {JANE: ctx_for(JANE, "sess-jane"), JOHN: ctx_for(JOHN, "sess-john")}
        self.typed: dict[str, set[str]] = {JANE: set(), JOHN: set()}

    def teardown(self):
        self.world.uninstall()
        self.loop.close()
        clear_filing_memory()

    @initialize(seed=st.integers(0, 2**32 - 1))
    def start_chaos(self, seed):
        self.api.chaos = self.CHAOS
        self.api.rng.seed(seed)

    # --- running a tool and checking every invariant ----------------------------------------

    def _ci(self, user):
        return ((self.ctxs[user].state.get("draft") or {}).get("device") or {}).get("ci", "")

    def _candidates(self, user):
        profile = self.ctxs[user].state["end_user"]["profile"]
        saved = {a["address"] for a in self.api.saved.get(EMAILS[user], [])}
        return {profile.get("location_address", ""), profile.get("location", "")} | saved | self.typed[user]

    def call(self, user, name, **kwargs):
        ctx = self.ctxs[user]
        fn = getattr(tools, name)
        self.api.acting_user = user
        start = len(self.api.log)
        ci_before = self._ci(user)
        ctx.state[CARD_KEY] = None
        try:
            result = self.loop.run_until_complete(fn(tool_context=ctx, **kwargs))
        except Exception as exc:  # noqa: BLE001
            fail("T7", f"{name} raised {type(exc).__name__}: {exc}", tool=name, args=kwargs, user=user)
        check_result_shape(name, result)
        event(f"{name} -> {result.get('status')}/{result.get('step', '')}")
        log, incidents = self.api.log, self.api.tables["incident"]
        check_queries(log, start)
        check_writes(log, start)
        card = ctx.state.get(CARD_KEY)
        allowed = equipment_exceptions(incidents, ci_before) | equipment_exceptions(incidents, self._ci(user))
        check_visibility(user, shown_numbers(result, card), incidents, allowed)
        check_unique_tickets(incidents)
        candidates = self._candidates(user)
        for entry in log[start:]:
            if "created" in entry:
                check_filed(entry["json"])
                check_ship_to(entry["json"].get("description", ""), candidates, "filed ticket")
            if entry["method"] == "PATCH" and "description" in (entry["json"] or {}):
                check_ship_to(entry["json"]["description"], candidates, "ticket update")
        check_reported_changes(name, kwargs, result, self.api, user)
        return result

    def number(self, user, kind, pick):
        incidents = self.api.tables["incident"]
        pools = {"own": [r for r in incidents if _caller(r) == user],
                 "foreign": [r for r in incidents if _caller(r) != user and user not in _watchers(r)],
                 "followed": [r for r in incidents if _caller(r) != user and user in _watchers(r)]}
        if kind in pools:
            pool = pools[kind]
            return pool[pick % len(pool)]["number"] if pool else "INC0099999"
        if kind == "lower":
            return f" {incidents[pick % len(incidents)]['number'].lower()} "
        if kind == "hostile":
            return S.HOSTILE_NUMBERS[pick % len(S.HOSTILE_NUMBERS)]
        return "INC0099999"

    # --- a whole request in one rule, so the machine reaches filed tickets often -----------------

    @rule(user=USERS, tag=st.sampled_from(S.ALL_TAGS + S.SERIALS[:2]), category=st.sampled_from(S.CATEGORIES_VALID),
          urgency=st.sampled_from(["low", "normal", "high", "critical"]), data=st.data(),
          photo=st.sampled_from(["damage", "label", "skip", "none"]), separately=st.booleans(), submit=st.booleans())
    def file_a_request(self, user, tag, category, urgency, data, photo, separately, submit):
        from app.vision import PhotoFindings

        self.call(user, "select_device", asset_tag=tag)
        result = self.call(user, "confirm_device", correct=True)
        if result.get("step") == "already_reported" and separately:
            self.call(user, "report_separately")
        self.call(user, "set_issue", category=category, description=data.draw(self.TEXT), urgency=urgency)
        if photo == "skip":
            self.call(user, "skip_photo")
        elif photo != "none":
            findings = PhotoFindings(image_kind="damage", damage_present=True, damage_description=data.draw(self.TEXT),
                                     damage_severity="severe", issue_category=category, supports_replacement=True) \
                if photo == "damage" else PhotoFindings(image_kind="label", asset_tag=tag)
            self.world.send_photos(self.ctxs[user].state, [findings])
            self.call(user, "analyze_photos")
        if submit:
            self.call(user, "submit_ticket")

    # --- one rule per tool ------------------------------------------------------------------

    @rule(user=USERS)
    def start_request(self, user):
        self.call(user, "start_request")

    @rule(user=USERS, tag=S.tag)
    def select_device(self, user, tag):
        self.call(user, "select_device", asset_tag=tag)

    @rule(user=USERS, description=S.description_query)
    def find_device(self, user, description):
        self.call(user, "find_device", description=description)

    @rule(user=USERS, correct=st.booleans())
    def confirm_device(self, user, correct):
        self.call(user, "confirm_device", correct=correct)

    @rule(user=USERS)
    def request_label_photo(self, user):
        self.call(user, "request_label_photo")

    @rule(user=USERS, category=S.category, urgency=S.urgency, data=st.data())
    def set_issue(self, user, category, urgency, data):
        self.call(user, "set_issue", category=category, description=data.draw(self.TEXT), urgency=urgency)

    @rule(user=USERS, data=st.data(), broken=st.booleans())
    def analyze_photos(self, user, data, broken):
        findings = data.draw(st.lists(S.photo_findings(self.TEXT), min_size=1, max_size=2))
        if broken and len(findings) > 1:
            findings[0] = RuntimeError("photo model failed")
        self.world.send_photos(self.ctxs[user].state, findings)
        self.call(user, "analyze_photos")

    @rule(user=USERS)
    def skip_photo(self, user):
        self.call(user, "skip_photo")

    @rule(user=USERS, data=st.data(), urgency=S.urgency, category=S.category, where=S.address,
          label=st.sampled_from(["", "Home", "Office", "Hotel", "{0}"]),
          kind=st.sampled_from(["", "permanent", "temporary", "x"]), which=st.sets(st.sampled_from(
              ["description", "urgency", "category", "delivery"]), min_size=1))
    def update_request(self, user, data, urgency, category, where, label, kind, which):
        draft = self.ctxs[user].state.get("draft") or {}
        if KNOWN_CRASHES["update_request_without_problem"](draft, which, category):
            return  # reported by test_update_request_before_a_problem_is_chosen; skipped here so it can't hide others
        args = {}
        if "description" in which:
            args["description"] = data.draw(self.TEXT)
        if "urgency" in which:
            args["urgency"] = urgency
        if "category" in which:
            args["category"] = category
        if "delivery" in which:
            args |= {"delivery_location": where, "delivery_label": label, "delivery_kind": kind}
            self.typed[user].add(collapse(where))
        self.call(user, "update_request", **args)

    @rule(user=USERS, address=st.one_of(st.just(""), st.just(HOME), S.address))
    def choose_ship_to(self, user, address):
        self.typed[user].add(collapse(address))
        self.call(user, "choose_ship_to", address=address)

    @rule(user=USERS)
    def show_review(self, user):
        self.call(user, "show_review")

    @rule(user=USERS)
    def submit_ticket(self, user):
        self.call(user, "submit_ticket")

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick)
    def follow_ticket(self, user, kind, pick):
        self.call(user, "follow_ticket", number=self.number(user, kind, pick))

    @rule(user=USERS)
    def report_separately(self, user):
        self.call(user, "report_separately")

    @rule(user=USERS, include_closed=st.booleans())
    def list_my_tickets(self, user, include_closed):
        self.call(user, "list_my_tickets", include_closed=include_closed)

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, show=S.show)
    def get_ticket(self, user, kind, pick, show):
        self.call(user, "get_ticket", number=self.number(user, kind, pick), show=show)

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, data=st.data(), status=S.status, urgency=S.urgency,
          ship_to=S.address, which=st.sets(st.sampled_from(["note", "status", "urgency", "ship_to"]), min_size=1))
    def update_ticket(self, user, kind, pick, data, status, urgency, ship_to, which):
        args = {"number": self.number(user, kind, pick)}
        if "note" in which:
            args["note"] = data.draw(self.TEXT)
        if "status" in which:
            args["status"] = status
        if "urgency" in which:
            args["urgency"] = urgency
        if "ship_to" in which:
            args["ship_to"] = ship_to
            self.typed[user].add(collapse(ship_to))
        self.call(user, "update_ticket", **args)

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, data=st.data())
    def add_ticket_note(self, user, kind, pick, data):
        self.call(user, "add_ticket_note", number=self.number(user, kind, pick), note=data.draw(self.TEXT))

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, address=S.address)
    def change_ticket_shipping(self, user, kind, pick, address):
        self.typed[user].add(collapse(address))
        self.call(user, "change_ticket_shipping", number=self.number(user, kind, pick), address=address)

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, data=st.data())
    def request_urgent_handling(self, user, kind, pick, data):
        self.call(user, "request_urgent_handling", number=self.number(user, kind, pick), reason=data.draw(self.TEXT))

    @rule(user=USERS, kind=S.number_kind, pick=S.number_pick, data=st.data())
    def cancel_ticket(self, user, kind, pick, data):
        self.call(user, "cancel_ticket", number=self.number(user, kind, pick), reason=data.draw(self.TEXT))

    # --- global invariants ------------------------------------------------------------------

    @invariant()
    def one_ticket_per_draft(self):
        check_unique_tickets(self.api.tables["incident"])

    @invariant()
    def others_tickets_keep_their_fields(self):
        """T1 on the stored data: Ana's tickets keep caller, state, urgency and description."""
        for row in self.api.tables["incident"]:
            if _caller(row) == ANA and row["number"].startswith("INC009"):
                original = getattr(self, "_ana", None)
                if original is None:
                    from fuzz.fakes import foreign_tickets
                    self._ana = original = {r["number"]: r for r in foreign_tickets()}
                o = original[row["number"]]
                for k in ("state", "urgency", "impact", "description", "short_description", "priority"):
                    check(row.get(k) == o.get(k), "T1", f"{k} of Ana's {row['number']} was changed",
                          before=o.get(k), after=row.get(k))


def _defuse_ship_to(text: str) -> str:
    """A free-text line starting with "Ship to:" becomes a second Ship-to line in the ticket description:
    reported by test_free_text_cannot_add_a_ship_to_line and test_ship_to_change_reaches_the_real_line.
    The hostile machine renames such lines so that known finding can't hide the others."""
    lines = text.splitlines(keepends=True)
    return "".join("Ship-to:" + ln[len("Ship to:"):] if ln.startswith("Ship to:") else ln for ln in lines)


class HostileTextMachine(ToolsMachine):
    TEXT = S.hostile_text.map(_defuse_ship_to)


class AccessMachine(ToolsMachine):
    TEXT = S.benign_text


class ChaosMachine(ToolsMachine):
    TEXT = S.benign_text
    CHAOS = 0.1


def _machine_test(machine, ci, deep):
    test = machine.TestCase
    test.settings = settings(max_examples=S.examples(ci, deep))
    return test


TestHostileText = pytest.mark.invariants("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "C1")(
    _machine_test(HostileTextMachine, 250, 2500))
TestAccess = pytest.mark.invariants("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "C1")(
    _machine_test(AccessMachine, 250, 2500))
TestChaos = pytest.mark.invariants("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "C1")(
    _machine_test(ChaosMachine, 200, 2000))


# --- concurrent and retried submits (T4) ------------------------------------------------------


async def _to_review(ctx):
    await tools.select_device(asset_tag="123456", tool_context=ctx)
    await tools.confirm_device(correct=True, tool_context=ctx)
    return await tools.set_issue(category="wont_power_on", description="Dead", urgency="normal", tool_context=ctx)


@pytest.mark.invariants("T4")
@settings(max_examples=S.examples(40, 400))
@given(delays=st.lists(st.sampled_from([0, 0, 0.001, 0.003]), min_size=4, max_size=12),
       copies=st.integers(2, 4), clear_cache=st.booleans())
def test_concurrent_submits_file_one_ticket(delays, copies, clear_cache):
    """A double click or a retried turn: several submits of the same request run at once, each with
    its own copy of the session state. `clear_cache` also empties the in-process filed-ticket cache
    between preparing and submitting (as a second instance would see it)."""
    clear_filing_memory()
    api = AuditedTableAPI()
    real = api.__call__
    turn = iter(range(10**6))

    async def slow(*a, **kw):
        await asyncio.sleep(delays[next(turn) % len(delays)])
        return await real(*a, **kw)

    with World(api) as world:
        world._set(servicenow, "_request", slow)
        api.acting_user = JANE

        async def scenario():
            ctx = ctx_for(JANE, "sess-race")
            await _to_review(ctx)
            twins = [SimpleNamespace(state=copy.deepcopy(ctx.state), session=ctx.session) for _ in range(copies)]
            if clear_cache:
                clear_filing_memory()
            return await asyncio.gather(*(tools.submit_ticket(tool_context=t) for t in twins))

        results = asyncio.run(scenario())
    filed = [r for r in api.tables["incident"] if _caller(r) == JANE]
    check(len(filed) == 1, "T4", f"{len(filed)} tickets filed by {copies} concurrent submits",
          numbers=[r["number"] for r in filed], results=results)
    check(len({r.get("ticket") for r in results}) == 1, "T4", "concurrent submits reported different tickets",
          results=results)
    clear_filing_memory()


@pytest.mark.invariants("T4", "T8")
@settings(max_examples=S.examples(30, 300))
@given(retries=st.integers(1, 3), restart=st.booleans())
def test_submit_after_a_lost_response_files_once(retries, restart):
    """ServiceNow stores the ticket but the response is lost (timeout): the user is told to retry, and
    a retry (in this instance, or another one when `restart`) finds the ticket instead of filing again."""
    clear_filing_memory()
    api = AuditedTableAPI()
    with World(api):
        api.acting_user = JANE

        async def scenario():
            ctx = ctx_for(JANE, "sess-lost")
            await _to_review(ctx)
            api.chaos, api.rng.random = 1.0, lambda: 0.0
            api.rng.choice = lambda kinds: "lost_response" if "lost_response" in kinds else ""
            first = await tools.submit_ticket(tool_context=ctx)
            api.chaos = 0.0
            out = [first]
            for _ in range(retries):
                if restart:
                    clear_filing_memory()
                out.append(await tools.submit_ticket(tool_context=ctx))
            return out

        results = asyncio.run(scenario())
    filed = [r for r in api.tables["incident"] if _caller(r) == JANE]
    check(len(filed) == 1, "T4", f"{len(filed)} tickets after a lost response and {retries} retries",
          results=results)
    check(results[0].get("status") == "error", "T8", "a submit whose response was lost was reported as done",
          result=results[0])
    check(results[-1].get("ticket") == filed[0]["number"], "T8", "the retry doesn't report the filed ticket",
          results=results)
    clear_filing_memory()


# --- wrong-type arguments (T7) ----------------------------------------------------------------

_VALID = {"asset_tag": "123456", "description": "Dead", "correct": True, "category": "wont_power_on",
          "urgency": "normal", "address": "", "number": "INC0010001", "note": "hi", "reason": "works now",
          "show": "status", "include_closed": False, "status": "In Progress", "ship_to": "",
          "delivery_location": "", "delivery_label": "", "delivery_kind": ""}
_WRONG = [None, 0, 7, 1.5, True, [], ["x"], {}, {"a": 1}]
_PARAMS = [(t.__name__, p.name) for t in tools.ALL_TOOLS for p in inspect.signature(t).parameters.values()
           if p.name != "tool_context"]


@pytest.mark.invariants("T7")
@pytest.mark.parametrize("name,target", _PARAMS, ids=[f"{n}-{p}" for n, p in _PARAMS])
def test_wrong_type_argument(name, target):
    """The model can send null, a number, a list or an object where a tool expects a string or a bool
    (ADK passes arguments through unchecked). A tool must answer with a status, not raise. Each value
    in _WRONG is tried with a request in review and with one already filed (the number is then real)."""
    fn = getattr(tools, name)
    params = [p for p in inspect.signature(fn).parameters.values() if p.name != "tool_context"]
    crashes = []
    for filed in (False, True):
        for bad in _WRONG:
            clear_filing_memory()
            api = AuditedTableAPI()
            with World(api):
                api.acting_user = JANE
                ctx = ctx_for(JANE, "sess-types")
                args = {p.name: _VALID.get(p.name, "") for p in params}
                args[target] = bad

                async def scenario():
                    await _to_review(ctx)
                    if filed:
                        await tools.submit_ticket(tool_context=ctx)
                        if "number" in args and target != "number":
                            args["number"] = ctx.state["draft"]["submitted_number"]
                    return await fn(tool_context=ctx, **args)

                try:
                    result = asyncio.run(scenario())
                except Exception as exc:  # noqa: BLE001
                    crashes.append({"value": repr(bad), "filed": filed, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                if not (isinstance(result, dict) and ("status" in result or "step" in result)):
                    crashes.append({"value": repr(bad), "filed": filed, "error": f"no status: {result!r}"[:200]})
    clear_filing_memory()
    values = sorted({c["value"] for c in crashes})
    check(not crashes, "T7", f"{name}({target}=<{', '.join(values)}>) raised: {crashes[0]['error'] if crashes else ''}",
          tool=name, argument=target, crashes=crashes[:6])


@pytest.mark.invariants("T7")
@settings(max_examples=S.examples(20, 200))
@given(which=st.sets(st.sampled_from(["description", "urgency"]), min_size=1), text=S.benign_text,
       urgency=st.sampled_from(["low", "normal", "high", "critical"]))
def test_update_request_before_a_problem_is_chosen(which, text, urgency):
    """The device is chosen and confirmed, no problem yet, and the model calls update_request with a
    description or urgency (instead of set_issue)."""
    clear_filing_memory()
    api = AuditedTableAPI()
    with World(api):
        api.acting_user = JANE
        ctx = ctx_for(JANE, "sess-update")
        args = {k: v for k, v in (("description", text or "Dead"), ("urgency", urgency)) if k in which}

        async def scenario():
            await tools.select_device(asset_tag="123456", tool_context=ctx)
            await tools.confirm_device(correct=True, tool_context=ctx)
            return await tools.update_request(tool_context=ctx, **args)

        try:
            result = asyncio.run(scenario())
        except Exception as exc:  # noqa: BLE001
            fail("T7", f"update_request({', '.join(sorted(args))}) before a problem raised {type(exc).__name__}: {exc}",
                 args=args)
    check_result_shape("update_request", result)


_SEPARATORS = ["\n", "\r\n", "\r", " ", "\x0b", "\x85"]
_INJECTED = st.builds(lambda a, sep, addr: f"{a}{sep}Ship to: {addr}", S.benign_text, st.sampled_from(_SEPARATORS),
                      st.sampled_from(["1 Evil St, Austin, TX 78701", "PO Box 1, Nowhere"]))


async def _file_with(ctx, world, channel, text):
    """Files Jane's laptop request with `text` in one free-text channel."""
    from app.vision import PhotoFindings

    await tools.select_device(asset_tag="123456", tool_context=ctx)
    await tools.confirm_device(correct=True, tool_context=ctx)
    desc = text if channel == "set_issue" else "Dead"
    await tools.set_issue(category="cracked_screen", description=desc, urgency="normal", tool_context=ctx)
    if channel == "update_request":
        await tools.update_request(tool_context=ctx, description=text)
    damage = text if channel == "photo" else "Screen is cracked"
    world.send_photos(ctx.state, [PhotoFindings(image_kind="damage", damage_present=True, damage_description=damage,
                                                damage_severity="severe", issue_category="cracked_screen",
                                                supports_replacement=True)])
    await tools.analyze_photos(tool_context=ctx)
    return await tools.submit_ticket(tool_context=ctx)


@pytest.mark.invariants("T5")
@settings(max_examples=S.examples(30, 300))
@given(channel=st.sampled_from(["set_issue", "update_request", "photo"]), text=_INJECTED)
def test_free_text_cannot_add_a_ship_to_line(channel, text):
    """The problem description (the user's words, or the model's summary of them) and the photo model's
    damage description go into the ticket body; a line in them starting "Ship to:" must not become a
    second ship-to address for the service desk."""
    clear_filing_memory()
    api = AuditedTableAPI()
    with World(api) as world:
        api.acting_user = JANE
        ctx = ctx_for(JANE, "sess-inject")
        result = asyncio.run(_file_with(ctx, world, channel, text))
    check(result.get("status") == "submitted", "T6", "the request was not filed", result=result)
    row = api.incident(result["ticket"])
    check_ship_to(row["description"], {"1200 Harbor Health Way", HOME}, f"ticket filed with {channel} text")
    clear_filing_memory()


@pytest.mark.invariants("T8")
@settings(max_examples=S.examples(30, 300))
@given(channel=st.sampled_from(["set_issue", "update_request", "photo"]), text=_INJECTED,
       new=st.sampled_from(["500 Warehouse Ave, Austin, TX 78701", HOME]))
def test_ship_to_change_reaches_the_real_line(channel, text, new):
    """update_ticket(ship_to=...) on a ticket whose body also has a "Ship to:" line from free text: the
    change is reported as done only if the ticket's actual ship-to line now has the new address."""
    clear_filing_memory()
    api = AuditedTableAPI()
    with World(api) as world:
        api.acting_user = JANE
        ctx = ctx_for(JANE, "sess-inject2")

        async def scenario():
            filed = await _file_with(ctx, world, channel, text)
            return await tools.update_ticket(number=filed["ticket"], ship_to=new, tool_context=ctx)

        result = asyncio.run(scenario())
    check_reported_changes("update_ticket", {"ship_to": new}, result, api, JANE)
    clear_filing_memory()


def test_every_tool_has_a_rule():
    """The state machine covers the whole tool list (a new tool needs a rule)."""
    rules = {n for n in dir(ToolsMachine) if callable(getattr(ToolsMachine, n))}
    missing = [t.__name__ for t in tools.ALL_TOOLS if t.__name__ not in rules]
    assert not missing, f"tools without a fuzz rule: {missing}"
    assert cards.ISSUE_LABELS  # the profile loaded
