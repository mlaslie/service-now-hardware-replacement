"""Delivery addresses. A ticket's ship-to is always a real street address: the one in ServiceNow,
a saved permanent address (verbatim, from memory.saved_addresses), or one the user typed.
Temporary places (hotels, events) are used once and never saved or suggested.
"""

import re

from app import memory
from app.tools._common import PROFILE

_TEMPORARY = re.compile(r"\b(hotel|motel|inn|suites|resort|lodge|marriott|hilton|hyatt|sheraton|westin|airbnb|"
                        r"conference|convention|event|airport|temporary|temp)\b", re.I)


_ADDRESS_ON_FILE = re.compile(r"\b(usual|default|on file|servicenow|original|regular|normal)\b", re.I)


_PLACE_ALIASES = {"home": ("home", "house", "my place", "apartment", "residence"), "office": ("office", "work")}


def _looks_like_street_address(text: str) -> bool:
    # Most scripts separate words; Chinese and Japanese addresses don't ("東京都港区芝公園1-2-3").
    return bool(re.search(r"\d", text)) and (len(text.split()) >= 3 or (len(text) >= 8 and not text.isascii()))


def _has_phrase(text: str, phrase: str) -> bool:
    """Whole words only: "house" is in "my house" but not in "500 Warehouse Ave"."""
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I) is not None


def _canonical_place(text: str) -> str:
    return next((name for name, words in _PLACE_ALIASES.items() if any(_has_phrase(text, w) for w in words)), "")


def _match_saved(text: str, saved: list[dict]) -> dict | None:
    """A saved address the user referred to: by its address, or by its label ("my house" -> Home).
    A typed street address is only ever matched by the address itself, so "500 Warehouse Ave" stays
    what the user typed even with a saved "Home"."""
    key = memory.normalize_address(text)
    for a in saved:
        if key and key == memory.normalize_address(a["address"]):
            return a
    if _looks_like_street_address(text):
        return None
    place = _canonical_place(text)
    for a in saved:
        label = a.get("label") or ""
        if label and (_has_phrase(text, label) or (place and _canonical_place(label) == place)):
            return a
    return None


def _set_delivery(draft: dict, address: str = "", label: str = "", kind: str = "", saved: bool = False) -> None:
    if not address:
        draft.pop("delivery", None)
        draft.pop("delivery_location", None)
        return
    draft["delivery"] = {"address": address, "label": label, "kind": kind, "saved": saved}
    draft["delivery_location"] = address


async def saved_addresses(email: str) -> list[dict]:
    """The person's saved permanent addresses, unless the organization switched saved addresses off."""
    return await memory.saved_addresses(email) if PROFILE.features.saved_addresses else []


async def _resolve_address(email: str, text: str, label: str = "", kind: str = "",
                           saved: list[dict] | None = None) -> dict:
    """{"address", "label", "kind", "saved"} for what the user asked, or {"error": ...}."""
    text = " ".join((text or "").split())
    saved = saved if saved is not None else await saved_addresses(email)
    match = _match_saved(text, saved) or (_match_saved(label, saved) if label else None)
    if match:
        return {"address": match["address"], "label": match.get("label", ""), "kind": "permanent", "saved": True}
    if not _looks_like_street_address(text):
        return {"error": f"{text!r} is not a street address. Ask for the full address (street, city, state, "
                         "ZIP). Nothing was changed."}
    if kind not in ("permanent", "temporary"):
        kind = "temporary" if _TEMPORARY.search(f"{text} {label}") else "permanent"
    return {"address": text, "label": (label or _canonical_place(label or text).title()).strip(), "kind": kind,
            "saved": False}


def replace_ship_to(description: str, address: str) -> str | None:
    """The description with its "Ship to:" line replaced, or None if it has none."""
    lines = description.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Ship to:"):
            lines[i] = f"Ship to: {address}"
            return "\n".join(lines)
    return None
