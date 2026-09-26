"""Normalizes an incoming Gemini Enterprise message before ADK ever sees it.

Three things arrive in shapes the model should not get raw:

1. Photos come as inline base64 bytes wrapped in `<start_of_user_uploaded_file>`
   text sentinels. They are saved as ADK artifacts here, ahead of the runner:
   ADK appends the user message to the session before any callback runs, and
   the session store (10MB per event) would otherwise keep every photo forever
   and replay it every turn.
2. The sentinel text parts, and duplicated text parts, are dropped.
3. A2UI button clicks arrive as a DataPart `{"userAction": {...}}`. They become
   a short text line the model can act on, e.g.
   `[UI action] select_device {"asset_tag": "IT-01234"}`.
"""

import base64
import json
import logging
import re
import uuid

from a2a.types import DataPart, FilePart, FileWithBytes, Part, TextPart
from google.genai import types

logger = logging.getLogger(__name__)

_SENTINEL = re.compile(r"<(start|end)_of_user_uploaded_file:[^>]*>")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/heic": "heic", "image/heif": "heif"}

# Written into A2A call-context state by `preprocess`, read by the request converter.
PHOTOS_KEY = "hw_new_photos"
END_USER_KEY = "hw_end_user"


def new_photo_filename(mime_type: str) -> tuple[str, str]:
    photo_id = f"ph_{uuid.uuid4().hex[:10]}"
    return photo_id, f"{photo_id}.{_EXT.get(mime_type, 'bin')}"


async def save_photo_artifact(artifacts, *, app_name: str, user_id: str, session_id: str,
                              data: bytes, mime_type: str) -> dict:
    """Stores a photo with the ADK artifact service and returns its reference."""
    photo_id, filename = new_photo_filename(mime_type)
    part = types.Part(inline_data=types.Blob(data=data, mime_type=mime_type))
    version = await artifacts.save_artifact(app_name=app_name, user_id=user_id, session_id=session_id,
                                            filename=filename, artifact=part)
    info = await artifacts.get_artifact_version(app_name=app_name, user_id=user_id, session_id=session_id,
                                                filename=filename, version=version)
    return {"photo_id": photo_id, "filename": filename, "version": version,
            "uri": info.canonical_uri if info else "", "mime_type": mime_type, "bytes": len(data)}


