"""Shared by every tool: the staged card, the request draft in session state, who is asking,
and turning ServiceNow failures into a sentence the model can say.
"""

import datetime as dt
import functools
import inspect
import logging
import uuid

from google.adk.tools import ToolContext

from app import cards, servicenow

logger = logging.getLogger(__name__)


CARD_KEY = "temp:card"


# Problem choices, photo rules, urgency floors, service texts: config/organization.yaml.
PROFILE = cards.PROFILE


# Matches the priority ServiceNow derives from servicenow.URGENCY_TO_IMPACT_URGENCY.
_PRIORITY = {"low": "4 - Low", "normal": "3 - Moderate", "high": "2 - High", "critical": "1 - Critical"}


def _jsonable(value):
    """Session state is stored as JSON."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _show(ctx: ToolContext, messages: list[dict]) -> None:
    ctx.state[CARD_KEY] = messages


def _new_draft() -> dict:
    return {"id": uuid.uuid4().hex[:8]}


def _draft(ctx: ToolContext) -> dict:
    draft = dict(ctx.state.get("draft") or {})
    if not draft.get("id"):
        # Saved at once: submit_ticket's duplicate check keys on it.
        draft["id"] = _new_draft()["id"]
        _save(ctx, draft)
    return draft


def _intake_draft(ctx: ToolContext) -> dict:
    """The draft to add device, problem or photo details to. Once a request is
    filed, new details (e.g. a photo sent after the confirmation) start a new
    one instead of editing the filed request."""
    draft = _draft(ctx)
    if draft.get("submitted_number"):
        draft = _new_draft()
        _save(ctx, draft)
    return draft


def _already_filed(draft: dict) -> dict | None:
    """For tools that edit the request before it is filed: once filed, changes go to the ticket."""
    number = draft.get("submitted_number")
    if not number:
        return None
    return {"status": "already_submitted", "ticket": number, "message": (
        f"This request was already filed as {number}, so the draft can't change. Make the change on the "
        f"ticket instead: update_ticket(number=\"{number}\", ...) for urgency, ship-to, status or a note.")}


def _save(ctx: ToolContext, draft: dict) -> None:
    ctx.state["draft"] = _jsonable(draft)


async def _employee(ctx: ToolContext) -> dict | None:
    """The signed-in person's ServiceNow profile, refreshed by the A2A layer every turn."""
    user = ctx.state.get("end_user") or {}
    if not user.get("verified"):
        return None
    return user.get("profile") or None


def _no_identity(ctx: ToolContext) -> dict:
    problem = (ctx.state.get("end_user") or {}).get("problem", "")
    if problem == "servicenow_hibernating":
        msg = "ServiceNow is waking up. Ask the user to try again in a minute."
    elif problem in ("no_token", "token_rejected"):
        msg = ("The user's ServiceNow sign-in is missing or expired. Tell them to reconnect ServiceNow "
               "for this agent in Gemini Enterprise, then try again.")
    else:
        msg = "ServiceNow could not be reached. Ask the user to try again shortly."
    return {"status": "error", "message": msg}


_TRUE = {"true", "yes", "y", "1", "on"}


def _coerce(fn, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """The model can send null, a number, a list or an object where a tool expects text or a yes/no
    (ADK passes arguments through unchecked). Each argument becomes the type the tool declares."""
    params = inspect.signature(fn).parameters
    bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    for name, value in list(bound.arguments.items()):
        kind = params[name].annotation
        if kind is str and not isinstance(value, str):
            if value is None:
                value = params[name].default if isinstance(params[name].default, str) else ""
            elif isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value if v is not None)
            elif isinstance(value, dict):
                value = " ".join(str(v) for v in value.values() if isinstance(v, (str, int, float)))
            else:
                value = str(value)
        elif kind is bool and not isinstance(value, bool):
            value = str(value).strip().lower() in _TRUE if isinstance(value, (str, int, float)) else False
        bound.arguments[name] = value
    return bound.args, bound.kwargs


def servicenow_errors(fn):
    """Turns ServiceNow failures into a result the model can explain in one sentence, and arguments
    of the wrong type into the declared type."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            args, kwargs = _coerce(fn, args, kwargs)
        except TypeError as exc:  # a missing or unknown argument
            return {"status": "error", "message": f"Wrong arguments for {fn.__name__}: {exc}"}
        try:
            return await fn(*args, **kwargs)
        except servicenow.NotSignedIn:
            return {"status": "error", "message": "The user's ServiceNow sign-in expired. Tell them to "
                    "reconnect ServiceNow for this agent in Gemini Enterprise, then try again."}
        except servicenow.Hibernating:
            return {"status": "error", "message": "ServiceNow is waking up. Ask the user to try again in a minute."}
        except servicenow.Unavailable as exc:
            logger.warning("ServiceNow unavailable in %s: %s", fn.__name__, exc)
            return {"status": "error", "message": "ServiceNow didn't respond, so this step may not have "
                    "completed. Ask the user to try again in a minute (a retry never files a second ticket)."}
        except servicenow.ServiceNowError as exc:
            logger.warning("ServiceNow call failed in %s: %s", fn.__name__, exc)
            return {"status": "error", "message": f"ServiceNow refused the request ({exc}). "
                    "Say it couldn't be completed and suggest contacting the service desk."}
    return wrapper
