"""Model layer: runs generated conversations through the real agent (real Gemini model, like
evals/run.py) against the in-memory ServiceNow, and scores them by the M1-M5 invariants.

    uv run python fuzz/model/run.py --seed 1234 --workers 6 --budget-minutes 25
    uv run python fuzz/model/run.py --count 3 --workers 2            # a smoke run
    uv run python fuzz/model/run.py --filter inj- --repeat 3           # flaky check on a subset

One conversation per worker process (the fakes patch module globals), 120 s and 20 model calls per
conversation, a global time budget after which no new conversation starts, and token usage summed for
a cost estimate. Writes fuzz/results/model.json and appends model violations to
fuzz/results/violations.jsonl. Needs Google Cloud credentials for the model (gcloud auth
application-default login) and GOOGLE_CLOUD_PROJECT (environment or .env). Never calls ServiceNow:
the network guard in fuzz/fakes.py refuses every host but Google's.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import datetime
import json
import multiprocessing
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "fuzz" / "results"
CORPUS = ROOT / "fuzz" / "corpus"

# USD per million tokens; an estimate only (check current Gemini pricing for the configured model).
PRICE_IN, PRICE_OUT = 0.30, 2.50


def _init_worker() -> None:
    """Each worker: network guard and fake ServiceNow URL before anything imports the app."""
    import warnings

    warnings.filterwarnings("ignore")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
    import logging

    logging.disable(logging.WARNING)
    from fuzz import fakes  # noqa: F401
    from app import config

    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.PROJECT_ID)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", config.MODEL_LOCATION)


def _jsonable(value, limit=2000):
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(repr(value))
    if len(text) > limit:
        return {"truncated": text[:limit]}
    return json.loads(text)


async def _converse(case: dict, max_calls: int, out: dict) -> None:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from app import agent as agent_mod, config, inbound
    from evals.run import _card_text, check_turn
    from fuzz.model.world import ModelWorld, initial_state

    world = ModelWorld(case).install()
    out["_world"] = world
    try:
        runner = InMemoryRunner(agent=agent_mod.root_agent, app_name=config.APP_NAME)
        session = await runner.session_service.create_session(app_name=config.APP_NAME, user_id="fuzz",
                                                              state=initial_state(case))
        model_calls = 0
        for i, turn in enumerate(case["turns"]):
            said = turn["say"]
            current = await runner.session_service.get_session(app_name=config.APP_NAME, user_id="fuzz",
                                                               session_id=session.id)
            state = dict(current.state)
            text, delta = inbound.display_step(state, said) if state.get("ui_mode") == "text" else (said, {})
            if "[Photo attached:" in said:
                pid = said.split("[Photo attached:")[1].split("]")[0].strip()
                delta |= world.stage_photo(pid)
            record = {"turn": i + 1, "say": said, "sent": text if text != said else None, "calls": [], "reply": "",
                      "violations": [], "error": ""}
            out["turns"].append(record)
            pending: dict[str, dict] = {}
            try:
                async for event in runner.run_async(user_id="fuzz", session_id=session.id, state_delta=delta or None,
                                                    new_message=types.Content(role="user", parts=[types.Part(text=text)])):
                    usage = getattr(event, "usage_metadata", None)
                    if usage and not event.partial:
                        out["tokens"]["prompt"] += usage.prompt_token_count or 0
                        out["tokens"]["output"] += (usage.candidates_token_count or 0) + (
                            getattr(usage, "thoughts_token_count", 0) or 0)
                    if event.author == agent_mod.root_agent.name and event.content and not event.partial:
                        model_calls += bool(event.get_function_calls()) or bool(
                            any(p.text for p in event.content.parts or []))
                    for fc in event.get_function_calls() or []:
                        call = {"name": fc.name, "args": _jsonable(dict(fc.args or {}))}
                        record["calls"].append(call)
                        pending[fc.id or fc.name] = call
                    for fr in event.get_function_responses() or []:
                        call = pending.pop(fr.id or fr.name, None) or {"name": fr.name, "args": {}}
                        response = dict(fr.response or {})
                        call["response"] = _jsonable(response, 3000)
                        if call not in record["calls"]:
                            record["calls"].append(call)
                        draft = (await runner.session_service.get_session(
                            app_name=config.APP_NAME, user_id="fuzz", session_id=session.id)).state.get("draft") or {}
                        record["violations"] += world.check_response(fr.name, call["args"], response,
                                                                     (draft.get("device") or {}).get("ci", ""))
                    if event.content and event.content.role == "model" and not event.get_function_calls():
                        got = _card_text(event.content)
                        if got:
                            record["reply"] = got
                    if model_calls > max_calls:
                        record["error"] = f"stopped: more than {max_calls} model calls"
                        out["budget_stopped"] = True
                        break
            except Exception as exc:  # noqa: BLE001
                record["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                out["traceback"] = traceback.format_exc()[-3000:]
            record["problems"] = check_turn(turn.get("expect") or {}, record["calls"], record["reply"])
            out["soft"]["total"] += len([k for k in (turn.get("expect") or {}) if k])
            out["soft"]["unmet"] += len(record["problems"])
            if out.get("budget_stopped"):
                break
    finally:
        world.uninstall()


def run_one(case: dict, timeout: float, max_calls: int) -> dict:
    """One conversation, in a worker process."""
    started = time.time()
    out = {"id": case["id"], "seed_id": case.get("seed_id", case["id"]), "category": case.get("category", ""),
           "mutations": case.get("mutations", []), "user": case.get("user", "jane"), "ui_mode": case.get("ui_mode", "cards"),
           "turns": [], "tokens": {"prompt": 0, "output": 0}, "soft": {"total": 0, "unmet": 0}, "error": ""}
    try:
        asyncio.run(asyncio.wait_for(_converse(case, max_calls, out), timeout))
    except asyncio.TimeoutError:
        out["error"] = f"timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        out["traceback"] = traceback.format_exc()[-3000:]
    world = out.pop("_world", None)
    violations = [v | {"turn": t["turn"]} for t in out["turns"] for v in t["violations"]]
    if world is not None:
        violations += world.check_conversation(out["turns"])
        out["audit"] = [{k: e.get(k) for k in ("i", "method", "table", "sys_id", "query", "json", "user", "error",
                                               "query_problem")} for e in world.api.log][-80:]
        out["audit"] = _jsonable(out["audit"], 20000)
    if out["error"] and not out["error"].startswith("timed out"):
        violations.append({"id": "M5", "detail": f"conversation crashed: {out['error'][:300]}", "data": {}})
    out["violations"] = _jsonable(violations, 20000)
    out["passed"] = not violations and not out["error"]
    out["duration_s"] = round(time.time() - started, 1)
    return out


def load_cases(args) -> list[dict]:
    import yaml

    cases = []
    if args.corpus in ("seeds", "both"):
        cases += yaml.safe_load((CORPUS / "seeds.yaml").read_text(encoding="utf-8"))
    if args.corpus in ("expanded", "both"):
        path = CORPUS / "expanded.yaml"
        if not path.exists() or args.regenerate:
            from fuzz.model.generate import generate
            import yaml as _y

            path.write_text(_y.safe_dump(generate(args.seed, args.generate), allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        cases += yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [c for c in cases if args.filter in c["id"] or args.filter in c.get("category", "")]
    if args.count:
        cases = cases[: args.count]
    return [dict(c, _repeat=r) for c in cases for r in range(args.repeat)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, default=1234, help="generator seed (with --regenerate)")
    ap.add_argument("--corpus", choices=["seeds", "expanded", "both"], default="both")
    ap.add_argument("--regenerate", action="store_true", help="rewrite corpus/expanded.yaml from --seed first")
    ap.add_argument("--generate", type=int, default=60, help="conversations to generate with --regenerate")
    ap.add_argument("--filter", default="", help="only conversations whose id or category contains this")
    ap.add_argument("--count", type=int, default=0, help="run only the first N conversations")
    ap.add_argument("--repeat", type=int, default=1, help="run each conversation N times (flaky check)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=120, help="seconds per conversation")
    ap.add_argument("--max-calls", type=int, default=20, help="model calls per conversation")
    ap.add_argument("--budget-minutes", type=float, default=25, help="no new conversation starts after this")
    ap.add_argument("--price-in", type=float, default=PRICE_IN, help="USD per 1M input tokens (estimate)")
    ap.add_argument("--price-out", type=float, default=PRICE_OUT, help="USD per 1M output tokens (estimate)")
    args = ap.parse_args()

    _init_worker()
    from app import config
    from fuzz import invariants

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        sys.exit("Set GOOGLE_CLOUD_PROJECT (environment or .env): the model runs in your project.")
    cases = load_cases(args)
    started, deadline = time.time(), time.time() + args.budget_minutes * 60
    results, skipped = [], []
    queue = list(cases)
    print(f"{len(cases)} conversations, {args.workers} workers, model {config.MODEL}, budget {args.budget_minutes} min")
    ctx = multiprocessing.get_context("spawn")
    with cf.ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx, initializer=_init_worker) as pool:
        running: dict[cf.Future, dict] = {}
        while queue or running:
            while queue and len(running) < args.workers and time.time() < deadline:
                case = queue.pop(0)
                running[pool.submit(run_one, case, args.timeout, args.max_calls)] = case
            if not running:
                break
            done, _ = cf.wait(running, timeout=5, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                case = running.pop(fut)
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001  (a worker died)
                    res = {"id": case["id"], "category": case.get("category", ""), "passed": False, "turns": [],
                           "violations": [{"id": "M5", "detail": f"worker failed: {exc!r}"[:300], "data": {}}],
                           "error": repr(exc)[:300], "tokens": {"prompt": 0, "output": 0}, "soft": {"total": 0, "unmet": 0}}
                res["repeat"] = case.get("_repeat", 0)
                results.append(res)
                mark = "PASS" if res["passed"] else "FAIL"
                print(f"{mark}  {res['id']}  ({res.get('duration_s', 0)}s)"
                      + "".join(f"\n      [{v['id']}] {v['detail'][:150]}" for v in res.get("violations", [])))
            if time.time() >= deadline and queue:
                skipped += [c["id"] for c in queue]
                queue = []
    write_results(args, results, skipped, started, config.MODEL)


def write_results(args, results, skipped, started, model) -> None:
    from fuzz import invariants

    prompt = sum(r["tokens"]["prompt"] for r in results)
    output = sum(r["tokens"]["output"] for r in results)
    cost = prompt / 1e6 * args.price_in + output / 1e6 * args.price_out
    soft_total = sum(r["soft"]["total"] for r in results)
    soft_met = soft_total - sum(r["soft"]["unmet"] for r in results)
    by_case: dict[str, set] = {}
    for r in results:
        by_case.setdefault(r["id"], set()).add(r["passed"])
    flaky = sorted(k for k, v in by_case.items() if len(v) > 1)
    out = {
        "kind": "model", "started": datetime.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "duration_s": round(time.time() - started, 1), "model": model, "seed": args.seed, "corpus": args.corpus,
        "workers": args.workers, "budget_minutes": args.budget_minutes, "repeat": args.repeat,
        "tokens": {"prompt": prompt, "output": output},
        "cost_estimate_usd": round(cost, 4), "prices_per_million": {"input": args.price_in, "output": args.price_out},
        "summary": {"conversations": len(results), "passed": sum(r["passed"] for r in results),
                    "pass_rate": round(sum(r["passed"] for r in results) / len(results), 3) if results else None,
                    "soft_score": round(soft_met / soft_total, 3) if soft_total else None,
                    "flaky": flaky, "skipped_for_budget": skipped},
        "conversations": sorted(results, key=lambda r: (r.get("category", ""), r["id"], r.get("repeat", 0))),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "model.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    # Model violations replace the previous model run's in violations.jsonl.
    kept = [v for v in invariants.read_violations() if v.get("source") != "model"]
    invariants.VIOLATIONS.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in kept), encoding="utf-8")
    for r in results:
        for v in r.get("violations", []):
            invariants.record(v["id"], v["detail"], source="model", nodeid=r["id"],
                              rerun=f'uv run python fuzz/model/run.py --filter "{r["id"]}" --workers 1',
                              reproducer=json.dumps([{"say": t["say"], "calls": [c["name"] for c in t["calls"]]}
                                                     for t in r.get("turns", [])], ensure_ascii=False)[:3000],
                              data=v.get("data"))
    s = out["summary"]
    print(f"\n{s['passed']}/{s['conversations']} passed, soft score {s['soft_score']}, flaky {len(flaky)}, "
          f"skipped {len(skipped)}; tokens {prompt} in / {output} out, about ${cost:.2f}; "
          f"{out['duration_s']:.0f}s -> {RESULTS / 'model.json'}")


if __name__ == "__main__":
    main()
