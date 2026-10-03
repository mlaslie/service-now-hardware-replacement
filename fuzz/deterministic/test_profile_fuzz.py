"""P1/P2: mutated organization profiles. A profile either loads, or fails with a ProfileError that
names the field; a profile that loads works: every consumer in this process, and a subprocess smoke
run of the agent (instruction, tools, cards) started with it."""

import asyncio
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app import profile
from fuzz.invariants import check, fail
from fuzz.strategies import PAYLOADS, examples, hostile_text, huge_text, wrong_type

pytestmark = pytest.mark.fuzz

ROOT = Path(__file__).resolve().parents[2]
BASE = yaml.safe_load(profile.DEFAULT_PATH.read_text(encoding="utf-8"))


def _paths(node, prefix=()):
    out = [prefix] if prefix else []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _paths(v, prefix + (i,))
    return out


PATHS = _paths(BASE)
KEY_PATHS = [p for p in PATHS if isinstance(p[-1], str)]
TEMPLATE_PATHS = [p for p in PATHS if p[:2] == ("servicenow", "ticket_fields") and len(p) == 3] + \
    [p for p in PATHS if p[:2] == ("service", "recommendations") and len(p) == 3] + [("agent", "persona")]
BRACES = ["{0}", "{}", "{issue_key.x}", "{issue_key[0]}", "{unknown}", "{issue_key!r}", "{issue_key:>9}", "{", "}",
          "{{literal}}", "{issue_key}", "{cost_center}", "{organization}", "{draft}", "{artifact.x}", "{ name }"]

_value = st.one_of(hostile_text, huge_text, wrong_type, st.sampled_from(BRACES),
                   st.lists(hostile_text, max_size=3), st.dictionaries(st.text(max_size=8), hostile_text, max_size=3))
MUTATION = st.one_of(
    st.tuples(st.just("delete"), st.sampled_from(PATHS)),
    st.tuples(st.just("rename"), st.sampled_from(KEY_PATHS), st.sampled_from(["x", "_", "S", " "])),
    st.tuples(st.just("set"), st.sampled_from(PATHS), _value),
    st.tuples(st.just("dup_issue"), st.sampled_from(["personal", "equipment"]), st.integers(0, 10)),
    st.tuples(st.just("template"), st.sampled_from(TEMPLATE_PATHS), st.sampled_from(BRACES), st.booleans()),
    st.tuples(st.just("query_value"), st.sampled_from([("servicenow", "ticket_category"), ("servicenow", "asset_tables", 0)]),
              st.sampled_from([p for p in PAYLOADS if "^" in p] + ["hardware", "alm_hardware", "x y"])),
)
# Not-UTF-8 files ("latin1", "garbage") have their own test below, so that known finding can't hide others.
TEXT_MUTATION = st.sampled_from(["none", "none", "none", "truncate", "tabs", "alias_chain", "empty", "list_top", "bom",
                                 "dup_key"])


def _get(node, path):
    for p in path:
        node = node[p]
    return node


def _apply(data, mutation):
    kind, path, *rest = mutation
    try:
        if kind == "dup_issue":
            items = data["issues"][path]
            items.append(copy.deepcopy(items[rest[0] % len(items)]))
            return ("issues",)
        parent = _get(data, path[:-1]) if path else None
        if kind == "delete":
            del parent[path[-1]]
        elif kind == "rename":
            parent[path[-1] + rest[0]] = parent.pop(path[-1])
        elif kind == "set" or kind == "query_value":
            parent[path[-1]] = rest[0]
        elif kind == "template":
            parent[path[-1]] = (str(parent[path[-1]]) + " " + rest[0]) if rest[1] else rest[0]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None  # an earlier mutation removed or replaced this path
    return path[:1]


