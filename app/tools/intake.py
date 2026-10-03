"""The request wizard: device, problem, photos and ship-to, up to the review card.
"""

import asyncio
import logging

from google.adk.tools import ToolContext

from app import cards, memory, servicenow, vision
from app.cards import ISSUE_LABELS
from app.tools._common import PROFILE, _PRIORITY, _already_filed, _draft, _employee, _intake_draft, _new_draft, _no_identity, _save, _show, servicenow_errors
from app.tools.devices import _asset_summary, _best_matches, _device_from_asset, _match_note, _nearby_assets, _query_words, _set_device, match_own_asset, normalize_tag
from app.tools.addresses import saved_addresses, _ADDRESS_ON_FILE, _looks_like_street_address, _match_saved, _resolve_address, _set_delivery
from app.tools.review import _next_step

logger = logging.getLogger(__name__)

@servicenow_errors
async def start_request(tool_context: ToolContext) -> dict:
    """Starts a new hardware replacement request and shows the device picker (step 1).

    Call this when the user wants to replace or report broken hardware and no request
    is in progress, or when they click "Start another request".
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _new_draft()
    _save(tool_context, draft)
    tool_context.state["last_photo_ids"] = []
    result = await _next_step(tool_context, draft, employee)
    remembered = (await memory.recall(tool_context, employee["email"])  # addresses are filtered out
                  if PROFILE.features.memory else [])
    return result | {"employee": {k: employee.get(k) for k in ("name", "department", "location")},
                     "remembered_about_user": remembered}


@servicenow_errors
async def select_device(asset_tag: str, tool_context: ToolContext) -> dict:
    """Records which device the request is for, by asset tag, then shows the next step.

    Args:
        asset_tag: The asset tag (or serial number), from a device button or typed by the user.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    asset = await servicenow.find_asset(asset_tag=asset_tag) or await servicenow.find_asset(serial_number=asset_tag)
    how = ""
    if not asset:
        asset, how = match_own_asset(await _nearby_assets(employee), identifier=asset_tag)
    if not asset:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": f"No asset {asset_tag} in inventory; asked the user for a label photo."}
    draft = _set_device(_intake_draft(tool_context), _device_from_asset(asset, employee), asset)
    draft["match_note"] = _match_note(asset, how) if how else ""
    draft["source"] = "tag_fuzzy" if how else "tag"  # a button click or a typed tag/serial (logged at filing)
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def find_device(description: str, tool_context: ToolContext) -> dict:
    """Finds a device or piece of equipment from how the user describes it ("the MRI",
    "the portable x-ray", "bed 312", "the infusion pump in ED bay 7"), then asks the user to
    confirm it. Searches their own devices and their department's equipment first, then
    the whole inventory.

    Args:
        description: What the user called it, including any place they mentioned.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    wanted = _query_words(description)
    if not wanted:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": "Nothing to search for; asked for the asset tag or a photo."}
    candidates = await _nearby_assets(employee)
    best = _best_matches(candidates, wanted)
    if not best and PROFILE.features.equipment_reporting:
        # Beyond their own area, e.g. a pump found in another department.
        for word in sorted(wanted, key=len, reverse=True)[:2]:
            candidates += await servicenow.search_assets(word)
        best = _best_matches(candidates, wanted)
    if not best:
        _show(tool_context, cards.label_photo_request())
        return {"status": "not_found", "message": f"Nothing matched {description!r}; asked for the asset tag or a photo."}
    if len(best) > 1:
        _show(tool_context, cards.device_choices("Which one do you mean?", best[:6]))
        return {"status": "ok", "step": "choose_device", "matches": _asset_summary(best[:6])}
    draft = _set_device(_intake_draft(tool_context), _device_from_asset(best[0], employee), best[0])
    draft["source"] = "description"
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def confirm_device(correct: bool, tool_context: ToolContext) -> dict:
    """The user checked the device details shown (model, asset tag, serial).

    Args:
        correct: True if they said it's the right device, False if not.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if filed := _already_filed(draft):
        return filed
    if not draft.get("device"):
        return await _next_step(tool_context, draft, employee)
    if correct:
        draft["device"]["confirmed"] = True
        return await _next_step(tool_context, draft, employee)
    draft = _set_device(draft, {})
    draft.pop("device")
    result = await _next_step(tool_context, draft, employee)
    return result | {"message": "Not that device. Showed their devices; they can also give the asset tag, "
                                "serial number or a photo of the sticker."}


