# Changelog

Changes by date, newest first, from the git history and `docs/HANDOFF.md`. Revision numbers are
the Cloud Run revisions of the reference deployment, given for orientation only.

Each entry has an **Action needed** line for operators who already run the agent. After any
update: `uv run python -m app.profile`, then `./scripts/deploy.sh`.

## 2026-10-05

**Review fixes R1-R4** (`f730e3b`, revision 00033)
- Correlation id cleaned before queries; close codes and hold reason from the profile (the old hard-coded close code
  wasn't a choice on the reference instance); following/unfollowing safe when people act at once, also across
  servers; optional `servicenow.ship_to_field`.
- Action needed: run `uv run python scripts/sn_profile.py check`. If it reports `close_code` values that aren't
  choices on your instance, set `servicenow.close_codes` in `config/organization.yaml`. To use a ship-to field,
  create it in ServiceNow and set `servicenow.ship_to_field`.

**Personal data out of the repository** (history rewritten before the repo went public)
- Action needed: re-clone; any old clone has the previous history.

## 2026-10-03

**Fixes from fuzzing** (`a5e2373` and the next commit, BACKLOG I)
- Free text can't add a second "Ship to:" line; submit files only what the user saw on the review card
  (a change after the review shows it again first); the reporter check fails closed; profile ServiceNow
  names refuse query operators; tool arguments are coerced to their types; malformed ServiceNow answers
  become clear errors; a device record with nothing to identify it is asked for again.
- Action needed: deploy. If your profile's `ticket_category` or `asset_tables` contain characters other
  than letters, digits, `_`, `-`, `.` or spaces, the profile now refuses to load: fix the value.

**Optional features** (`e45e40d`, `1e94c10`, `1768748`, `9677f06`)
- Profile `features` (switch parts off), `requester_changes`, `devices.category_types`,
  `service.safety_event_url`, per-problem `self_help`; unfollow; "any news?" digest; own open ticket offered
  for the same device; admin-account warning; repeated photos read once; `ticket_filed` log line.
- Action needed: none (all off or unchanged by default). Optional: `./ops/observability.sh --apply`.

**Card wording in `config/messages.yaml`** (`f35ce7b`, BACKLOG G3.3)
- Every card text and the desktop/mobile question come from `app/messages.py` defaults, overridden
  by `config/messages.yaml` (or `MESSAGES_FILE`). Unknown keys, new placeholders and unbalanced
  braces are refused with the key named.
- Action needed: none. If you had changed card text in code, move it to `config/messages.yaml` and
  check with `uv run python -m app.messages`. `deploy.sh` passes `MESSAGES_FILE` to Cloud Run when set.

**`app/tools.py` split into a package** (`3ff4e86`, `788bd2b`, BACKLOG G1.2)
- `app/tools/`: `intake`, `devices`, `addresses`, `review`, `filing`, `tickets`, `_common`. No
  behaviour change.
- Action needed: none, unless you carry local patches to `app/tools.py` (re-apply them to the
  matching module).

**Review fixes: robustness, lower-priority items, test gaps** (`f03bbb2`, `087088a`, `b40ab17`,
`75560c6`, BACKLOG H1-H3)
- ServiceNow timeouts and connection errors are reported as "didn't respond, try again" (a retry
  never files twice); a 403/502 HTML page is no longer mistaken for hibernation.
- Display question: more answers accepted ("1)", "I'm on desktop", "on my phone"); a session read
  blip no longer re-asks; after repeated non-answers text mode is used. Old numbered options are
  cleared when a reply has no card.
- Photos that can't be read, are too large (over 15 MB), of an unsupported type, or sent as a link
  no longer fail the turn.
- Concurrent submits of the same request file one ticket.
- Profile validation is stricter: positional `{}` / `{0}`, format specs and unbalanced braces in
  `ticket_fields` and `recommendations` are rejected at load; an issue key in both lists must have
  the same rules.
- Logs mask the email and log request metadata by key only. ServiceNow JWT access tokens are
  accepted. The container runs as an unprivileged user.
- `seed reset` / `clear-tickets` delete only the agent's tickets for users that existed before.
  `sn_doctor` adds Limit checks.
- Action needed: run `uv run python -m app.profile` (a profile that loaded before may now be
  refused), then redeploy.

## 2026-10-02

**Review fixes, correctness and security** (`4b74c9a`, revision 00031, BACKLOG H0)
- A typed street address is never swapped for a saved one; labels match whole words only.
- Ticket numbers are validated and serials filtered before they go into ServiceNow queries.
- Only the reporter can change status, urgency or ship-to; a follower's request becomes a note.
  `update_ticket` can no longer cancel or close.
- Mobile text mode works when the registration negotiated A2UI v0.8.
- The ticket number is recorded right after creation; follow-up notes are best-effort.
- Custom role: follower-list changes limited to open equipment tickets, and a new business rule
  (`u_hardware_requester: follow only`) lets a requester add or remove only themselves.
- Action needed: **re-run `scripts/servicenow/create_hardware_requester_role.js`** as a
  `security_admin`-elevated admin, then `uv run python scripts/sn_custom_role.py status`
  ([RUNBOOK.md](RUNBOOK.md#re-run-the-custom-role-script-after-updates)). Redeploy.

## 2026-10-01

**ServiceNow field mapping, model-routing evals, custom requester role** (`aa7669f`, `2316962`)
- Profile: `servicenow.ticket_fields` (fixed or templated), `device_values`, `urgency_matrix`.
  Fields ServiceNow refuses are noted on the ticket. The hospital profile sets `contact_type` and
  `subcategory` from the device type.
- `scripts/sn_profile.py`: check the profile against the instance, list choices, import a choice
  list as problem options.
- `evals/run.py`: real model against an in-memory ServiceNow. Found and fixed: the model sometimes
  dropped the problem at the "Is this the right device?" step.
- Custom role `u_hardware_requester`: measured to pass every check `itil` does.
- Action needed: run `uv run python scripts/sn_profile.py check` (the default profile now writes
  `subcategory`). Optional: create the custom role ([ROLES.md](ROLES.md)).

**Docs and demo** (`ed535c3`, `54846d1`)
- README overview, registration screenshots, generic demo users (Jane Doe, John Doe at
  example.com), demo run sheet built from the seed users file.
- Agent card updates no longer need a re-add: edit the agent in Gemini Enterprise and paste the
  card.
- Action needed: none.

## 2026-09-30

**A2UI v0.9, saved addresses, organization profile, install kit** (`e17018f`, revisions 00024-00026)
- Cards are A2UI v0.9 with the brand colour on primary buttons; the agent card declares v0.9 and
  v0.8, and cards are translated for v0.8 clients.
- Permanent delivery addresses saved verbatim in their own Memory Bank scope; hotels never saved;
  saved addresses only offered as buttons.
- Settings moved to `.env` (built-in project, engine and instance defaults removed).
  `config/organization.yaml` holds names, colour, problem choices and rules.
- `scripts/setup.sh`, `scripts/create_state_engine.py`, `scripts/sn_doctor.py`, role scripts,
  install and configuration docs.
- Action needed: create `.env` from `.env.example` (`GOOGLE_CLOUD_PROJECT`, `SN_INSTANCE_URL`,
  `AGENT_ENGINE_ID` are required). Review `config/organization.yaml`. Redeploy, then **update the
  agent card in Gemini Enterprise** (new A2UI versions): edit the agent and paste the card.

## 2026-09-26

**Shared and clinical equipment** (`aac457a`, revision 00023)
- Device kinds (personal, shared, clinical), confirm step, find equipment by description,
  ownership shown and noted, routing to the support group, followers, safety concern.
- Action needed: equipment needs a department, location and support group in ServiceNow for
  routing. Medical model categories go in `devices.clinical_categories`. Demo data:
  `seed/sn_seed.py set`.

**Matching and intake fixes** (`4d25eca`, `2862aad`, revisions 00020-00021)
- Fuzzy matching against the user's own devices (one candidate, always confirmed); intake bugs
  fixed (a second request in one conversation files a new ticket; a photo after submit starts a
  new draft); 31 intake path tests.
- Action needed: none.

**Mobile text mode** (`4661a8c`, revision 00018)
- First reply asks "Desktop or Mobile App?"; mobile gets numbered text.
- Action needed: none.

**Seed tool and handoff docs** (`fe4d57d`, `3f0fa0a`, `c854002`)
- Per-user reset and role grants in the seed tool.
- Action needed: none.

## 2026-09-25

**First version** (`f25d8c9`, `1df2207`)
- Cloud Run A2A agent, A2UI v0.8 wizard, photo reading. ServiceNow per-user OAuth (Table API as
  the user), Agent Runtime used only for Sessions and Memory Bank, attempt-verify-note for ticket
  changes. Seed tool with PDF report and reset.
- Action needed (new install): create the ServiceNow OAuth client and add the agent with its
  authorization in Gemini Enterprise ([INSTALL.md](INSTALL.md)).

## How to write an entry

One block per release: what changed in a few bullets, then **Action needed**. Typical actions:

| Change | Action |
|---|---|
| The profile's `agent` block, declared A2UI versions or input types | Deploy, then edit the agent in Gemini Enterprise and paste the new card |
| `scripts/servicenow/create_hardware_requester_role.js` | Re-run it in ServiceNow (elevated), then `sn_custom_role.py status` |
| New required `.env` setting | Add it to `.env`; `deploy.sh` names anything missing |
| Profile schema made stricter | `uv run python -m app.profile` before deploying |
| Memory Bank topics (`create_state_engine.py`) | Applies to new instances only; update or recreate the instance |
