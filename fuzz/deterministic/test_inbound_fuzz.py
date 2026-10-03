"""I1/I2: inbound parsing (`inbound.rewrite_parts`) and the display-mode state machine
(`inbound.display_step`) on random parts, clicks, states and texts."""

import asyncio
import json
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import inbound
from fuzz.invariants import check, fail
from fuzz.strategies import a2a_part, examples, hostile_text, is_click

pytestmark = pytest.mark.fuzz

_SENTINEL = re.compile(r"<(start|end)_of_user_uploaded_file:[^>]*>")


def _run(parts, failing_uploads):
    calls = []

    async def upload(data, mime):
        calls.append(len(data))
        if failing_uploads and failing_uploads[(len(calls) - 1) % len(failing_uploads)]:
            raise OSError("storage outage")
        return {"photo_id": f"ph_{len(calls)}", "uri": f"gs://b/{len(calls)}", "mime_type": mime, "bytes": len(data)}

    return asyncio.run(inbound.rewrite_parts(parts, upload))


@pytest.mark.invariants("I1")
@settings(max_examples=examples(500, 5000))
@given(parts=st.lists(a2a_part(), max_size=6), failing=st.lists(st.booleans(), max_size=3))
def test_rewrite_parts_never_raises_and_keeps_one_action_per_click(parts, failing):
    """Any parts, including clicks whose [{key, value}] context has keys that aren't strings."""
    _check_rewrite(parts, failing)


@pytest.mark.invariants("I1")
@settings(max_examples=examples(500, 5000))
@given(parts=st.lists(a2a_part(junk_keys=False), max_size=6), failing=st.lists(st.booleans(), max_size=3))
def test_rewrite_parts_with_string_context_keys(parts, failing):
    """The same with string keys only, so a failure above doesn't hide the rest of the space."""
    _check_rewrite(parts, failing)


def _check_rewrite(parts, failing):
    from a2a.types import DataPart, TextPart

    try:
        out, photos = _run(parts, failing)
    except Exception as exc:  # noqa: BLE001
        fail("I1", f"rewrite_parts raised {type(exc).__name__}: {exc}",
             parts=[p.root.model_dump(mode="json") for p in parts])
    check(out, "I1", "rewrite_parts returned no parts")
    texts = [p.root.text for p in out if isinstance(p.root, TextPart)]
    for t in texts:
        if t.startswith(("[Data] ", "[UI action] ")):
            continue  # JSON from a data part: a sentinel-like string there is data, not GE's upload wrapper
        check(not _SENTINEL.search(t), "I1", "upload sentinel reached the model", text=t[:300])
    clicks = sum(1 for p in parts if isinstance(p.root, DataPart) and isinstance(p.root.data, dict) and is_click(p.root.data))
    typed = {(_SENTINEL.sub("", p.root.text).strip()) for p in parts if isinstance(p.root, TextPart)}
    typed_actions = sum(1 for t in typed if t.startswith("[UI action]"))
    actions = sum(1 for t in texts if t.startswith("[UI action]"))
    check(actions == clicks + typed_actions, "I1", f"{clicks} clicks became {actions - typed_actions} actions",
          parts=[p.root.model_dump(mode="json") for p in parts], out=texts)
    if actions:
        check(inbound.CLICK_ECHO not in texts, "I1", "click echo text kept next to a click", out=texts)
    attached = [t for t in texts if t.startswith("[Photo attached: ") and t not in typed]
    check(len(attached) == len(photos), "I1", "photos staged and photo lines differ", out=texts, photos=len(photos))


_MODES = st.sampled_from([None, "", "asking", "text", "cards"])
_OPTION = st.fixed_dictionaries({
    "label": st.one_of(st.sampled_from(["Submit request", "Change something", "MacBook Air 13 (123456)"]), hostile_text),
    "action": st.sampled_from(["submit_ticket", "edit_request", "select_device", "list_tickets"]),
    "context": st.dictionaries(st.sampled_from(["asset_tag", "number", "address"]), hostile_text, max_size=2)})