async def request_label_photo(tool_context: ToolContext) -> dict:
    """Asks the user for a photo of the asset tag or serial label.

    Use when the device isn't in their list ("A different device") or they don't know
    which device it is.
    """
    _show(tool_context, cards.label_photo_request())
    return {"status": "ok", "step": "label_photo"}


@servicenow_errors
async def set_issue(category: str, description: str, urgency: str, tool_context: ToolContext) -> dict:
    """Records what is wrong, then shows the photo step if useful, otherwise the review.

    Args:
        category: For the user's own devices: {PERSONAL_ISSUES}. For shared or
            equipment: {EQUIPMENT_ISSUES}. {SAFETY_RULE}
        description: The problem in the user's own words, lightly cleaned up. Use the
            category label if they only clicked a button.
        urgency: low, normal, high or critical. Infer it from what they said: "I have a
            client demo tomorrow" is high, "can't work at all" is critical. Default normal.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    if category not in ISSUE_LABELS:
        return {"status": "error", "message": f"Unknown category {category!r}. Use one of {list(ISSUE_LABELS)}."}
    draft = _intake_draft(tool_context)
    draft["issue"] = {"category": category, "description": description.strip(),
                      "urgency": urgency if urgency in _PRIORITY else "normal"}
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def analyze_photos(tool_context: ToolContext) -> dict:
    """Reads the photo(s) the user just attached: device, label data and damage.

    Call whenever a message contains "[Photo attached: ...]". Fills in the device from the
    asset tag or serial number, records damage as evidence, then shows the next step.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    photo_ids = tool_context.state.get("last_photo_ids") or []
    photos = [tool_context.state.get(f"photo:{pid}") for pid in photo_ids]
    photos = [p for p in photos if p]
    if not photos:
        return {"status": "error", "message": "No new photo found. Ask the user to attach it again."}

    draft = _intake_draft(tool_context)
    if not PROFILE.features.photo_analysis:
        return await _photos_unread(tool_context, draft, employee, photos)
    hint = (draft.get("issue") or {}).get("description", "")
    # A photo already read in this request (same bytes, sent again) isn't sent to the model again.
    known = {p.get("sha256"): p.get("findings") for p in draft.get("photos") or [] if p.get("sha256") and p.get("findings")}
    fresh = [p for p in photos if p.get("sha256") not in known]
    results = await asyncio.gather(
        *(vision.analyze_photo(p["uri"], p["mime_type"], hint) for p in fresh), return_exceptions=True
    )
    read = {id(p): r for p, r in zip(fresh, results)}
    findings = []
    for photo in photos:
        if photo.get("sha256") in known:
            logger.info("photo %s is a repeat; reusing its findings", photo["photo_id"])
            continue  # already on the draft, with its findings
        result = read[id(photo)]
        if isinstance(result, Exception):
            logger.warning("photo analysis failed for %s: %s", photo["photo_id"], result)
            continue
        findings.append((photo, result.model_dump(mode="json")))
    tool_context.state["last_photo_ids"] = []
    if not findings and len(fresh) < len(photos):
        result = await _next_step(tool_context, draft, employee)
        return result | {"findings": [], "note": "That photo was already received for this request."}
    if not findings:
        return {"status": "error", "message": "The photo could not be read. Ask the user to retake it in better light."}

    draft.setdefault("photos", [])
    photo_warnings = list(draft.get("photo_warnings") or [])
    note = ""
    owned: list[dict] | None = None
    for photo, f in findings:
        draft["photos"].append({"uri": photo["uri"], "photo_id": photo["photo_id"], "filename": photo.get("filename", ""),
                                "mime_type": photo["mime_type"], "findings": f, "sha256": photo.get("sha256", "")})

        # Label data -> inventory match; failing that, one of the user's own devices.
        current = draft.get("device") or {}
        has_id = bool(f["asset_tag"] or f["serial_number"])
        asset = await servicenow.find_asset(f["asset_tag"], f["serial_number"]) if has_id else None
        how = ""
        if not asset and not current and f["image_kind"] != "unrelated":
            if owned is None:
                owned = await _nearby_assets(employee)
            asset, how = match_own_asset(owned, f["serial_number"] or f["asset_tag"], f["model"], f["manufacturer"])
        if asset:
            if current and current.get("asset_tag") != asset["asset_tag"]:
                # Offered on the review card as "Use <device> instead" (see _refresh).
                draft["suggested_device"] = _device_from_asset(asset, employee)
            elif not current:
                _set_device(draft, _device_from_asset(asset, employee, confirmed=not how), asset)
                draft["match_note"] = _match_note(asset, how) if how else ""
                draft["source"] = "photo_match" if how else "photo_label"
            photo_serial = f["serial_number"].upper().replace(" ", "")
            if not how and photo_serial and asset.get("serial_number") and photo_serial != asset["serial_number"].upper():
                photo_warnings.append(
                    f"Serial on the label ({photo_serial}) differs from inventory ({asset['serial_number']}).")
        elif has_id and not current:
            _set_device(draft, {
                "asset_tag": normalize_tag(f["asset_tag"]), "serial_number": f["serial_number"],
                "manufacturer": f["manufacturer"], "model": f["model"], "part_number": f["part_number"],
                "device_type": f["device_type"], "in_inventory": False, "confirmed": True,
                "kind": "clinical" if f["device_type"] == "medical equipment" else "personal",
                "relation": "unconfirmed", "relation_text": "Not found in inventory",
                "ownership_note": "Not found in inventory; details were read from the reporter's photo",
            })
        elif not current and f["image_kind"] in ("label", "device"):
            note = "I couldn't read an asset tag or serial number. Try a closer, sharper photo of the label."

        # Damage -> evidence.
        if f["damage_present"]:
            draft["evidence"] = {
                "summary": f"{f['damage_description']} (severity: {f['damage_severity']})",
                "severity": f["damage_severity"], "category": f["issue_category"],
                "supports_replacement": f["supports_replacement"], "photo_uri": photo["uri"],
            }
        elif draft.get("issue") and PROFILE.photo_policy(draft["issue"]["category"])[0] == "required" \
                and f["image_kind"] not in ("label",) \
                and (draft.get("evidence") or {}).get("severity", "none") == "none":
            # Only when no photo so far showed damage: a wide shot after a close-up keeps the close-up.
            photo_warnings.append("The photo didn't clearly show the damage; the desk may ask for another.")
            draft["evidence"] = {"summary": "Photo provided; damage not clearly visible", "severity": "none",
                                 "category": "", "supports_replacement": False, "photo_uri": photo["uri"]}
    if (draft.get("evidence") or {}).get("severity", "none") != "none":
        photo_warnings = [w for w in photo_warnings if not w.startswith("The photo didn't clearly show the damage")]
    draft["photo_warnings"] = list(dict.fromkeys(photo_warnings))

    summary = [{"image_kind": f["image_kind"], "device": f"{f['manufacturer']} {f['model']}".strip(),
                "asset_tag": f["asset_tag"], "serial": f["serial_number"],
                "damage": f["damage_description"] if f["damage_present"] else "none visible",
                "confidence": f["confidence"]} for _, f in findings]

    if not draft.get("device"):
        _save(tool_context, draft)
        assets = await servicenow.my_assets(employee["sys_id"])
        _show(tool_context, cards.photo_findings(findings[-1][1], assets, note))
        return {"status": "ok", "step": "device_unknown", "findings": summary, "assets": _asset_summary(assets)}
    result = await _next_step(tool_context, draft, employee)
    return result | {"findings": summary}


