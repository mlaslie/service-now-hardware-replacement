# Backlog

Created 2026-09-26. The agent works end to end; this is the next round of work.
Priority: **P0** = bug or blocks the next manual test, **P1** = next up, **P2** = later.
Size: S (< half a day), M (about a day), L (several days).

Suggested order: A0 fixes → A1 path tests → B demo → E0 mobile probe → C setup → D ideas.

---

## A. Verify every intake path in code before manual testing

A code review of the wizard (`app/tools.py`, `app/agent.py`, `app/vision.py`,
`servicenow.find_asset`) against the ways a user can identify a device. Today the only
automated tests for tools are the photo-mismatch check, the ship-to replacement and
`_apply_changes`; **no intake path is tested**.

### A0. Bugs found in the review (P0): **fixed 2026-09-26 (revision 00020)**

| ID | Bug | Where | Fix | Size |
|---|---|---|---|---|
| A0.1 | **A second request in the same conversation returns the first ticket.** "Start another request" resets the draft, but `submit_ticket` looks up an open incident by `correlation_id` = session id, finds ticket 1 and shows it instead of filing ticket 2. | `submit_ticket` | Give each draft its own id (set in `start_request`) and use `<session id>:<draft id>` as the correlation id | S |
| A0.2 | **A photo sent after a finished request edits the old draft.** `analyze_photos` (and `set_issue`, `select_device`) reuse a draft that already has `submitted_number`; the next submit then says "already submitted". | `_draft` callers | If the draft is submitted, start a fresh one when any intake tool runs | S |
| A0.3 | Typed asset tag or serial isn't routed. The instruction only mentions typed values under the `no_label` button, so "my asset tag is 123456, screen is cracked" depends on the model guessing `select_device`. | `INSTRUCTION` | Add a rule: a tag or serial in free text → `select_device` (after `start_request` if needed), in the same turn as `set_issue` | S |
| A0.4 | A photo that shows a **different** inventory device than the one selected only raises a warning; there's no way to switch. | `analyze_photos` | Return the matched asset and let the review card offer "Use <asset> instead" | S |

### A1. Intake path matrix: automated tests (P0, M): **done, `tests/test_paths.py` (31 tests)**

Runs the real tools and ServiceNow client; only the HTTP call, the photo model, photo upload and Memory Bank are faked. The A0 bug tests were checked to fail on the old code.

Add `tests/test_paths.py`. It should use a fake ServiceNow (like `FakeSN` in `test_sn_seed.py`), a stubbed
`vision.analyze_photo` returning canned `PhotoFindings`, and a fake `ToolContext`. Each row
asserts the resulting draft (device, in_inventory, evidence, warnings) and the card shown.

| # | User gives | Expected |
|---|---|---|
| 1 | Description only, 1 device | Device auto-selected by the model? (see A2), issue set, photo step |
| 2 | Description only, several devices | Device picker with the likely one highlighted |
| 3 | Typed asset tag (`123456`, `#123456`, `it-123456`) | Matched; `#`/case/spaces normalized |
| 4 | Typed serial (`FCPJ2GJTHC`, lower case, with spaces) | Matched via the serial fallback |
| 5 | Typed tag/serial not in inventory | Label-photo card; user can type again |
| 6 | Typed tag of **someone else's** asset | Selected with the "assigned to someone else" warning |
| 7 | Photo of asset-tag sticker only | Device from tag, no evidence, photo step still offered for damage |
| 8 | Photo of maker label (model + serial, no tag) | Matched by serial; part number kept |
| 9 | Photo of maker label with model/part number but **no readable serial** | Today: "device unknown" picker. Proposed: auto-pick if exactly one owned asset matches the model (A3) |
| 10 | Photo of damage with sticker visible | Device and evidence both filled in one step → review |
| 11 | Photo of damage, no label, device already selected | Evidence only; make/type mismatch check |
| 12 | Photo of damage, no label, nothing selected | Findings card with the user's devices |
| 13 | Label serial ≠ inventory serial for that tag | Serial-mismatch warning |
| 14 | Photo shows a different owned device than selected | A0.4 behaviour |
| 15 | Unrelated or unreadable photo | Friendly retake message, draft unchanged |
| 16 | Two photos in one message (label + damage) | Both applied; device from the label, evidence from the damage |
| 17 | Photo first, before any request | Fresh draft (A0.2) |
| 18 | Serial with OCR-confusable characters (`0/O`, `1/I`, `5/S`) or Apple's leading `S` | Proposed fuzzy match against the user's own assets (A3) |
| 19 | Device not in inventory at all (read from the photo) | `in_inventory=False`, warning, ticket filed without `cmdb_ci` |
| 20 | Second request in the same conversation | New ticket (A0.1) |

### A2. Model-routing evals (P1, M)
The table above tests the tools; whether the **model** calls the right tool from free text needs
evals. Use `agents-cli eval` with an evalset of about 15 one-line openers ("asset 123456 won't turn on",
"S/N FCPJ2GJTHC cracked screen, demo tomorrow", "here's the sticker" + photo marker,
"file it", "show my tickets", "the tracking number doesn't work, reopen it"). Assert the tool
trajectory, and assert that no tool is called that the user didn't ask for (e.g. no submit without consent).

