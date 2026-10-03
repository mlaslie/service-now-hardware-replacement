"""Model-routing checks: does the model pick the right tools for what people actually say?

The unit tests run the tools directly. These run the **real agent with the real model**
against an in-memory ServiceNow (the same fake as tests/test_paths.py), one conversation
per case from evals/cases.yaml, and check which tools were called, with which arguments,
and what the resulting card shows. Nothing touches a real ServiceNow.

    uv run python evals/run.py                 # every case once
    uv run python evals/run.py --repeat 3      # each case 3 times (models vary)
    uv run python evals/run.py safety-photo    # cases whose id contains this text

Needs Google Cloud credentials for the model (gcloud auth application-default login)
and GOOGLE_CLOUD_PROJECT in .env. A run of every case costs a few cents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import warnings
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")

from app import config  # noqa: E402  (loads .env)

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", config.MODEL_LOCATION)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from app import agent as agent_mod, cards, memory, servicenow, tools, vision  # noqa: E402
from app.tools import filing  # noqa: E402
from app.vision import PhotoFindings  # noqa: E402
from tests import test_paths as fakes  # noqa: E402  (FakeTableAPI, PROFILES, ...)

CASES = Path(__file__).with_name("cases.yaml")


# --- a fresh fake world per case ---------------------------------------------------------


def install_fakes(case: dict) -> fakes.FakeTableAPI:
    api = fakes.FakeTableAPI()
    servicenow._request = api
    saved = {fakes.JANE_EMAIL: list(case.get("saved_addresses", []))}

    async def recall(ctx, email):
        return []

    async def remember(ctx, email):
        return None

    async def saved_addresses(email):
        return list(saved.get(email, []))

    async def save_address(email, label, address):
        api.saved_calls = getattr(api, "saved_calls", []) + [(label, address)]

    async def attach(ctx, sys_id, draft):
        return None

    memory.recall, memory.remember_conversation = recall, remember
    memory.saved_addresses, memory.save_address = saved_addresses, save_address
    filing._attach_photos = attach

    photo = case.get("photo")
    if photo:
        async def analyze(uri, mime, hint=""):
            return PhotoFindings(**photo)
        vision.analyze_photo = analyze

    for t in case.get("tickets", []):  # tickets that already exist
        user = fakes.JANE if case.get("user", "jane") == "jane" else fakes.JOHN
        api.tables["incident"].append({
            "sys_id": f"i{len(api.tables['incident']) + 1}", "number": t["number"], "state": t.get("state", "2"),
            "short_description": t.get("short_description", "Won't turn on: MacBook Air 13 123456"),
            "description": t.get("description", "Problem: Won't turn on\nShip to: 1200 Harbor Health Way"),
            "category": "hardware", "priority": "3", "urgency": "2", "impact": "2",
            "caller_id": {"value": user}, "caller_id.name": fakes.NAMES[user], "watch_list": "",
            "cmdb_ci": {"value": t.get("ci", "ci_a1")}, "assignment_group": {"value": ""},
            "sys_created_on": "2026-09-29 10:00:00", "sys_updated_on": "2026-09-30 09:00:00",
            "comments_log": []})
    return api


def initial_state(case: dict) -> dict:
    user = fakes.JANE if case.get("user", "jane") == "jane" else fakes.JOHN
    profile = {"sys_id": user, "location": fakes.HOSPITAL, "location_address": "1200 Harbor Health Way",
               "cost_center": "Radiology", **fakes.PROFILES[user]}
    return {"end_user": {"verified": True, "profile": profile}, "ui_mode": "cards", "a2ui_version": "0.9"}


# --- running a conversation ----------------------------------------------------------------


def _card_text(content: types.Content | None) -> str:
    texts, messages = [], []
    for part in (content.parts if content else []) or []:
        parsed = agent_mod._parse_blob(part)
        if parsed:
            messages.append(parsed.get("data", parsed))
        elif part.text and not part.thought:
            texts.append(part.text)
    if messages:
        try:
            texts.append(cards.to_text(messages)[0])
        except Exception:  # noqa: BLE001
            texts.append(json.dumps(messages)[:500])
    return "\n".join(texts)


async def run_case(case: dict) -> list[dict]:
    install_fakes(case)
    runner = InMemoryRunner(agent=agent_mod.root_agent, app_name=config.APP_NAME)
    session = await runner.session_service.create_session(
        app_name=config.APP_NAME, user_id="eval", state=initial_state(case))
    results = []
    for i, turn in enumerate(case["turns"]):
        text = turn["say"]
        delta = {}
        if "[Photo attached:" in text:
            pid = text.split("[Photo attached:")[1].split("]")[0].strip()
            delta = {f"photo:{pid}": {"photo_id": pid, "uri": f"gs://eval/{pid}.jpg", "mime_type": "image/jpeg"},
                     "last_photo_ids": [pid]}
        calls, reply = [], ""
        async for event in runner.run_async(
                user_id="eval", session_id=session.id, state_delta=delta or None,
                new_message=types.Content(role="user", parts=[types.Part(text=text)])):
            for fc in event.get_function_calls() or []:
                calls.append({"name": fc.name, "args": dict(fc.args or {})})
            if event.content and event.content.role == "model" and not event.get_function_calls():
                got = _card_text(event.content)
                if got:
                    reply = got
        results.append({"turn": i + 1, "say": text, "calls": calls, "reply": reply,
                        "problems": check_turn(turn.get("expect") or {}, calls, reply)})
    return results


# --- expectations ------------------------------------------------------------------------------


def _matches(want, got) -> bool:
    if isinstance(want, str):
        return want.lower() in str(got).lower()
    return want == got


def check_turn(expect: dict, calls: list[dict], reply: str) -> list[str]:
    problems = []
    names = [c["name"] for c in calls]
    pos = 0
    for want in expect.get("calls", []):  # in this order; other calls may come in between
        try:
            pos = names.index(want, pos) + 1
        except ValueError:
            problems.append(f"expected a call to {want} (got {names or 'no calls'})")
    for name in expect.get("not_calls", []):
        if name in names:
            problems.append(f"must not call {name}")
    for name, args in (expect.get("args") or {}).items():
        made = [c["args"] for c in calls if c["name"] == name]
        if made and not any(all(_matches(v, a.get(k, "")) for k, v in args.items()) for a in made):
            problems.append(f"{name} args {made} don't match {args}")
    if expect.get("no_calls") and names:
        problems.append(f"expected no tool calls, got {names}")
    for text in expect.get("reply_contains", []):
        if text.lower() not in reply.lower():
            problems.append(f"reply should mention {text!r}")
    for text in expect.get("reply_not_contains", []):
        if text.lower() in reply.lower():
            problems.append(f"reply must not mention {text!r}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("filter", nargs="?", default="", help="only cases whose id contains this")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--verbose", "-v", action="store_true", help="show every call and reply")
    args = ap.parse_args()
    if not config.PROJECT_ID:
        sys.exit("Set GOOGLE_CLOUD_PROJECT in .env (the model runs in your project).")

    cases = [c for c in yaml.safe_load(CASES.read_text()) if args.filter in c["id"]]
    passed = total = 0
    started = time.time()
    for case in cases:
        for n in range(args.repeat):
            total += 1
            try:
                results = asyncio.run(run_case(case))
            except Exception as exc:  # noqa: BLE001
                results = [{"turn": 0, "say": "", "calls": [], "reply": "", "problems": [f"crashed: {exc!r}"]}]
            ok = not any(r["problems"] for r in results)
            passed += ok
            print(f"{'PASS' if ok else 'FAIL'}  {case['id']}" + (f"  (run {n + 1})" if args.repeat > 1 else ""))
            for r in results:
                if args.verbose or r["problems"]:
                    print(f"      turn {r['turn']}: {r['say'][:70]!r}")
                    print(f"        calls: {[c['name'] + json.dumps(c['args'])[:80] for c in r['calls']]}")
                    if args.verbose:
                        print(f"        reply: {r['reply'][:160]!r}")
                    for p in r["problems"]:
                        print(f"        -> {p}")
    print(f"\n{passed}/{total} passed in {time.time() - started:.0f}s")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