# What reaches display_step: the text parts rewrite_parts kept (each stripped, never empty), joined.
_TYPED = hostile_text.map(str.strip).filter(bool)


def _reachable(state):
    """While asking, the pending message is never empty: it starts as the first message."""
    if state.get(inbound.UI_MODE_KEY) == "asking" and not (state.get(inbound.UI_PENDING_KEY) or "").strip():
        state[inbound.UI_PENDING_KEY] = "my screen is cracked"
    return state


_STATE = st.fixed_dictionaries({}, optional={
    inbound.UI_MODE_KEY: _MODES,
    inbound.UI_PENDING_KEY: st.one_of(st.just(""), _TYPED),
    inbound.UI_ASKS_KEY: st.one_of(st.none(), st.integers(0, 5)),
    inbound.UI_OPTIONS_KEY: st.lists(_OPTION, max_size=6)}).map(_reachable)
_SAID = st.one_of(
    st.sampled_from(["1", "2", "1)", "option 2", "#1", "3", "7", "mobile", "desktop", "I'm on my phone",
                     "1 desktop", "buttons", "text", "Text mode", "switch to buttons", "yes", "Hello",
                     '[UI action] select_device {"asset_tag": "123456"}', "[Photo attached: ph_1]"]),
    _TYPED, st.builds(lambda a, b: f"{a}\n{b}", _TYPED, _TYPED))


def _is_click(text: str) -> bool:
    return text.startswith("[UI action]")


@pytest.mark.invariants("I2", "C2")
@settings(max_examples=examples(500, 5000))
@given(state=_STATE, text=_SAID)
def test_display_step_keeps_the_pending_message_and_maps_numbers_exactly(state, text):
    try:
        out, delta = inbound.display_step(dict(state), text)
    except Exception as exc:  # noqa: BLE001
        fail("I2", f"display_step raised {type(exc).__name__}: {exc}", state=state, text=text)
    check(isinstance(out, str) and isinstance(delta, dict), "I2", "display_step returned the wrong types")
    check(out != "" or text == "", "I2", "display_step returned no text for a message", state=state, text=text)
    allowed = {inbound.UI_MODE_KEY, inbound.UI_PENDING_KEY, inbound.UI_ASKS_KEY}
    check(set(delta) <= allowed, "I2", f"unexpected state keys {sorted(set(delta) - allowed)}")
    if inbound.UI_MODE_KEY in delta:
        check(delta[inbound.UI_MODE_KEY] in ("text", "cards", "asking"), "I2", "unknown display mode",
              mode=delta[inbound.UI_MODE_KEY])

    mode = state.get(inbound.UI_MODE_KEY)
    pending = state.get(inbound.UI_PENDING_KEY) or ""
    if mode in (None, "") and not _is_click(text):
        check(out == inbound.ASK_MARKER and delta.get(inbound.UI_PENDING_KEY) == text, "I2",
              "first message not kept while asking", text=text, out=out, delta=delta)
    if mode == "asking" and not _is_click(text):
        # Clicks are excluded: a click needs a card, and no card is shown before the mode is known.
        kept = delta.get(inbound.UI_PENDING_KEY, pending) if out == inbound.ASK_MARKER else out
        check(pending.strip() in kept, "I2", "the pending first message was lost", state=state, text=text, out=out,
              delta=delta)
        if out == inbound.ASK_MARKER:
            check(text.strip() in delta.get(inbound.UI_PENDING_KEY, ""), "I2", "a non-answer was dropped while asking",
                  state=state, text=text, delta=delta)

    if mode == "text" and _is_click(out) and not _is_click(text) and out != inbound.SHOW_CURRENT:
        options = state.get(inbound.UI_OPTIONS_KEY) or []
        rendered = [f"[UI action] {o['action']} {json.dumps(o['context'])}" for o in options]
        check(out in rendered, "C2", "a reply became a click that is not one of the shown options", out=out, text=text)
        if text.strip().isdigit() and 1 <= int(text.strip()) <= len(options):
            n = int(text.strip())
            check(out == rendered[n - 1], "C2", f"reply {n} mapped to a different option", out=out,
                  expected=rendered[n - 1])
