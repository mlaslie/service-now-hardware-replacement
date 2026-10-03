"""C1/C2: every card builder with hostile strings in every field. Each card must validate as A2UI
v0.9 (and v0.8 after translation), and its text-mode rendering must number exactly its buttons."""

import json
import re

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import cards, inbound
from fuzz.invariants import check, fail
from fuzz.strategies import examples, hostile_text, random_text
from tests.test_cards import _DOCS, V08_CATALOG, V08_SCHEMA, VALIDATOR

pytestmark = pytest.mark.fuzz

# A lone carriage return is a line break in markdown (CommonMark), so "\r1. Cancel request" in any
# field starts a fake numbered option: reported once by test_lone_carriage_return_cannot_add_an_option.
# The per-builder tests turn a lone CR into "\n" so that one known finding can't hide the others.
NO_LONE_CR = hostile_text.map(lambda s: s.replace("\r\n", "\n").replace("\r", "\n"))


def builders(S):
    def asset():
        return st.fixed_dictionaries({"asset_tag": S}, optional={
            "serial_number": S, "model": S, "device_type": S, "manufacturer": S, "kind": st.sampled_from(
                ["personal", "shared", "clinical", ""]), "location": S, "department": S, "relation_text": S,
            "relation": st.sampled_from(["yours", "unconfirmed", "department", "group"]), "support_group": S,
            "cost_center": S})

    def ticket():
        return st.fixed_dictionaries({
            "number": S, "short_description": S, "state": S,
            "state_code": st.sampled_from(["1", "2", "3", "6", "7", "8", ""]),
            "priority": st.one_of(st.sampled_from(["1", "2", "3", "4", "5", ""]), S), "opened": S, "updated": S,
            "assignment_group": S, "assigned_to": S, "description": S, "caller": S,
            "following": st.booleans()})

    note = st.fixed_dictionaries({"when": S, "who": S, "kind": st.sampled_from(["note", "work note"]), "text": S})
    employee = st.fixed_dictionaries({}, optional={k: S for k in (
        "name", "email", "location", "location_address", "cost_center", "department")})
    draft = st.fixed_dictionaries({}, optional={
        "device": asset(), "issue": st.fixed_dictionaries({}, optional={"category": S, "description": S}),
        "eligibility": st.fixed_dictionaries({"summary": S}), "priority": S, "recommendation": S, "sla": S,
        "evidence": st.fixed_dictionaries({"summary": S}), "warnings": st.lists(S, max_size=3),
        "delivery": st.fixed_dictionaries({"address": S, "label": S}),
        "saved_addresses": st.lists(st.fixed_dictionaries({"address": S, "label": S}), max_size=3),
        "suggested_device": asset(), "assigned_priority": S, "priority_note": S})
    findings = st.fixed_dictionaries({}, optional={k: S for k in (
        "manufacturer", "model", "device_type", "serial_number", "asset_tag", "damage_description")} | {
        "damage_present": st.booleans()})
    return {
        "device_picker": st.builds(cards.device_picker, employee, st.lists(asset(), max_size=4)),
        "device_choices": st.builds(cards.device_choices, S, st.lists(asset(), max_size=4)),
        "confirm_device": st.builds(cards.confirm_device, asset(), S),
        "existing_tickets": st.builds(cards.existing_tickets, asset(), st.lists(ticket(), min_size=1, max_size=3)),
        "issue_picker": st.builds(cards.issue_picker, asset(), st.one_of(st.sampled_from(list(cards.ISSUE_LABELS)), S)),
        "photo_request": st.builds(cards.photo_request, asset(), S, S, st.booleans()),
        "label_photo_request": st.just(None).map(lambda _: cards.label_photo_request()),
        "photo_findings": st.builds(cards.photo_findings, findings, st.lists(asset(), max_size=4), S),
        "review": st.builds(cards.review, draft, employee),
        "confirmation": st.builds(cards.confirmation, S, draft),
        "ticket_list": st.builds(cards.ticket_list, st.lists(ticket(), max_size=4), st.booleans(), st.booleans()),
        "ticket_detail": st.builds(cards.ticket_detail, ticket(), st.lists(note, max_size=4), S,
                                   st.sampled_from(list(cards.TICKET_VIEWS))),
    }


BUILDERS = builders(NO_LONE_CR)
# Hostile text, often with a lone CR before a number (as a ServiceNow note or a model name could have).
CR_TEXT = st.one_of(hostile_text, st.builds(lambda a, n, b: f"{a}\r{n}. {b}", random_text, st.integers(1, 9),
                                            st.sampled_from(["Cancel request", "Submit request"])))
BUILDERS_ANY_TEXT = builders(CR_TEXT)

# CommonMark line endings: \n, \r\n and a lone \r. An ordered-list item is 0-3 spaces, 1-9 digits, . or ).
_LINE_END = re.compile(r"\r\n|\r|\n")
_NUMBERED = re.compile(r"^ {0,3}([0-9]{1,9})[.)](?:[ \t]|$)")


