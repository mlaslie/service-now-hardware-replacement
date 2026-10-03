"""The A2A front door: identity every turn, the token never stored, photos staged, display mode."""

import base64
import json
from types import SimpleNamespace

import pytest
from a2a.types import FilePart, FileWithBytes, Message, Part, Role, TextPart

from app import identity, inbound, server, servicenow

TOKEN = "sn-access-token-SECRET"


def _context(parts, headers=None, context_id="ctx-1"):
    message = Message(message_id="m1", role=Role.user, parts=parts)
    return SimpleNamespace(
        call_context=SimpleNamespace(state={"headers": headers if headers is not None
                                            else {"authorization": f"Bearer {TOKEN}"}}),
        message=message, context_id=context_id, requested_extensions=set(), metadata={},
        configuration=None)


@pytest.fixture
def world(monkeypatch):
    seen = {"tokens": [], "sessions": {}}

    async def resolve(token):
        seen["tokens"].append(token)
        if not token:
            return identity.EndUser(email="", problem="no_token")
        return identity.EndUser(email="jane.doe@example.com", name="Jane Doe", sys_id="u_jane", source="servicenow")

    async def get_session(app_name, user_id, session_id, config=None):
        found = seen["sessions"].get(session_id)
        if isinstance(found, Exception):
            raise found
        return SimpleNamespace(state=found) if found is not None else None

    async def save_photo(service, *, app_name, user_id, session_id, data, mime_type):
        return {"photo_id": "ph_1", "uri": "gs://b/ph_1.jpg", "mime_type": mime_type}

    monkeypatch.setattr(server, "resolve_end_user", resolve)
    monkeypatch.setattr(server.session_service, "get_session", get_session)
    monkeypatch.setattr(inbound, "save_photo_artifact", save_photo)
    return seen


def _text(t):
    return Part(root=TextPart(text=t))


async def test_identity_is_refreshed_every_turn_and_the_token_is_never_stored(world):
    for turn in range(2):
        ctx = await server.preprocess(_context([_text("hello")]))
        state = ctx.call_context.state
        assert state[inbound.END_USER_KEY]["email"] == "jane.doe@example.com"
        assert servicenow.user_token.get() == TOKEN  # request-scoped, for the tools
    assert world["tokens"] == [TOKEN, TOKEN]
    stored = {k: v for k, v in state.items() if k != "headers"}  # headers are the request's, not stored
    assert TOKEN not in json.dumps(stored, default=str)


async def test_the_session_delta_never_carries_the_token(world, monkeypatch):
    ctx = await server.preprocess(_context([_text("hello")]))
    monkeypatch.setattr(server, "convert_a2a_request_to_agent_run_request",
                        lambda context, converter: SimpleNamespace(state_delta=None))
    request = server.to_run_request(ctx, None)
    assert TOKEN not in json.dumps(request.state_delta, default=str)
    assert request.state_delta["end_user"]["sys_id"] == "u_jane"


async def test_a_google_identity_token_is_not_used_as_the_users(world):
    google = ".".join(base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
                      for p in ({"alg": "RS256"}, {"iss": "https://accounts.google.com"})) + ".sig"
    await server.preprocess(_context([_text("hi")], headers={"authorization": f"Bearer {google}"}))
    assert world["tokens"] == [None]


async def test_first_turn_asks_desktop_or_mobile_and_keeps_the_message(world):
    ctx = await server.preprocess(_context([_text("my screen is cracked")]))
    assert ctx.message.parts[0].root.text == inbound.ASK_MARKER
    delta = ctx.call_context.state[server.UI_DELTA_KEY]
    assert delta[inbound.UI_MODE_KEY] == "asking" and delta[inbound.UI_PENDING_KEY] == "my screen is cracked"


async def test_a_session_blip_keeps_the_conversation_going(world):
    world["sessions"]["ctx-1"] = RuntimeError("Vertex 503")
    ctx = await server.preprocess(_context([_text("2")]))
    assert ctx.message.parts[0].root.text == "2"  # passed through, not "which app are you on?"
    assert ctx.call_context.state[server.UI_DELTA_KEY] == {}


async def test_a_numbered_reply_in_text_mode_becomes_the_click(world):
    world["sessions"]["ctx-1"] = {inbound.UI_MODE_KEY: "text", inbound.UI_OPTIONS_KEY: [
        {"label": "Yes", "action": "confirm_device", "context": {"correct": True}}]}
    ctx = await server.preprocess(_context([_text("1")]))
    assert ctx.message.parts[0].root.text == '[UI action] confirm_device {"correct": true}'


async def test_photos_are_staged_not_sent_to_the_model(world):
    photo = Part(root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"jpeg").decode(), mime_type="image/jpeg")))
    world["sessions"]["ctx-1"] = {inbound.UI_MODE_KEY: "cards"}
    ctx = await server.preprocess(_context([_text("cracked"), photo]))
    texts = [p.root.text for p in ctx.message.parts]
    assert texts == ["cracked\n[Photo attached: ph_1]"]  # one text part; no image bytes for the model
    assert ctx.call_context.state[inbound.PHOTOS_KEY][0]["uri"] == "gs://b/ph_1.jpg"
