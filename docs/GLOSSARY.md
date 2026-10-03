# Glossary

Terms used in the code and docs, in plain words. Where a term maps to code, the file is named.

**Attempt, verify, note.** The rule for every ticket change. The agent tries the change as the
user, reads the ticket back, and compares. What stuck is reported as changed. What ServiceNow
refused is added to the ticket as a note for the service desk, and the user is told it was
requested, not done. `_apply_changes` in `app/tools/tickets.py`. See
[decision 0007](decisions/0007-attempt-verify-note.md).

**Card.** One screen the agent shows: a title, some fields, and buttons. Built by Python in
`app/cards.py`, never by the model. Rendered as A2UI v0.9, v0.8, or numbered text.

**CI (configuration item).** ServiceNow's record for a device in the CMDB (`cmdb_ci`, usually
`cmdb_ci_hardware`). An asset (`alm_hardware`) links to its CI. A ticket's `cmdb_ci` field ties it
to the equipment, which is how "already reported" works. Users without read access to CIs get no
CI link and no duplicate check.

**Correlation id.** Set on every ticket the agent files: `<session id>:<draft id>`, with
`correlation_display` "Gemini Enterprise - Hardware Replacement agent". Before filing, the agent
looks for an open ticket with that id, so a retried turn or a double click never files twice,
while a second request in the same conversation still files a new ticket.

**Device kind.** How a device is handled (`servicenow._kind`):
- **personal**: assigned to a person. Replaced and shipped.
- **shared**: not assigned to a person (department equipment). Repaired on site by the asset's
  support group, at its location.
- **clinical**: the model category is listed in `devices.clinical_categories` in the profile.
  Handled like shared equipment; shown as "medical equipment".

**Display mode.** How replies are drawn in this conversation: `cards` (A2UI, web app) or `text`
(numbered markdown, mobile app). Asked on the first turn because the apps can't be told apart.
Stored in session state as `ui_mode`. "text" / "buttons" switch it. `inbound.display_step`. See
[decision 0005](decisions/0005-desktop-or-mobile-question.md).

**Draft.** The request being built before it is filed: device, problem, urgency, photos, evidence,
ship-to. Kept in session state under `draft`, with its own short id. Saved after every step. Once
filed it holds `submitted_number`, and later edits go to the ticket instead.

**Follower / watch list.** A person on a ticket's `watch_list`. When someone reports equipment
that already has an open ticket, they can add their note and follow it instead of filing a
duplicate. Followers see the ticket in "my tickets" and can add notes. Only the reporter (the
`caller_id`) can change status, urgency or ship-to, or cancel; a follower's request for those
becomes a note for the desk. When someone else reports a device, the person it is assigned to
and the person in the asset's "Managed by" field are also put on the watch list.

**Memory Bank scope.** Memory Bank stores memories under a scope of `app_name` and `user_id`.
This agent uses two, both keyed by the user's email:
- `hardware_replacement`: memories Memory Bank extracts from conversations (preferences, contact,
  hardware history). Offered as background, never applied.
- `hardware_replacement_addresses`: saved delivery addresses, verbatim.

Sessions are not keyed by email: their `user_id` is `A2A_USER_<contextId>`.

**Numbered options.** In text mode, each card's buttons become "1. ...", "2. ...". A reply of
the number, or the option's words, acts as a click. The options are kept in session state
(`ui_options`) and cleared when a reply has no card, so an old "1" can't trigger a stale button.

**Profile (organization profile).** `config/organization.yaml` (or the file in
`ORGANIZATION_PROFILE`). What the agent says and does for one organization: names, colour,
problem choices, photo and urgency rules, response targets, ticket field mapping. Validated at
start-up by `app/profile.py`. Deployment settings (project, instance URL) are in `.env` instead.

**Relation.** How the reporter relates to a device, shown on the confirm card and noted on the
ticket (`_relation` in `app/tools/devices.py`). Checked, never enforced:
- **yours**: assigned to the reporter.
- **department**: owned by the reporter's department.
- **group**: supported by a group the reporter belongs to.
- **unconfirmed**: none of the above (or the agent couldn't read it). The report is still filed,
  with a note saying ownership could not be confirmed.

**Saved address.** A permanent delivery address (home, office) saved verbatim on submit, one per
label, in its own Memory Bank scope. Offered as a grey button on the review card; never applied
without a click. Hotels and event venues are never saved. See
[decision 0006](decisions/0006-verbatim-saved-addresses.md).

**Staged card.** A card a tool has put in session state (`temp:card`) instead of returning it to
the model. When the model stops calling tools, `agent.render_staged_card` sends the last staged
card, with the model's one sentence as its first line. See
[decision 0003](decisions/0003-deterministic-cards.md).

**Surface.** A2UI's word for one rendered card area. Each card gets a fresh `surfaceId`
(`hw_xxxxxxxx`); reusing one would make Gemini Enterprise redraw the previous card in place.

**ticket_fields.** Profile setting (`servicenow.ticket_fields`): extra incident fields set on
every new ticket, as fixed text or templates with placeholders such as `{device_value}` or
`{issue_value}`. Fields the agent manages itself (caller, category, description, impact,
urgency, CI, watch list, state, correlation) can't be set here. A field ServiceNow refuses is
listed in a note for the desk.