async def _photos_unread(tool_context: ToolContext, draft: dict, employee: dict, photos: list[dict]) -> dict:
    """Photo analysis is switched off: the photos go on the ticket as they are, for the desk to look at."""
    tool_context.state["last_photo_ids"] = []
    draft.setdefault("photos", [])
    for photo in photos:
        draft["photos"].append({"uri": photo["uri"], "photo_id": photo["photo_id"], "filename": photo.get("filename", ""),
                                "mime_type": photo["mime_type"], "findings": {}})
    if not draft.get("evidence"):
        draft["evidence"] = {"summary": f"{len(draft['photos'])} photo(s) attached for the service desk",
                             "severity": "unknown", "category": "", "supports_replacement": False,
                             "photo_uri": photos[-1]["uri"]}
    result = await _next_step(tool_context, draft, employee)
    return result | {"photos": len(photos), "analyzed": False}


@servicenow_errors
async def self_help_result(fixed: bool, tool_context: ToolContext) -> dict:
    """The user tried the quick checks on the card. Fixed: nothing is filed. Not fixed: continue
    to filing, with the checks recorded on the ticket.

    Args:
        fixed: True if the checks fixed it ("that fixed it"), False if it still doesn't work.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if filed := _already_filed(draft):
        return filed
    issue = draft.get("issue") or {}
    chosen = PROFILE.issue(issue.get("category", ""))
    if fixed:
        logger.info("self_help_fixed %s", issue.get("category", ""))  # deflected: no ticket (D6)
        _save(tool_context, _new_draft())
        _show(tool_context, cards.self_help_done())
        return {"status": "ok", "step": "done", "filed": False}
    draft["self_help_done"] = True
    draft["self_help_tried"] = list(chosen.self_help) if chosen else []
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def skip_photo(tool_context: ToolContext) -> dict:
    """The user chose not to add a photo. Moves on to the review step."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if filed := _already_filed(draft):
        return filed
    draft["photo_skipped"] = True
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def update_request(tool_context: ToolContext, description: str = "", urgency: str = "",
                         category: str = "", delivery_location: str = "", delivery_label: str = "",
                         delivery_kind: str = "") -> dict:
    """Changes details on the request, then shows the review card again.

    Args:
        description: New problem description, if the user changed it.
        urgency: low, normal, high or critical, if the user changed it.
        category: New issue category, if the user changed it.
        delivery_location: Only when the user asked to ship somewhere else: the full street
            address as they typed it, or their words for a saved place ("my house"), which is
            looked up. "my usual address" goes back to the address on file.
        delivery_label: A short name for a new address, e.g. "Home", "Denver office", "Hotel".
        delivery_kind: "permanent" (home, an office) or "temporary" (hotel, event, trip).
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if filed := _already_filed(draft):
        return filed
    issue = dict(draft.get("issue") or {})
    if description:
        issue["description"] = description.strip()
    if urgency in _PRIORITY:
        issue["urgency"] = urgency
    if category in ISSUE_LABELS:
        issue["category"] = category
    if issue:
        draft["issue"] = issue
    if delivery_location:
        if _ADDRESS_ON_FILE.search(delivery_location) and not _looks_like_street_address(delivery_location):
            _set_delivery(draft)
        else:
            found = await _resolve_address(employee["email"], delivery_location, delivery_label, delivery_kind,
                                           draft.get("saved_addresses"))
            if "error" in found:
                _save(tool_context, draft)
                return {"status": "need_address", "message": found["error"]}
            _set_delivery(draft, **found)
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def choose_ship_to(address: str, tool_context: ToolContext) -> dict:
    """The user picked where to ship from the review card: a saved address, or "" for the
    address on file in ServiceNow.

    Args:
        address: The saved address from the button, or "" for the address on file.
    """
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    draft = _draft(tool_context)
    if filed := _already_filed(draft):
        return filed
    if not address:
        _set_delivery(draft)
    else:
        match = _match_saved(address, draft.get("saved_addresses") or await saved_addresses(employee["email"]))
        if not match:
            return {"status": "not_found", "message": "That saved address wasn't found. Ask for the full address."}
        _set_delivery(draft, match["address"], match.get("label", ""), "permanent", saved=True)
    return await _next_step(tool_context, draft, employee)


@servicenow_errors
async def show_review(tool_context: ToolContext) -> dict:
    """Shows the current step again: the review card if everything is filled in."""
    employee = await _employee(tool_context)
    if not employee:
        return _no_identity(tool_context)
    return await _next_step(tool_context, _draft(tool_context), employee)


# set_issue's argument docs list this organization's problem choices (config/organization.yaml).
_safety_keys = [i.key for i in PROFILE.issues.equipment + PROFILE.issues.personal if i.safety]


set_issue.__doc__ = (set_issue.__doc__
                     .replace("{PERSONAL_ISSUES}", ", ".join(PROFILE.keys("personal")))
                     .replace("{EQUIPMENT_ISSUES}", ", ".join(PROFILE.keys("equipment")))
                     .replace("{SAFETY_RULE}", f"Use {' or '.join(dict.fromkeys(_safety_keys))} whenever a person is, "
                              "or could be, put at risk." if _safety_keys else ""))