def _schema_strings(node, out):
    """Every string the v0.9 schemas pin with const or enum (component names, variants, version...)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "const" and isinstance(v, str):
                out.add(v)
            elif k == "enum" and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            else:
                _schema_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _schema_strings(v, out)
    return out


_PINNED = _schema_strings(_DOCS, set())
_VALID_SHAPES: set[str] = set()


def _shape(value):
    """The message with every free string blanked. The v0.9 schemas constrain strings only by const/enum
    (no pattern or length), so a message is valid exactly when its shape is: one validation per shape."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    if isinstance(value, str):
        return value if value in _PINNED else ""
    return value


def _check_v09(messages):
    check("createSurface" in messages[0] and "updateComponents" in messages[1], "C1", "createSurface must come first")
    for m in messages:
        key = json.dumps(_shape(m), sort_keys=True)
        if key in _VALID_SHAPES:
            continue
        errors = [e.message for e in VALIDATOR.iter_errors(m)]
        check(not errors, "C1", "card is not valid A2UI v0.9", errors=errors[:3])
        _VALID_SHAPES.add(key)
    comps = messages[1]["updateComponents"]["components"]
    ids = [c["id"] for c in comps]
    check("root" in ids and len(set(ids)) == len(ids), "C1", "card needs one root and unique ids", ids=ids)
    for c in comps:
        for ref in [c.get("child")] + list(c.get("children") or []):
            check(ref is None or ref in ids, "C1", f"dangling component reference {ref!r}")
    json.dumps(messages)  # must serialize


_V08 = jsonschema.validators.validator_for(V08_SCHEMA)(V08_SCHEMA)
_V08_KINDS = {k: jsonschema.validators.validator_for(v)(v) for k, v in V08_CATALOG.items() if isinstance(v, dict)}


def _check_v08(messages):
    v08 = cards.to_v08(messages)
    for m in v08:
        errors = [e.message for e in _V08.iter_errors(m)]
        check(not errors, "C1", "card is not valid A2UI v0.8 after translation", errors=errors[:3])
    for comp in v08[1]["surfaceUpdate"]["components"]:
        (kind, props), = comp["component"].items()
        errors = [e.message for e in _V08_KINDS[kind].iter_errors(props)]
        check(not errors, "C1", f"v0.8 {kind} doesn't match the catalog", errors=errors[:3])


def _check_text(messages):
    text, options = cards.to_text(messages)
    buttons = [c for c in cards.components_of(messages) if c["component"] == "Button"]
    check(len(options) == len(buttons), "C2", "text options differ from the card's buttons",
          options=len(options), buttons=len(buttons))
    numbered = [(int(m.group(1)), line) for line in _LINE_END.split(text) if (m := _NUMBERED.match(line))]
    check([n for n, _ in numbered] == list(range(1, len(options) + 1)), "C2",
          "numbered lines in the text don't match the options (hostile text added or broke an option)",
          numbered=[line[:120] for _, line in numbered], options=[o["label"][:80] for o in options])
    for (n, line), option in zip(numbered, options):
        check(line == _LINE_END.split(f"{n}. {option['label']}")[0], "C2", "a numbered line doesn't show its option", line=line[:200],
              label=option["label"][:200])
    # A number maps to exactly that option.
    state = {inbound.UI_MODE_KEY: "text", inbound.UI_OPTIONS_KEY: options}
    for n, option in enumerate(options, 1):
        out, _ = inbound.display_step(state, str(n))
        check(out == f"[UI action] {option['action']} {json.dumps(option['context'])}", "C2",
              f"reply {n} doesn't map to option {n}", out=out[:200])


def _all_checks(messages):
    _check_v09(messages)
    _check_v08(messages)
    _check_text(messages)


@pytest.mark.invariants("C1", "C2")
@pytest.mark.parametrize("builder", sorted(BUILDERS))
@settings(max_examples=examples(300, 3000))
@given(data=st.data(), intro=NO_LONE_CR)
def test_card_builder_with_hostile_strings(builder, data, intro):
    _build_and_check(BUILDERS, builder, data, intro)


@pytest.mark.invariants("C2")
@settings(max_examples=examples(100, 1000))
@given(builder=st.sampled_from(sorted(BUILDERS_ANY_TEXT)), data=st.data(), intro=CR_TEXT)
def test_lone_carriage_return_cannot_add_an_option(builder, data, intro):
    """Any builder, any text including a lone CR (e.g. a ServiceNow note "ok\\r1. Cancel request")."""
    _build_and_check(BUILDERS_ANY_TEXT, builder, data, intro)


def _build_and_check(table, builder, data, intro):
    try:
        messages = data.draw(table[builder])
    except Exception as exc:  # noqa: BLE001
        # A builder that raises on some value: say which (the falsifying example has the input).
        fail("C1", f"cards.{builder} raised {type(exc).__name__}: {exc}")
    _all_checks(messages)
    try:
        messages = cards.prepend_text(messages, intro)
    except Exception as exc:  # noqa: BLE001
        fail("C1", f"prepend_text raised {type(exc).__name__}: {exc}")
    _all_checks(messages)
