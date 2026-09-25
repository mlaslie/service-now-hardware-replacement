"""Memory Bank access, scoped to the verified person rather than the conversation.

ADK's `tool_context.search_memory` scopes by `session.user_id`, which here is
`A2A_USER_<contextId>` (the session partition key). That would give every
conversation its own empty memory. Memories are keyed by the verified email
instead, so what someone said last week ("ship to my home office in Denver")
is there the next time they ask.
"""

import asyncio
import logging

from google.adk.tools import ToolContext

from app import config

logger = logging.getLogger(__name__)

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
    for memory in result.memories[:limit]:
        text = " ".join(p.text for p in (memory.content.parts or []) if p.text).strip()
        if text:
            facts.append(text)
    return facts


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
