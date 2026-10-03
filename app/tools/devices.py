"""Devices: finding the user's device or equipment, fuzzy matching typed or photographed
identifiers, ownership (yours / department / group / unconfirmed) and refresh eligibility.
"""

import datetime as dt
import re

from app import servicenow
from app.tools._common import PROFILE

# Words that don't help find equipment by description.
_STOP_WORDS = {"the", "a", "an", "in", "on", "at", "of", "to", "is", "it", "its", "our", "my", "and", "or", "with",
               "has", "have", "not", "broken", "down", "working", "please", "there", "this", "that", "room", "bay",
               "floor", "unit", "department", "dept", "one", "machine", "device", "equipment"}


def normalize_tag(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", (value or "").upper())


def eligibility(asset: dict) -> dict:
    """Warranty and refresh status, which decide how a replacement is funded."""
    out = {"in_warranty": False, "refresh_eligible": False, "age_years": None, "summary": "Unknown device age"}
    today = dt.date.today()
    try:
        purchased = dt.date.fromisoformat((asset.get("purchase_date") or "")[:10])
    except ValueError:
        purchased = None
    warranty_end = (asset.get("warranty_end") or "")[:10]
    try:
        out["in_warranty"] = bool(warranty_end) and dt.date.fromisoformat(warranty_end) >= today
    except ValueError:
        warranty_end = ""
    if purchased:
        age = (today - purchased).days / 365.25
        out["age_years"] = round(age, 1)
        years = PROFILE.devices.refresh_years.get(asset.get("device_type", ""), PROFILE.devices.default_refresh_years)
        out["refresh_eligible"] = age >= years
    if out["refresh_eligible"]:
        out["summary"] = f"Refresh eligible ({out['age_years']} yrs old)"
    elif out["in_warranty"]:
        out["summary"] = f"Under warranty until {warranty_end}"
    elif warranty_end:
        out["summary"] = f"Out of warranty since {warranty_end}"
    return out


# Characters a label photo (or a person) commonly confuses, mapped to one form.
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})


def _loose(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper()).translate(_CONFUSABLE)


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
        else:
            i += 1
            j += 1
    return edits + (len(b) - j) <= 1


