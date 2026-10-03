"""Memory Bank access, scoped to the verified person rather than the conversation.

ADK's `tool_context.search_memory` scopes by `session.user_id`, which here is
`A2A_USER_<contextId>` (the session partition key). That would give every
conversation its own empty memory. Memories are keyed by the verified email
instead, so what someone said last week ("ship to my home office in Denver")
is there the next time they ask.
"""

import asyncio
import functools
import json
import logging
import re
import unicodedata
import warnings

from google.adk.tools import ToolContext

from app import config

logger = logging.getLogger(__name__)

# Saved delivery addresses live in their own Memory Bank scope, stored verbatim (one
# fact per address), so they are never paraphrased ("a Marriott in Chicago") the way
# extracted conversation memories are. Only permanent places are saved.
_ADDRESS_SCOPE_APP = f"{config.APP_NAME}_addresses"
_ADDRESS_PREFIX = "Saved delivery address: "
# Extracted memories about deliveries are vague and may be temporary: never shown to the model.
_DELIVERY_WORDS = re.compile(r"\b(ship|shipped|shipping|deliver|delivered|delivery|address|hotel|sent to|send to)\b", re.I)

_RECALL_QUERY = "delivery location, contact preferences, and past hardware problems"


def _service(ctx: ToolContext):
    return ctx._invocation_context.memory_service  # noqa: SLF001 - no public accessor


async def recall(ctx: ToolContext, email: str, limit: int = 8) -> list[str]:
    """Facts remembered about this person. Never fails the turn."""
    service = _service(ctx)
    if not service or not email:
        return []
    try:
        result = await asyncio.wait_for(
            service.search_memory(app_name=config.APP_NAME, user_id=email, query=_RECALL_QUERY), timeout=8)
    except Exception as exc:  # noqa: BLE001 - memory is a convenience, not a dependency
        logger.warning("memory recall failed: %s", type(exc).__name__)
        return []
    facts = []
    for memory in result.memories:
        text = " ".join(p.text for p in (memory.content.parts or []) if p.text).strip()
        if text and not _DELIVERY_WORDS.search(text):  # addresses come from saved_addresses()
            facts.append(text)
        if len(facts) >= limit:  # filter first: delivery memories rank high for this query
            break
    return facts


@functools.lru_cache(maxsize=1)
def _client():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import vertexai
        return vertexai.Client(project=config.PROJECT_ID, location=config.AGENT_ENGINE_LOCATION)


def _engine() -> str:
    return (f"projects/{config.PROJECT_ID}/locations/{config.AGENT_ENGINE_LOCATION}"
            f"/reasoningEngines/{config.AGENT_ENGINE_ID}")


def normalize_address(address: str) -> str:
    """For comparing addresses: case, punctuation and spacing ignored; letters of any script kept
    ("東京都港区1-2-3" and "大阪府北区1-2-3" stay different, "Élysées" stays "élysées")."""
    return " ".join(re.sub(r"[\W_]+", " ", unicodedata.normalize("NFC", address or "").casefold()).split())


def _list_addresses(email: str) -> list[dict]:
    out = []
    for item in _client().agent_engines.memories.retrieve(
            name=_engine(), scope={"app_name": _ADDRESS_SCOPE_APP, "user_id": email},
            simple_retrieval_params={"page_size": 50}):
        fact = (item.memory.fact or "") if item.memory else ""
        if fact.startswith(_ADDRESS_PREFIX):
            try:
                out.append(json.loads(fact[len(_ADDRESS_PREFIX):]) | {"memory": item.memory.name})
            except ValueError:
                continue
    return out


async def saved_addresses(email: str) -> list[dict]:
    """The person's saved permanent delivery addresses: [{"label", "address"}]. Never fails."""
    if not email:
        return []
    try:
        found = await asyncio.wait_for(asyncio.to_thread(_list_addresses, email), timeout=8)
    except Exception as exc:  # noqa: BLE001 - a convenience, not a dependency
        logger.warning("saved addresses not loaded: %s", type(exc).__name__)
        return []
    unique: dict[str, dict] = {}
    for a in found:
        unique.setdefault(normalize_address(a.get("address", "")), {"label": a.get("label") or "Saved address",
                                                                    "address": a.get("address", "")})
    return [a for a in unique.values() if a["address"]]


def _save_address(email: str, label: str, address: str) -> None:
    existing = _list_addresses(email)
    key = normalize_address(address)
    if any(normalize_address(a.get("address", "")) == key for a in existing):
        return
    for a in existing:  # a new address for the same label (they moved) replaces the old one
        if label and (a.get("label") or "").lower() == label.lower():
            _client().agent_engines.memories.delete(name=a["memory"])
    _client().agent_engines.memories.create(
        name=_engine(), fact=_ADDRESS_PREFIX + json.dumps({"label": label, "address": address}),
        scope={"app_name": _ADDRESS_SCOPE_APP, "user_id": email}, config={"wait_for_completion": False})


async def save_address(email: str, label: str, address: str) -> None:
    """Remembers a permanent delivery address, verbatim. Never fails the turn."""
    if not email or not address:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(_save_address, email, label, address), timeout=10)
        logger.info("saved a delivery address (%s)", label or "no label")
    except Exception as exc:  # noqa: BLE001
        logger.warning("delivery address not saved: %s", type(exc).__name__)


async def remember_conversation(ctx: ToolContext, email: str) -> None:
    """Sends this conversation to Memory Bank.

    Awaited rather than backgrounded: Cloud Run throttles CPU once the response
    is sent, so a background task could be cut off. The call only submits the
    events; Memory Bank extracts memories server-side, and only for the
    configured topics (preferences, delivery and contact, hardware history).
    """
    service = _service(ctx)
    session = ctx.session
    if not service or not email or not session:
        return
    try:
        await asyncio.wait_for(service.add_events_to_memory(
            app_name=config.APP_NAME, user_id=email, events=list(session.events), session_id=session.id),
            timeout=10)
        logger.info("conversation %s sent to memory", session.id)
    except Exception as exc:  # noqa: BLE001 - never fail a filed ticket over memory
        logger.warning("memory generation failed: %s", type(exc).__name__)
