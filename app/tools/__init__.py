"""Wizard tools. Each one advances the request and stages the next card.

A tool never returns UI JSON to the model. It puts the card in `temp:card`,
returns a short summary, and `agent.render_staged_card` sends the card to the
user in place of the next model call. That keeps the step order in the model's
hands while the cards themselves stay deterministic.

Every ServiceNow call runs as the signed-in person, with the token Gemini
Enterprise forwarded (see `servicenow.py`). The requester comes from that
token, never from a tool argument, so the model cannot file or read tickets
on someone else's behalf.

The tools live in submodules (one job each); this package exposes them to the agent:

    _common    staged card, draft in session state, who is asking, ServiceNow errors
    devices    finding and matching devices, ownership, refresh eligibility
    addresses  ship-to: address on file, saved permanent addresses, typed addresses
    review     urgency, response target, recommendation; which step comes next
    intake     the request wizard up to the review card
    filing     submit: one ticket per request, then best-effort notes and photos
    tickets    existing tickets: list, show, change, cancel, follow
"""

from app.tools._common import CARD_KEY, PROFILE, servicenow_errors  # noqa: F401
from app.tools.addresses import _TEMPORARY, _looks_like_street_address, replace_ship_to  # noqa: F401
from app.tools.devices import eligibility, match_own_asset, normalize_tag  # noqa: F401
from app.tools.filing import submit_ticket
from app.tools.intake import (analyze_photos, choose_ship_to, confirm_device, find_device, request_label_photo,
                              select_device, set_issue, show_review, skip_photo, start_request, update_request)
from app.tools.tickets import (add_ticket_note, cancel_ticket, change_ticket_shipping, follow_ticket, get_ticket,
                               list_my_tickets, report_separately, request_urgent_handling, unfollow_ticket,
                               update_ticket)

ALL_TOOLS = [start_request, select_device, find_device, confirm_device, request_label_photo, set_issue,
             analyze_photos, skip_photo, update_request, choose_ship_to, show_review, submit_ticket, follow_ticket,
             report_separately, list_my_tickets, get_ticket, update_ticket, add_ticket_note, change_ticket_shipping,
             request_urgent_handling, cancel_ticket, unfollow_ticket]