def _words(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def match_own_asset(assets: list[dict], identifier: str = "", model: str = "",
                    manufacturer: str = "") -> tuple[dict | None, str]:
    """Picks one of the user's own devices when an exact inventory lookup failed.

    `identifier` is a serial number or asset tag as read or typed: compared with
    commonly confused characters folded (O/0, I/1, S/5...), one wrong, missing or
    extra character allowed, and Apple's "S" prefix ignored. `model` is compared
    by name. Only a single candidate counts. Returns (asset, what matched).
    """
    raw = re.sub(r"[^A-Z0-9]", "", (identifier or "").upper())
    if len(raw) >= 5:
        keys = {_loose(raw)} | ({_loose(raw[1:])} if raw.startswith("S") and len(raw) >= 9 else set())
        for field, label in (("serial_number", "serial number"), ("asset_tag", "asset tag")):
            hits = [a for a in assets if (target := _loose(a.get(field, ""))) and len(target) >= 5
                    and any(k == target or (len(target) >= 6 and _within_one_edit(k, target)) for k in keys)]
            if len(hits) == 1:
                return hits[0], label
    wanted = _words(model)
    if len(wanted) >= 4:
        maker = _words(manufacturer)
        hits = [a for a in assets
                if (have := _words(a.get("model", ""))) and (wanted in have or have in wanted)
                and (not maker or not a.get("manufacturer") or maker in _words(a["manufacturer"])
                     or _words(a["manufacturer"]) in maker)]
        if len(hits) == 1:
            return hits[0], "model"
    return None, ""


def _match_note(asset: dict, how: str) -> str:
    return (f"Matched to your {_device_name(asset)} by its {how}. If that's not the right device, "
            "choose Change something.")


def _device_name(asset: dict) -> str:
    return f"{asset.get('model') or 'device'} ({asset.get('asset_tag', '')})"


def _asset_summary(assets: list[dict]) -> list[dict]:
    return [{"asset_tag": a["asset_tag"], "type": a.get("device_type"), "model": a.get("model")} for a in assets]


def _relation(asset: dict, employee: dict) -> tuple[str, str, str]:
    """How the reporter relates to the device: (relation, text for the user, note
    for the ticket). Anyone may report anything; this only says what is known."""
    me, name = employee.get("sys_id"), employee.get("name", "the reporter")
    if asset.get("assigned_to"):
        if asset["assigned_to"] == me:
            return "yours", "You", "Assigned to the reporter"
        owner = asset.get("assigned_to_name") or "another person"
        return ("unconfirmed", f"{owner}, not you. You can still report it; the ticket will say so.",
                f"Ownership could not be confirmed: assigned to {owner}, reported by {name}")
    if asset.get("department_id") and asset["department_id"] == employee.get("department_id"):
        return ("department", f"{asset.get('department')} (your department)",
                f"Department equipment ({asset.get('department')}); the reporter is in this department")
    if asset.get("support_group_id") and asset["support_group_id"] in (employee.get("group_ids") or []):
        return ("group", f"{asset.get('department') or 'Shared equipment'}, supported by your group "
                f"({asset.get('support_group')})",
                f"Supported by {asset.get('support_group')}; the reporter is a member of that group")
    owner = asset.get("department") or "no department on record"
    return ("unconfirmed", f"{owner}. Not registered to you or your department; you can still report it "
            "and the ticket will say so.",
            f"Ownership could not be confirmed: registered to {owner}, reported by {name}, who is not "
            "in that department or its support group")


def _device_from_asset(asset: dict, employee: dict, confirmed: bool = False) -> dict:
    keys = ("sys_id", "asset_tag", "serial_number", "manufacturer", "model", "device_type", "purchase_date",
            "warranty_end", "assigned_to", "assigned_to_name", "ci", "kind", "category", "department",
            "department_id", "location", "location_id", "support_group", "support_group_id", "cost_center",
            "managed_by")
    relation, text, note = _relation(asset, employee)
    return {k: asset.get(k, "") for k in keys} | {
        "in_inventory": True, "relation": relation, "relation_text": text, "ownership_note": note,
        # A label photo that matched exactly already proves which device it is.
        "confirmed": confirmed}


def _set_device(draft: dict, device: dict, asset: dict | None = None) -> dict:
    """Puts a device on the draft, clearing what belonged to the previous one."""
    for key in ("suggested_device", "duplicates_checked", "match_note"):
        draft.pop(key, None)
    previous = draft.get("device") or {}
    if previous and (previous.get("asset_tag"), previous.get("serial_number")) != \
            (device.get("asset_tag"), device.get("serial_number")):
        # A different device: the photos, damage and label warnings were about the old one.
        for key in ("photos", "evidence", "photo_warnings", "photo_skipped"):
            draft.pop(key, None)
    draft["device"] = device
    if asset:
        draft["eligibility"] = eligibility(asset)
    else:
        draft.pop("eligibility", None)
    return draft


def _words_of(asset: dict) -> set[str]:
    text = " ".join(str(asset.get(k) or "") for k in ("model", "manufacturer", "category", "device_type",
                                                      "location", "department", "asset_tag"))
    return set(_words(text).split())


def _score(asset: dict, wanted: list[str]) -> int:
    have = _words_of(asset)
    return sum(1 for w in wanted if w in have or (len(w) >= 4 and any(w in h for h in have)))


def _query_words(description: str) -> list[str]:
    return [w for w in _words(description).split() if w not in _STOP_WORDS and (len(w) > 1 or w.isdigit())]


async def _nearby_assets(employee: dict) -> list[dict]:
    """The user's own devices and their department's equipment: where a fuzzy
    match or a description is most likely to point."""
    own = await servicenow.my_assets(employee["sys_id"])
    dept = await servicenow.department_assets(employee.get("department_id", ""))
    seen = {a["sys_id"] for a in own}
    return own + [a for a in dept if a["sys_id"] not in seen]


def _best_matches(assets: list[dict], wanted: list[str]) -> list[dict]:
    unique = list({a["sys_id"]: a for a in assets}.values())
    scored = [(a, _score(a, wanted)) for a in unique]
    top = max((sc for _, sc in scored), default=0)
    return [a for a, sc in scored if top and sc == top]
