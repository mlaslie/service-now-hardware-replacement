"""P2 smoke run in a fresh process: the agent started with a given organization profile files and
follows up on tickets end to end, against the in-memory ServiceNow.

    ORGANIZATION_PROFILE=/path/to/profile.yaml uv run python fuzz/deterministic/profile_smoke.py

Prints one JSON object: {"ok": bool, "steps": [...], "error": "..."}. Used by test_profile_fuzz.py.
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fuzz.fakes import ANA, JANE, AuditedTableAPI, World, ctx_for  # noqa: E402  (guard first)
from fuzz.invariants import (InvariantViolation, check_queries, check_result_shape, check_visibility,  # noqa: E402
                             check_writes, shown_numbers)


async def instruction_check() -> None:
    """The agent's instruction (the profile's persona + the routing rules) as ADK renders it each turn."""
    from google.adk.utils.instructions_utils import inject_session_state

    from app import agent

    session = SimpleNamespace(state={"end_user": {}, "draft": {}, "ui_mode": "cards", "a2ui_version": "0.9",
                                     "last_photo_ids": []}, app_name="hardware_replacement", user_id="u", id="s")
    ctx = SimpleNamespace(_invocation_context=SimpleNamespace(session=session, artifact_service=None),
                          agent_name=agent.root_agent.name)
    await inject_session_state(agent.root_agent.instruction, ctx)


async def flow(steps: list) -> None:
    from app import cards, tools
    from app.vision import PhotoFindings
    from tests.test_cards import VALIDATOR

    api = AuditedTableAPI()
    with World(api) as world:
        ctx = ctx_for(JANE, "sess-smoke")

        async def call(name, **kw):
            api.acting_user = JANE
            start = len(api.log)
            ctx.state[tools.CARD_KEY] = None
            result = await getattr(tools, name)(tool_context=ctx, **kw)
            check_result_shape(name, result)
            check_queries(api.log, start)
            check_writes(api.log, start)
            card = ctx.state.get(tools.CARD_KEY)
            check_visibility(JANE, shown_numbers(result, card), api.tables["incident"],
                             {r["number"] for r in api.tables["incident"] if r.get("cmdb_ci", {}).get("value")
                              and str(r.get("state")) in ("1", "2", "3")})
            if card:
                errors = [e.message for e in VALIDATOR.iter_errors(card[1])]
                if errors:
                    raise InvariantViolation("P2", f"{name} staged an invalid card", errors=errors[:3])
                cards.to_text(card)
                cards.to_v08(card)
            steps.append({"tool": name, "status": result.get("status"), "step": result.get("step")})
            return result

        personal = cards.PROFILE.keys("personal")[0]
        equipment = cards.PROFILE.keys("equipment")[0]
        numbers = []
        for tag, key in (("123456", personal), ("CE-10421", equipment)):
            await call("start_request")
            await call("select_device", asset_tag=tag)
            r = await call("confirm_device", correct=True)
            if r.get("step") == "already_reported":
                await call("report_separately")
            r = await call("set_issue", category=key, description="Dead since this morning", urgency="normal")
            if r.get("step") == "photo":
                if r.get("photo") == "required":
                    world.send_photos(ctx.state, [PhotoFindings(image_kind="damage", damage_present=True,
                                                                damage_description="Cracked", damage_severity="severe",
                                                                issue_category=key, supports_replacement=True)])
                    await call("analyze_photos")
                else:
                    await call("skip_photo")
            r = await call("submit_ticket")
            if r.get("status") == "submitted":
                numbers.append(r["ticket"])
        for number in numbers:
            await call("update_ticket", number=number, urgency="high", note="any news?")
            await call("get_ticket", number=number, show="details")
        await call("list_my_tickets", include_closed=True)
        steps.append({"filed": numbers, "foreign_rows": sum(1 for r in api.tables["incident"]
                                                            if r.get("caller_id", {}).get("value") == ANA)})


def main() -> None:
    steps: list = []
    out = {"ok": True, "steps": steps, "error": "", "invariant": ""}
    try:
        from app import profile

        profile.current()
        asyncio.run(instruction_check())
        asyncio.run(flow(steps))
    except InvariantViolation as exc:
        out.update(ok=False, invariant=exc.inv_id, error=str(exc)[:3000])
    except BaseException as exc:  # noqa: BLE001
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}\n" + traceback.format_exc()[-3000:])
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
