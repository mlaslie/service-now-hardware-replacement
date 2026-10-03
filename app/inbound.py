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
import hashlib
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


# Gemini Enterprise sends this text part alongside every A2UI v0.9 click.
CLICK_ECHO = "User action triggered."


def parse_user_action(data: dict) -> dict | None:
    """A button click: `{"action": ...}` (A2UI v0.9) or `{"userAction": ...}` (v0.8),
    possibly wrapped in `{"data": {...}}`."""
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    action = inner.get("action") or inner.get("userAction") or inner.get("user_action")
    return action if isinstance(action, dict) and (action.get("name") or action.get("actionName")) else None


def action_context(action: dict) -> dict:
    """Normalizes both context shapes renderers send: a dict, or [{key, value}] pairs."""
    ctx = action.get("context", {})
    if isinstance(ctx, dict):
        return {k: (v.get("literalString") if isinstance(v, dict) else v) for k, v in ctx.items()}
    out = {}
    for kv in ctx if isinstance(ctx, list) else []:
        if isinstance(kv, dict) and isinstance(kv.get("key"), (str, int, float, bool)):
            v = kv.get("value")
            out[str(kv["key"])] = v.get("literalString") if isinstance(v, dict) else v
    return out


def action_text(action: dict) -> str:
    name = action.get("name") or action.get("actionName") or "unknown"
    return f"[UI action] {name} {json.dumps(action_context(action))}"


PHOTO_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}
MAX_PHOTO_BYTES = 15 * 1024 * 1024


async def _stage_photo(encoded: str, mime: str, upload, photos: list[dict]) -> str:
    """Saves one photo and returns the text the model sees. A photo that can't be read or saved
    never fails the turn: the user's words still go through, with a note about the photo."""
    try:
        data = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError):
        return "[A photo was attached but could not be read. Ask the user to attach it again.]"
    if not data:
        return "[A photo was attached but was empty. Ask the user to attach it again.]"
    if len(data) > MAX_PHOTO_BYTES:
        return f"[A photo was too large ({len(data) // (1024 * 1024)} MB; the limit is 15 MB). Ask for a smaller one.]"
    try:
        photo = await upload(data, mime)
    except Exception as exc:  # noqa: BLE001  (storage outage, permissions)
        logger.warning("photo could not be saved: %s", type(exc).__name__)
        return "[A photo was attached but could not be saved. Ask the user to attach it again in a minute.]"
    photo["sha256"] = hashlib.sha256(data).hexdigest()  # the same photo sent twice is read once (D12)
    photos.append(photo)
    return f"[Photo attached: {photo['photo_id']}]"


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
            if isinstance(f, FileWithBytes) and mime in PHOTO_TYPES:
                out.append(Part(root=TextPart(text=await _stage_photo(f.bytes, mime, upload, photos))))
            elif isinstance(f, FileWithBytes):
                out.append(Part(root=TextPart(text=f"[Unsupported attachment ignored: {mime or 'unknown type'}. "
                                                   "Photos can be JPEG, PNG, WebP or HEIC.]")))
            else:
                # A link instead of the file's bytes: the model can't open it (and errors on some schemes).
                out.append(Part(root=TextPart(text="[An attachment arrived as a link, which can't be opened. "
                                                   "Ask the user to attach the photo again.]")))
        elif isinstance(root, DataPart) and isinstance(root.data, dict):
            action = parse_user_action(root.data)
            if action:
                out.append(Part(root=TextPart(text=action_text(action))))
            else:
                out.append(Part(root=TextPart(text=f"[Data] {json.dumps(root.data)[:2000]}")))
        else:
            out.append(part)

    if any(t.root.text.startswith("[UI action]") for t in out if isinstance(t.root, TextPart)):
        out = [t for t in out if not (isinstance(t.root, TextPart) and t.root.text == CLICK_ECHO)]
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

# The A2UI version the client requested this turn ("0.9", "0.8" or ""), from its A2A extensions.
A2UI_VERSION_KEY = "a2ui_version"
UI_MODE_KEY = "ui_mode"            # "cards" | "text" | "asking"
UI_PENDING_KEY = "ui_pending"      # the first message, replayed once they answer
UI_OPTIONS_KEY = "ui_options"      # numbered options of the last text card
ASK_MARKER = "[Ask display mode]"
SHOW_CURRENT = "[UI action] show_current_step {}"

