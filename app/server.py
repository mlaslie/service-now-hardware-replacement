"""A2A server for Gemini Enterprise, run on Cloud Run with uvicorn.

    uvicorn app.server:app --port 8080

Request path, per chat turn:
  Cloud Run IAM checks x-serverless-authorization (the Discovery Engine agent)
  -> `preprocess` resolves the end user from their ServiceNow token (the
     pass-through bearer), stages photos as artifacts, turns A2UI clicks into text
  -> `to_run_request` carries identity and photos into session state
  -> ADK runs the agent with Agent Runtime managed services: Sessions (keyed by
     contextId) and Memory Bank (keyed by the verified email), plus the ADK
     artifact service for photos
"""

import logging
import os

from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2ui.a2a.extension import get_a2ui_agent_extension
from a2ui.schema.constants import VERSION_0_8
from google.adk.a2a.converters.request_converter import convert_a2a_request_to_agent_run_request
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig, ExecuteInterceptor
from google.adk.artifacts import GcsArtifactService
from google.adk.memory import VertexAiMemoryBankService
from google.adk.runners import Runner
from google.adk.sessions import VertexAiSessionService
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import cards, config, inbound, servicenow
from app.agent import root_agent
from app.identity import bearer, resolve_end_user

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("hardware_replacement")

# ADK's model client reads these; gemini-3.8-flash is served from `global`.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", config.MODEL_LOCATION)

# Cloud Run caps an HTTP/1 request body at 32MB. Gemini Enterprise inlines
# photos as base64, and the a2a-sdk default of 10MB rejects one phone photo
# with an error the client reports as a Content-Type mismatch.
MAX_BODY = 32 * 1024 * 1024

# Agent Runtime managed services, hosted on a code-less engine.
session_service = VertexAiSessionService(
    project=config.PROJECT_ID, location=config.AGENT_ENGINE_LOCATION, agent_engine_id=config.AGENT_ENGINE_ID)
memory_service = VertexAiMemoryBankService(
    project=config.PROJECT_ID, location=config.AGENT_ENGINE_LOCATION, agent_engine_id=config.AGENT_ENGINE_ID)
artifact_service = GcsArtifactService(bucket_name=config.ARTIFACT_BUCKET)


def adk_user_id(context_id: str) -> str:
    """The session partition key ADK derives when A2A auth is off. Kept on
    purpose: keying sessions to an identity that can fail to resolve on one turn
    would strand the conversation. Identity rides in state; memory uses email."""
    return f"A2A_USER_{context_id}"


async def preprocess(context: RequestContext) -> RequestContext:
    """Runs before the ADK runner, so nothing large reaches the session store."""
    state = context.call_context.state if context.call_context else {}
    headers = state.get("headers") or {}
    parts = list(context.message.parts) if context.message else []
    logger.info("turn start context=%s parts=%s", context.context_id,
                [getattr(p.root, "kind", "?") for p in parts])
    token = bearer(headers)
    # Request-scoped: tools call ServiceNow as this person. Never persisted.
    servicenow.user_token.set(token)
    user = await resolve_end_user(token)
    state[inbound.END_USER_KEY] = user.to_state()

    if context.message and context.message.parts:
        context_id = context.context_id or "no-context"

        async def upload(data: bytes, mime_type: str) -> dict:
            return await inbound.save_photo_artifact(
                artifact_service, app_name=config.APP_NAME, user_id=adk_user_id(context_id),
                session_id=context_id, data=data, mime_type=mime_type)

        parts, photos = await inbound.rewrite_parts(parts, upload)
        context.message.parts = parts
        state[inbound.PHOTOS_KEY] = photos
    logger.info("turn context=%s user=%s(%s) photos=%d", context.context_id, user.email or "-",
                user.source, len(state.get(inbound.PHOTOS_KEY) or []))
    return context


def to_run_request(context: RequestContext, part_converter):
    """ADK's converter, plus identity and staged photos as a session state delta.

    `user_id` stays ADK's default (`A2A_USER_<contextId>`) on purpose: it is the
    session partition key, and keying it to an identity that can fail to resolve
    on one turn (token expiry, a tokeninfo blip) would strand the conversation.
    Identity rides in state instead, refreshed every turn.
    """
    request = convert_a2a_request_to_agent_run_request(context, part_converter)
    state = context.call_context.state if context.call_context else {}
    delta: dict = {}
    end_user = state.get(inbound.END_USER_KEY)
    if end_user:
        # Refreshed every turn, so a sign-in that lapses is noticed immediately.
        delta["end_user"] = end_user
    photos = state.get(inbound.PHOTOS_KEY) or []
    for photo in photos:
        delta[f"photo:{photo['photo_id']}"] = photo
    if photos:
        delta["last_photo_ids"] = [p["photo_id"] for p in photos]
    request.state_delta = delta or None
    return request


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="Hardware Replacement",
        description=(
            "Replace broken or failing work hardware in a few taps. Snap a photo of the device "
            "and I'll identify it, read the asset tag and serial number, document the damage, "
            "and file the service desk request for you."
        ),
        # Must be the public URL: Gemini Enterprise POSTs to whatever the card says.
        url=f"{config.SERVICE_URL}/",
        version="1.0.0",
        protocol_version="0.3.0",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extensions=[get_a2ui_agent_extension(
                version=VERSION_0_8,
                accepts_inline_catalogs=True,
                supported_catalog_ids=[cards.BASIC_CATALOG_ID],
            )],
        ),
        # Without image types here Gemini Enterprise may never offer an attachment.
        default_input_modes=["text/plain", "image/png", "image/jpeg", "image/webp", "image/heic", "application/json"],
        default_output_modes=["text/plain", "application/json", "application/json+a2ui"],
        skills=[AgentSkill(
            id="hardware_replacement",
            name="Hardware replacement request",
            description="Guided replacement request for laptops, monitors, phones and accessories, with photo-based device identification and damage evidence.",
            tags=["it", "hardware", "replacement", "service desk", "broken"],
            examples=[
                "My laptop screen is cracked",
                "My laptop won't turn on",
                "I need to replace my monitor",
            ],
        )],
    )


def build_app():
    runner = Runner(agent=root_agent, app_name=config.APP_NAME, session_service=session_service,
                    artifact_service=artifact_service, memory_service=memory_service)
    executor = A2aAgentExecutor(
        runner=runner,
        config=A2aAgentExecutorConfig(
            request_converter=to_run_request,
            execute_interceptors=[ExecuteInterceptor(before_agent=preprocess)],
        ),
        use_legacy=True,
    )
    # Each Gemini Enterprise turn is a new task that completes within the turn,
    # so tasks need not outlive an instance. Conversation state is in Agent Runtime Sessions.
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(
        agent_card=build_agent_card(), http_handler=handler, max_content_length=MAX_BODY
    ).build()

    async def health(_request):
        return JSONResponse({"status": "ok"})

    app.router.routes.append(Route("/health", health))
    logger.info("agent card url=%s model=%s state engine=%s", config.SERVICE_URL, config.MODEL, config.AGENT_ENGINE_ID)
    return app


app = build_app()
