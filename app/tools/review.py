"""The review step: recomputes urgency, response target and recommendation from the profile,
and decides which step (and card) comes next.
"""

import hashlib
import json

from google.adk.tools import ToolContext

from app import cards, memory, servicenow
from app.cards import ISSUE_LABELS
from app.profile import URGENCY_ORDER
from app.tools._common import PROFILE, _PRIORITY, _save, _show
from app.tools.addresses import saved_addresses
from app.tools.devices import _asset_summary, _device_name

def _refresh(draft: dict, employee: dict) -> dict:
    """Derives priority, recommendation, SLA and warnings from what is known."""
    issue = draft.get("issue") or {}
    category = issue.get("category", "")
    elig = draft.get("eligibility") or {}
    evidence = draft.get("evidence") or {}

    urgency = issue.get("urgency", "normal")
    if urgency not in URGENCY_ORDER:
        urgency = "normal"
    floor = PROFILE.min_urgency(category)  # e.g. a safety concern is always critical
    if floor and URGENCY_ORDER.index(floor) > URGENCY_ORDER.index(urgency):
        urgency = floor
    draft["priority"] = _PRIORITY[urgency]
    device = draft.get("device") or {}
    texts = PROFILE.service.recommendations
    chosen = PROFILE.issue(category)

    if cards.is_equipment(device):
        # Who does it is its own line ("Handled by").
        rec = texts.equipment_safety if PROFILE.is_safety(category) else texts.equipment_repair
    elif elig.get("refresh_eligible"):
        rec = texts.refresh
    elif (chosen and chosen.replace) or evidence.get("supports_replacement"):
        rec = (texts.warranty_replacement if elig.get("in_warranty")
               else texts.charged_replacement.format(cost_center=employee.get("cost_center") or "department"))
    elif chosen and chosen.recommendation:
        rec = chosen.recommendation
    elif elig.get("in_warranty"):
        rec = texts.warranty_repair
    else:
        rec = texts.repair_assessment
    draft["recommendation"] = rec
    targets = (PROFILE.service.equipment_response_targets if cards.is_equipment(device)
               else PROFILE.service.personal_response_targets)
    draft["sla"] = targets[draft["priority"][0]]

    warnings = list(draft.get("photo_warnings") or [])
    if draft.get("match_note"):
        warnings.append(draft["match_note"])
    # Unconfirmed ownership is shown once, as "Belongs to" (and noted on the ticket).
    if device and not device.get("in_inventory"):
        warnings.append("Device not found in inventory; details were read from your photo.")
    suggested = draft.get("suggested_device") or {}
    if suggested and suggested.get("asset_tag") != device.get("asset_tag"):
        warnings.append(f"Your photo shows asset {suggested['asset_tag']} ({suggested.get('model') or 'device'}), "
                        f"not the selected {device.get('asset_tag')}.")
    mismatch = _photo_mismatch(draft)
    if mismatch:
        warnings.append(mismatch)
    need, _ = PROFILE.photo_policy(category)
    if need == "required" and not evidence:
        warnings.append("A photo of the damage is still needed before this can be submitted.")
    draft["warnings"] = warnings
    return draft


def _photo_mismatch(draft: dict) -> str:
    """Flags a photo that shows a different kind or make of device than the one
    selected. Asset tag and serial checks can't catch this when the photo shows
    no label, e.g. a MacBook photo filed against a ThinkPad."""
    device = draft.get("device") or {}
    if not device.get("in_inventory"):
        return ""
    for photo in draft.get("photos") or []:
        f = photo.get("findings") or {}
        seen_type = (f.get("device_type") or "").lower()
        seen_make = (f.get("manufacturer") or "").lower()
        wrong_type = seen_type and seen_type != "other" and seen_type != (device.get("device_type") or "").lower()
        wrong_make = seen_make and seen_make not in (device.get("manufacturer") or "").lower() \
            and seen_make not in (device.get("model") or "").lower()
        if wrong_type or wrong_make:
            seen = " ".join(filter(None, [f.get("manufacturer"), f.get("model")])) or seen_type
            return (f"The photo shows {seen}, but the selected device is "
                    f"{device.get('manufacturer', '')} {device.get('model', '')}. Please confirm the device.").replace("  ", " ")
    return ""


def review_signature(draft: dict) -> str:
    """What the user approves on the review card. If any of it changes, they must see the card again."""
    device, issue = draft.get("device") or {}, draft.get("issue") or {}
    parts = [device.get("asset_tag"), device.get("serial_number"), issue.get("category"), issue.get("description"),
             issue.get("urgency"), (draft.get("delivery") or {}).get("address"), draft.get("priority"),
             draft.get("recommendation"), (draft.get("evidence") or {}).get("summary")]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