UI_ASKS_KEY = "ui_asks"            # how many times the question was asked without an answer

_MOBILE_WORDS = {"mobile", "phone", "iphone", "android", "ipad", "tablet", "cell", "cellphone", "smartphone"}
_DESKTOP_WORDS = {"desktop", "browser", "web", "computer", "laptop", "pc", "mac", "chrome", "edge", "safari", "firefox"}
_FILLER = {"i", "i'm", "im", "am", "on", "the", "my", "a", "an", "using", "use", "it's", "its", "from", "via", "in",
           "app", "version", "option", "gemini", "enterprise", "is", "it", "i'll", "ill", "be", "and"}
_ANSWER_NUMBER = re.compile(r"^\s*(?:option\s*|#|\()?\s*([12])\s*(?:[.):=\-]|\b)")
_TO_TEXT = {"text", "text mode", "switch to text", "use text", "numbers"}
_TO_CARDS = {"buttons", "cards", "button mode", "switch to buttons", "use buttons", "show buttons"}
_NUMBER = re.compile(r"^\s*(?:option\s*|#)?(\d{1,2})\s*[.)]?\s*$", re.IGNORECASE)
_MAX_ASKS = 3  # after this many unanswered asks, use text (works in both apps; "buttons" switches)


def _norm(text: str) -> str:
    return re.sub(r"[\s.!?]+$", "", " ".join(text.lower().split()))


def _words(said: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", said)


def display_answer(text: str) -> tuple[str | None, bool]:
    """("text" | "cards" | None, whether the reply said anything besides the answer).

    Accepts "1", "1)", "1 - mobile", "option 2", "I'm on desktop", "on my phone"... but not a
    message that mentions both, or a longer message about something else."""
    said = _norm(text)
    words = _words(said)
    if not words or len(words) > 8:
        return None, False
    number = _ANSWER_NUMBER.match(said)
    mobile = bool(_MOBILE_WORDS & set(words))
    desktop = bool(_DESKTOP_WORDS & set(words))
    if number:
        chosen = "text" if number.group(1) == "1" else "cards"
        if (chosen == "text" and desktop) or (chosen == "cards" and mobile):
            return None, False  # "1 desktop": contradictory
    elif mobile != desktop:
        chosen = "text" if mobile else "cards"
    else:
        return None, False
    extra = [w for w in words if w not in _FILLER | _MOBILE_WORDS | _DESKTOP_WORDS and not w.isdigit()]
    return chosen, bool(extra)


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
        pending = state.get(UI_PENDING_KEY) or ""
        if mode == "asking":
            chosen, extra = display_answer(text)
            if chosen:
                # "I'm on my phone, the screen is cracked": the rest is part of the request too.
                replay = f"{pending}\n{text}".strip() if extra else pending
                return replay or "Hello", {UI_MODE_KEY: chosen, UI_PENDING_KEY: "", UI_ASKS_KEY: 0}
            asks = int(state.get(UI_ASKS_KEY) or 1) + 1
            if asks >= _MAX_ASKS:
                # Still no answer: text works in both apps (and "buttons" switches on desktop).
                return f"{pending}\n{text}".strip(), {UI_MODE_KEY: "text", UI_PENDING_KEY: "", UI_ASKS_KEY: 0}
            # Not an answer: keep the first message, but a photo or a longer
            # message sent now also counts as what they want.
            return ASK_MARKER, {UI_PENDING_KEY: f"{pending}\n{text}".strip(), UI_ASKS_KEY: asks}
        return ASK_MARKER, {UI_MODE_KEY: "asking", UI_PENDING_KEY: text, UI_ASKS_KEY: 1}

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


def requested_a2ui_version(extensions) -> str:
    """The highest A2UI version among the A2A extensions the client requested."""
    versions = [e.rsplit("/v", 1)[-1] for e in (extensions or []) if "a2ui.org/a2a-extension/a2ui/v" in e]
    return max(versions, key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()), default="")