### A3. Matching improvements (P1, S)
- If an exact lookup fails, fall back to the user's **own** assets. Match by model or part number
  when exactly one fits, and by serial with edit distance ≤ 1 after mapping confusable characters. Always confirm
  on the review card ("Matched to your MacBook Air, serial FCPJ2GJTHC").
- Say in the vision prompt that Apple labels print "Serial (S)" and that barcodes can't be decoded, so
  only printed text counts.
- Remove the hard-coded "IT-#####" hint; read the tag format from config.

---

## B. Demo script with the two test users (P1, S)

Write `demo/DEMO-2-USERS.md` (keep `demo/DEMO.md` for the one-user flow). Prerequisites:
seed is reset and set up, Jane's password is set, each user has authorized the agent once
(in separate browser profiles, never while an admin is signed in).

1. **Jane (employee), about 3 min:** "My laptop screen is cracked and I have a client meeting tomorrow"
   → device auto-picked → photo → review shows the Kansas City ship-to and Sales cost center →
   submit. The ticket shows Jane's real name from ServiceNow.
2. **Jane, typed path, about 1 min:** "Asset <Jane's monitor tag> has dead pixels" → goes straight to the photo step.
3. **John (IT, Jane's manager), about 2 min:** "Show my tickets". Jane's ticket is **not** listed, because
   each user sees only their own. In ServiceNow, John assigns Jane's ticket and adds a work note.
4. **Jane, about 2 min:** "Any updates?" → fresh status. "Ship it to my home instead: <address>"
   → changed, or a policy note is added and Jane is told plainly. "Cancel it, I found a spare" → cancelled with the reason.
5. Close with the seed PDF report as the "what each person has" handout.

Depends on A0.1 (step 2 files a second ticket in the same conversation, unless the demo uses a new chat).

---

## C. Replicate in someone else's Google Cloud (P1, M)

Goal: one page, `docs/SETUP.md`, plus scripts so a new owner runs about 5 commands.

Code changes that make it portable:
- **C1.** Remove this environment's defaults from `app/config.py` and `scripts/deploy.sh`
  (`PROJECT_ID`, engine `5797…`, `INSTANCE`). Require them in a `.env` read by
  `deploy.sh`, and ship a `.env.example`. (S)
- **C2.** `scripts/bootstrap.sh`: enable the APIs (run, cloudbuild, aiplatform, storage, discoveryengine),
  create the bucket and service account, and grant the roles. (S)
- **C3.** `scripts/create_state_engine.py`: create the code-less Agent Runtime engine with the
  Memory Bank topics (USER_PREFERENCES, delivery_and_contact, hardware_history) and print its ID.
  Today this engine was created by hand and isn't reproducible. (S)
- **C4.** ServiceNow steps with screenshots: an OAuth client in the Application Registry (authorization code,
  GE redirect URIs), the user roles, and the seed script client. (S)
- **C5.** Gemini Enterprise steps: add the A2A agent from the card URL, create the authorization
  (auth URL, token URL, scope `useraccount`), and authorize as a real user. Include the gotchas table
  (one authorization per agent; delete and re-add to change it; don't authorize while signed in as admin). (S)

SETUP.md outline: prerequisites → `cp .env.example .env` → `bootstrap.sh` → `create_state_engine.py`
→ ServiceNow client + roles → `deploy.sh` → add the agent in GE → optional `sn_seed.py set` → smoke test.

---

## D. Ideas to make the agent better and more streamlined (P2 unless noted)

**Fewer steps**
- **D1. One-shot intake (P1):** a single photo plus one sentence goes straight to review. This mostly works
  already; make it the documented happy path and eval it (A2).
- **D2.** Auto-submit option: "file it" in the opening message plus a complete draft → submit with a
  confirmation card that has an Undo (cancel) button, instead of a separate review step.
- **D3.** Loaner request toggle on the review card for blocking issues.

**Better outcomes**
- **D4.** Service Catalog item instead of an incident (the production path in the handoff). It removes the
  need for `itil`/`sn_incident_write` on employees and sets fields server-side.
- **D5.** Scripted REST "my devices" endpoint, so employees don't need the `asset` role.
- **D6.** Self-help deflection for `performance`/`wont_power_on`: 2–3 guided checks before filing,
  logged in the ticket.
- **D7.** Returns/logistics: after fulfilment, show the return label and shipping instructions for the old device.

**Proactive and status**
- **D8.** "What's happening with my replacement?" digest: all open tickets in one card with the latest note
  (single ServiceNow query).
- **D9.** Warn on duplicates: an open hardware ticket for the same asset already exists → offer to add a note to it instead.

**Quality and operations**
- **D10.** Structured logging of each path (intake source: typed/photo-label/photo-damage/picker)
  to measure which paths users actually take.
- **D11.** Admin/system-account guard: if the resolved ServiceNow user has the `admin` role, warn
  (problem 12 in the handoff).
- **D12.** Cost: skip the vision call when the photo is a duplicate (same hash) within a request.

---

## E. Mobile: a text-only experience (needs a decision)

**Why:** the Gemini Enterprise mobile app doesn't render A2UI.

**What we already know (measured):** A2A on Agent Runtime loses the user's identity (the gateway
replaces `Authorization`). ADK agents registered **directly** on Agent Runtime get the GE
authorization's token in session state instead (the documented GE OAuth pattern). That path was never tested here.

### Measured 2026-09-26
- The GE mobile app **does** use A2A agents and can upload photos. Identity works: the ServiceNow token
  is passed through as on desktop.
- Any response containing A2UI fails on mobile with "Response contains unsupported content".
- **Mobile and desktop requests are identical.** Both send `X-A2A-Extensions` with A2UI v0.8,
  `a2uiClientCapabilities` (basic catalog) in the params metadata, and user-agent `Google`, with no
  message metadata. Mobile advertises A2UI support it doesn't have, so **auto-detection isn't
  possible** from the request. Option A stays (one Cloud Run agent); B and C aren't needed.
- On mobile, a reply with text **and** a card shows the text plus a red "unsupported content" box.

**Decided and built (revision 00018):** the first reply of every conversation asks "Are you using the
Gemini Enterprise mobile app? 1 = Yes, 2 = No". Mobile gets text only, with numbered options (a
number or the option's words acts as a click). The web app gets cards. The first message is saved
and replayed once they answer. "text" / "buttons" switch modes. A click on the first turn skips the
question. Code: `inbound.display_step`, `cards.to_text`, `agent.ask_display_mode`, `apply_display_mode`
in `server.py`; tests in `tests/test_display.py`. The options below are kept for the record.

### Options

| | A. One agent, Cloud Run A2A, text fallback | B. Same code, two deployments | C. Text-only agent on Agent Runtime only |
|---|---|---|---|
| What | Current agent renders either A2UI or numbered text | Cloud Run A2A (rich) + Agent Runtime ADK (text), shared `app/` package | Replace the rich agent |
| GE listing | One agent | Two agents ("Hardware Replacement", "… (mobile)") | One agent |
| Identity | Proven | Rich: proven. Text: GE authorization token in state (to verify) | To verify |
| Works on mobile | **Only if GE mobile can use A2A agents at all** (unknown) | Yes, if GE mobile supports ADK agents (to verify) | Same |
| Maintenance | Lowest | Medium: two deploys, two authorizations | Loses the rich UI |

**Recommendation:** build the text renderer first (useful in every option), then choose between A
and B based on a 1-hour probe. Aim for **A** (a single agent).

### Work items
- **E0. Probe (P0 for this track, S).** From the GE mobile app, check:
  1. Is the existing A2A agent listed and usable at all?
  2. Log the request headers and the A2A extension activation (`X-A2A-Extensions`, message `extensions`,
     `metadata`) from desktop vs mobile. If mobile doesn't activate the A2UI extension, the agent can
     **auto-detect** and no question is needed.
  3. Can mobile attach photos to an agent?
  4. If A2A isn't available on mobile: does a minimal ADK agent on Agent Runtime with a ServiceNow
     authorization receive the token on mobile?
- **E1. Text renderer (P1, M).** `cards.py` already builds every screen from a small model
  (title, fields, buttons with an action and context). Add `render_text()`:
  plain lines plus **numbered options** ("1. MacBook Air 13 (123456)  2. Dell monitor  3. None of these").
  Store the numbered actions in state. In `inbound.rewrite_parts`, turn a reply of "2" (or "option 2",
  or the option's words) into the same `[UI action] name {ctx}` text a click produces, so
  **tools and the instruction don't change**. Keep it short for phone screens: at most 5 options and
  no tables.
- **E2. Mode selection (P1, S).** Per session: auto-detect from E0 if possible. Otherwise, the first card asks
  "On your phone? Reply 1 for a text version" (a button on desktop, and harmless as text), and
  "text mode" / "rich mode" switch at any time. Store the mode in session state.
- **E3. Packaging for option B, only if E0 requires it (M).** `app/runtime_app.py` with `AdkApp` wrapping
  the same `root_agent` (text mode forced), reading the ServiceNow token from the GE authorization state
  key into the same `servicenow.user_token` ContextVar through a `before_agent_callback`. Deploy with
  `agents-cli deploy` to the existing engine or a new one, and register it in GE with its own authorization.
- **E4. Tests (P1, S).** Text rendering snapshots per card type, and "2" → action mapping, including stale
  numbers after a new card.

### Open questions for discussion
1. Is one GE listing a hard requirement? It rules out B unless auto-routing is possible.
2. Is photo upload on mobile needed for launch? Text mode still works with a typed tag or serial.
3. Should desktop users be able to pick text mode (accessibility, screen readers)? E2 allows it.