def _text(data, how):
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    if how == "truncate":
        return text[: len(text) * 2 // 3].encode()
    if how == "tabs":
        return text.replace("  ", "\t", 3).encode()
    if how == "alias_chain":
        chain = "a0: &a0 [x, x]\n" + "".join(f"a{i}: &a{i} [*a{i-1}, *a{i-1}]\n" for i in range(1, 12))
        return (chain + text).encode()
    if how == "latin1":
        return text.replace("Riverside", "Clínica Señora").encode("latin-1", "replace")
    if how == "garbage":
        return b"\xff\xfe\x00garbage\x80\x81"
    if how == "empty":
        return b""
    if how == "list_top":
        return b"- organization: x\n"
    if how == "bom":
        return ("﻿" + text).encode("utf-8")
    if how == "dup_key":
        return (text + "\norganization: Duplicate\n").encode()
    return text.encode("utf-8")


def _write(content: bytes) -> Path:
    fd, name = tempfile.mkstemp(suffix=".yaml", prefix="profile_", dir=_TMP)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return Path(name)


_TMP = tempfile.mkdtemp(prefix="fuzz_profiles_")


def _mutated(mutations, how):
    data = copy.deepcopy(BASE)
    tops = set()
    for m in mutations:
        top = _apply(data, m)
        if top:
            tops.add(top[0])
    return data, tops, _text(data, how)


def _load(path):
    try:
        return profile.load(path), None
    except profile.ProfileError as exc:
        return None, exc
    except Exception as exc:  # noqa: BLE001
        fail("P1", f"loading the profile raised {type(exc).__name__}, not ProfileError: {exc}"[:400],
             profile_head=path.read_bytes()[:300].decode("utf-8", "replace"))


@pytest.mark.invariants("P1")
@settings(max_examples=examples(20, 200))
@given(mutations=st.lists(MUTATION, max_size=1), how=st.sampled_from(["latin1", "garbage"]))
def test_profile_saved_in_another_encoding(mutations, how):
    """A profile edited on Windows and saved as Latin-1 / cp1252, or a file that isn't text at all."""
    test_profile_loads_or_names_the_field.hypothesis.inner_test(mutations, how)


@pytest.mark.invariants("P1")
@settings(max_examples=examples(300, 3000))
@given(mutations=st.lists(MUTATION, min_size=1, max_size=3), how=TEXT_MUTATION)
def test_profile_loads_or_names_the_field(mutations, how):
    data, tops, content = _mutated(mutations, how)
    path = _write(content)
    try:
        _, err = _load(path)
    finally:
        path.unlink(missing_ok=True)
    if err is None:
        return
    msg = str(err)
    check(str(path) in msg or "Organization profile" in msg, "P1", "the error doesn't name the profile", error=msg[:500])
    if "is not valid YAML" in msg:
        return
    locs = re.findall(r"^  - (.+?): ", msg, re.M)
    check(locs, "P1", "the error lists no field", error=msg[:800])
    if how == "none" and tops:
        # A renamed key is named as written ("brandingx"), so a prefix match counts.
        check(any(loc == "(top level)" or any(loc.split(".")[0].startswith(t) for t in tops) for loc in locs), "P1",
              f"the error names {locs[:4]}, not the mutated field(s) {sorted(tops)}", error=msg[:800],
              mutations=[m[:2] for m in mutations])


def _exercise(p) -> None:
    """Every consumer of a loaded profile the app has, called in this process."""
    from google.adk.utils.instructions_utils import inject_session_state

    from app import agent

    labels = p.issue_labels
    for key in list(labels) + ["", "nope"]:
        p.issue(key), p.photo_policy(key), p.is_safety(key), p.min_urgency(key)
    p.keys("personal"), p.keys("equipment"), p.safety_keys
    values = {k: v for k, v in zip(sorted(profile.TEMPLATE_KEYS), ["x", "{0}", "", "{issue_key}", "é", "1", "a", "b", "c"])}
    p.servicenow.render_fields(values)
    p.servicenow.device_value("laptop", "Computer")
    p.service.recommendations.charged_replacement.format(cost_center="Radiology")
    for prio in profile.PRIORITY_KEYS:
        p.service.personal_response_targets[prio], p.service.equipment_response_targets[prio]
    for u in profile.URGENCY_ORDER:
        p.servicenow.urgency_matrix[u]
    p.devices.refresh_years.get("laptop", p.devices.default_refresh_years) >= 0
    # The agent's instruction as app/agent.py builds it from the profile, rendered as ADK does every turn.
    instruction = p.agent.persona.strip() + "\n\n" + agent._FLOW.replace("SAFETY_KEY", (p.safety_keys or ["other"])[0])
    session = SimpleNamespace(state={"end_user": {}, "draft": {}, "ui_mode": "cards", "a2ui_version": "0.9"},
                              app_name="hardware_replacement", user_id="u", id="s")
    ctx = SimpleNamespace(_invocation_context=SimpleNamespace(session=session, artifact_service=None), agent_name="a")
    asyncio.run(inject_session_state(instruction, ctx))


@pytest.mark.invariants("P2")
@settings(max_examples=examples(300, 3000))
@given(mutations=st.lists(MUTATION, min_size=1, max_size=3))
def test_a_profile_that_loads_works(mutations):
    data, _, content = _mutated(mutations, "none")
    path = _write(content)
    try:
        p, err = _load(path)
    finally:
        path.unlink(missing_ok=True)
    assume(p is not None)
    # A {placeholder} in the persona breaks every turn: reported by test_persona_with_a_placeholder_works.
    # Defused here so that known finding can't hide the others.
    p.agent.persona = re.sub(r"[{}]", "", p.agent.persona)
    try:
        _exercise(p)
    except Exception as exc:  # noqa: BLE001
        fail("P2", f"the profile loads, but {type(exc).__name__}: {exc}"[:400], mutations=[repr(m)[:200] for m in mutations])


@pytest.mark.invariants("P2")
@settings(max_examples=examples(30, 300))
@given(placeholder=st.sampled_from(["{organization}", "{name}", "{draft}", "{artifact.notes}", "{ user }"]),
       where=st.sampled_from(["start", "end"]))
def test_persona_with_a_placeholder_works(placeholder, where):
    """The persona is free text; an organization may well write "{organization}" or "{name}" in it."""
    data = copy.deepcopy(BASE)
    persona = data["agent"]["persona"]
    data["agent"]["persona"] = f"{placeholder} {persona}" if where == "start" else f"{persona} Sign as {placeholder}."
    path = _write(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode())
    try:
        p, err = _load(path)
    finally:
        path.unlink(missing_ok=True)
    if p is None:
        check("persona" in str(err), "P1", "the persona was refused without naming it", error=str(err)[:500])
        return
    try:
        _exercise(p)
    except Exception as exc:  # noqa: BLE001
        fail("P2", f"persona with {placeholder!r} loads, but every turn would fail: {type(exc).__name__}: {exc}"[:400])


# Mutations that keep a profile loadable, for the (slower) subprocess smoke run.
_SMOKE = st.one_of(
    st.tuples(st.just("set"), st.sampled_from([p for p in PATHS if isinstance(_get(BASE, p), str)]),
              st.one_of(hostile_text, st.sampled_from(["Ünïcödé 東京", "**bold**", "x" * 5000]))),
    st.tuples(st.just("template"), st.sampled_from(TEMPLATE_PATHS[:-1]), st.sampled_from(["{issue_key}", "{{x}}", "{location}"]),
              st.booleans()),
    # Query operators in ticket_category / asset_tables: test_query_operators_in_servicenow_settings.
    st.tuples(st.just("query_value"), st.sampled_from([("servicenow", "ticket_category"), ("servicenow", "asset_tables", 0)]),
              st.sampled_from(["alm_hardware", "hardware", "Hardware", "x y"])),
    st.tuples(st.just("delete"), st.sampled_from([("servicenow", "ticket_fields"), ("servicenow", "device_values"),
                                                  ("devices", "refresh_years"), ("branding",)])),
)


@pytest.mark.invariants("P2")
@settings(max_examples=examples(5, 25))
@given(mutations=st.lists(_SMOKE, min_size=1, max_size=3))
def test_agent_runs_with_a_mutated_profile(mutations):
    data, _, content = _mutated(mutations, "none")
    path = _write(content)
    try:
        p, _ = _load(path)
        assume(p is not None)
        env = {**os.environ, "ORGANIZATION_PROFILE": str(path), "SN_INSTANCE_URL": "https://sn.fuzz.invalid"}
        proc = subprocess.run([sys.executable, str(ROOT / "fuzz" / "deterministic" / "profile_smoke.py")],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    finally:
        path.unlink(missing_ok=True)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        fail("P2", "the smoke run printed no result", stderr=proc.stderr[-2000:], mutations=[repr(m)[:200] for m in mutations])
    check(out["ok"], out.get("invariant") or "P2", f"agent with the mutated profile failed: {out['error'][:600]}",
          mutations=[repr(m)[:200] for m in mutations], steps=out["steps"][-6:])


def _smoke(data) -> dict:
    path = _write(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode())
    try:
        p, err = _load(path)
        if p is None:
            return {"ok": True, "refused": str(err)}
        env = {**os.environ, "ORGANIZATION_PROFILE": str(path), "SN_INSTANCE_URL": "https://sn.fuzz.invalid"}
        proc = subprocess.run([sys.executable, str(ROOT / "fuzz" / "deterministic" / "profile_smoke.py")],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    finally:
        path.unlink(missing_ok=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.invariants("P2", "T3")
@pytest.mark.parametrize("field,value", [("ticket_category", "hardware^NQnumber=INC0090001"),
                                         ("ticket_category", "hardware^ORcaller_id!=x"),
                                         ("asset_tables", "alm_hardware?sysparm_query=x")])
def test_query_operators_in_servicenow_settings(field, value):
    """servicenow.ticket_category goes into every ticket query ("...^category=<it>"); a value with
    ServiceNow query operators either is refused when the profile loads, or must not widen the queries."""
    data = copy.deepcopy(BASE)
    data["servicenow"][field] = [value] if field == "asset_tables" else value
    out = _smoke(data)
    check(out["ok"], out.get("invariant") or "P2", f"profile loads with {field}={value!r}, then: {out['error'][:500]}",
          steps=out.get("steps", [])[-4:])