async def _review(ctx: ToolContext, draft: dict, employee: dict) -> dict:
    draft = _refresh(draft, employee)
    # Remember what was shown, and in which turn: submit_ticket files only what the user has seen.
    draft["reviewed"] = {"sig": review_signature(draft), "turn": getattr(ctx, "invocation_id", "") or ""}
    if "saved_addresses" not in draft and not cards.is_equipment(draft.get("device") or {}):
        on_file = memory.normalize_address(employee.get("location_address", ""))
        draft["saved_addresses"] = [a for a in await saved_addresses(employee.get("email", ""))
                                    if memory.normalize_address(a["address"]) != on_file][:3]
    _save(ctx, draft)
    _show(ctx, cards.review(draft, employee))
    ship_to = (draft.get("delivery") or {}).get("address") or employee.get("location_address") or employee.get("location")
    return {"status": "ok", "step": "review", "shown": "review card with Submit and Change buttons",
            "priority": draft["priority"], "recommendation": draft["recommendation"],
            "warnings": draft["warnings"], "ship_to": ship_to,
            "saved_addresses_offered_as_buttons": draft.get("saved_addresses", [])}


async def _next_step(ctx: ToolContext, draft: dict, employee: dict) -> dict:
    """Shows whichever step is still missing, so the wizard skips what is known."""
    device, issue = draft.get("device"), draft.get("issue")
    if issue and not issue.get("category"):
        issue = None  # details given before the problem was picked (kept on the draft for set_issue)
    if device and cards.is_equipment(device) and not PROFILE.features.equipment_reporting:
        draft.pop("device", None)
        _save(ctx, draft)
        return {"status": "not_supported", "message": (
            f"{_device_name(device)} is shared or clinical equipment, which this organization doesn't take through "
            "this assistant. Tell the user to contact the service desk for equipment; offer to help with their own "
            "devices.")}
    _save(ctx, draft)
    if not device:
        assets = await servicenow.my_assets(employee["sys_id"])
        _show(ctx, cards.device_picker(employee, assets))
        return {"status": "ok", "step": "choose_device", "assets": _asset_summary(assets)}
    if not device.get("confirmed"):
        _show(ctx, cards.confirm_device(device, draft.get("match_note", "")))
        result = {"status": "ok", "step": "confirm_device", "device": _device_name(device),
                  "belongs_to": device.get("relation_text")}
        if not issue:
            # The model tends to stop at this card; a problem already described would then be asked again.
            result["next"] = ("If the user's message already said what is wrong, call set_issue now, in this "
                              "turn: it is kept while they check the device. Never call confirm_device yourself.")
        return result
    if device.get("ci") and not draft.get("duplicates_checked") and PROFILE.features.follow_open_tickets:
        draft["duplicates_checked"] = True
        _save(ctx, draft)
        existing = await servicenow.open_incidents_for_ci(device["ci"])
        own = not cards.is_equipment(device)
        if own:  # a personal device: only the user's own open ticket for it (D9)
            existing = [t for t in existing if t.get("caller_id") == employee.get("sys_id")]
        if existing:
            _show(ctx, cards.existing_tickets(device, existing, own=own))
            return {"status": "ok", "step": "already_reported",
                    "open_tickets": [{k: t[k] for k in ("number", "state", "short_description", "caller")}
                                     for t in existing]}
    if not issue:
        _show(ctx, cards.issue_picker(device, (draft.get("evidence") or {}).get("category", "")))
        return {"status": "ok", "step": "describe_issue", "device": device.get("model")}
    chosen = PROFILE.issue(issue["category"])
    if chosen and chosen.self_help and not draft.get("self_help_done") and not cards.is_equipment(device) \
            and not PROFILE.is_safety(issue["category"]):
        _show(ctx, cards.self_help(device, ISSUE_LABELS.get(issue["category"], "The problem"), chosen.self_help))
        return {"status": "ok", "step": "self_help", "checks": chosen.self_help}
    need, what = PROFILE.photo_policy(issue["category"])
    if need and not draft.get("evidence") and not draft.get("photo_skipped"):
        _show(ctx, cards.photo_request(device, ISSUE_LABELS.get(issue["category"], "The problem"), what, need == "required"))
        return {"status": "ok", "step": "photo", "photo": need}
    return await _review(ctx, draft, employee)