def parse_user_action(data: dict) -> dict | None:
    """Accepts both `{"userAction": ...}` and an already-wrapped `{"data": {...}}`."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return inner.get("userAction") or inner.get("user_action")


def action_context(action: dict) -> dict:
    """Normalizes both context shapes renderers send: a dict, or [{key, value}] pairs."""
    ctx = action.get("context", {})
    if isinstance(ctx, dict):
        return {k: (v.get("literalString") if isinstance(v, dict) else v) for k, v in ctx.items()}
    out = {}
    for kv in ctx if isinstance(ctx, list) else []:
        if isinstance(kv, dict) and "key" in kv:
            v = kv.get("value")
            out[kv["key"]] = v.get("literalString") if isinstance(v, dict) else v
    return out


def action_text(action: dict) -> str:
    name = action.get("name") or action.get("actionName") or "unknown"
    return f"[UI action] {name} {json.dumps(action_context(action))}"


async def rewrite_parts(parts: list[Part], upload) -> tuple[list[Part], list[dict]]:
    """Returns the parts the model should see, and the photos that were staged.

    `await upload(bytes, mime_type) -> dict` is injected so this stays testable.
    """
    out: list[Part] = []
    photos: list[dict] = []
    seen_text: set[str] = set()
    for part in parts:
        root = part.root
        if isinstance(root, TextPart):
            text = _SENTINEL.sub("", root.text).strip()
            if text and text not in seen_text:
                seen_text.add(text)
                out.append(Part(root=TextPart(text=text)))
        elif isinstance(root, FilePart):
            f = root.file
            mime = (getattr(f, "mime_type", None) or "").lower()
            if isinstance(f, FileWithBytes) and mime.startswith("image/"):
                photo = await upload(base64.b64decode(f.bytes), mime)
                photos.append(photo)
                out.append(Part(root=TextPart(text=f"[Photo attached: {photo['photo_id']}]")))
            elif isinstance(f, FileWithBytes):
                out.append(Part(root=TextPart(text=f"[Unsupported attachment ignored: {mime or 'unknown type'}]")))
            else:
                out.append(part)
        elif isinstance(root, DataPart) and isinstance(root.data, dict):
            action = parse_user_action(root.data)
            if action:
                out.append(Part(root=TextPart(text=action_text(action))))
            else:
                out.append(Part(root=TextPart(text=f"[Data] {json.dumps(root.data)[:2000]}")))
        else:
            out.append(part)

    if not out:
        # A message with no parts is rejected by the model API.
        out.append(Part(root=TextPart(text="(empty message)")))
    return out, photos



# --- Display mode: A2UI cards (web app) or plain text (mobile app) -----------------
#
# The Gemini Enterprise mobile app requests A2UI but shows "Response contains
# unsupported content" for it, and its requests are identical to the web app's
# (measured 2026-09-26). So the first turn of each conversation asks, and the
# answer is kept in session state.

UI_MODE_KEY = "ui_mode"            # "cards" | "text" | "asking"
UI_PENDING_KEY = "ui_pending"      # the first message, replayed once they answer
UI_OPTIONS_KEY = "ui_options"      # numbered options of the last text card
ASK_MARKER = "[Ask display mode]"
SHOW_CURRENT = "[UI action] show_current_step {}"

_YES = {"1", "mobile", "mobile app", "app", "phone", "1 = mobile app", "1 mobile app", "yes", "y"}
_NO = {"2", "desktop", "browser", "desktop/browser", "web", "computer", "laptop", "2 = desktop/browser", "2 desktop", "no", "n"}
_TO_TEXT = {"text", "text mode", "switch to text", "use text", "numbers"}
_TO_CARDS = {"buttons", "cards", "button mode", "switch to buttons", "use buttons", "show buttons"}
_NUMBER = re.compile(r"^\s*(?:option\s*|#)?(\d{1,2})\s*[.)]?\s*$", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"[\s.!?]+$", "", " ".join(text.lower().split()))


def display_step(state: dict, text: str) -> tuple[str, dict]:
    """Decides the display mode for this turn and what the model should see.

    Returns (text for the model, state delta). `state` is the session state.
    """
    mode = state.get(UI_MODE_KEY)
    said = _norm(text)
    is_click = text.startswith("[UI action]")

    if mode in (None, "", "asking"):
        if is_click:  # only the web app can click
            return text, {UI_MODE_KEY: "cards"}
        if mode == "asking" and (said in _YES or said in _NO):
            chosen = "text" if said in _YES else "cards"
            pending = state.get(UI_PENDING_KEY) or ""
            return pending or "Hello", {UI_MODE_KEY: chosen, UI_PENDING_KEY: ""}
        if mode == "asking":
            # Not an answer: keep the first message, but a photo or a longer
            # message sent now also counts as what they want.
            pending = state.get(UI_PENDING_KEY) or ""
            return ASK_MARKER, {UI_PENDING_KEY: f"{pending}\n{text}".strip()}
        return ASK_MARKER, {UI_MODE_KEY: "asking", UI_PENDING_KEY: text}

    if said in _TO_TEXT and mode != "text":
        return SHOW_CURRENT, {UI_MODE_KEY: "text"}
    if said in _TO_CARDS and mode != "cards":
        return SHOW_CURRENT, {UI_MODE_KEY: "cards"}
    if mode == "text":
        options = state.get(UI_OPTIONS_KEY) or []
        match = _NUMBER.match(text)
        choice = None
        if match and 1 <= int(match.group(1)) <= len(options):
            choice = options[int(match.group(1)) - 1]
        else:
            choice = next((o for o in options if _norm(o["label"]) == said), None)
        if choice:
            return f"[UI action] {choice['action']} {json.dumps(choice['context'])}", {}
    return text, {}
