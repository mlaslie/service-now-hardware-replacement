# Backlog

Created 2026-09-26. The agent works end to end; this is the next round of work.
Priority: **P0** = bug or blocks the next manual test, **P1** = next up, **P2** = later.
Size: S (< half a day), M (about a day), L (several days).

Status 2026-10-01. **Done:** A0, A1, A2 (evals), A3, B, E (mobile text mode), F (hospital equipment), G1.1,
G1.3, G2.1-G2.3, G2.6-G2.8, G3.1, G3.5, most of G3.7, A2UI v0.9 + green buttons, verbatim saved addresses,
custom role `u_hardware_requester` (created and measured: passes everything `itil` does), README/HTML overview,
generic configurable demo users.
**Next:** H0 (review fixes, below), then G3.3 (wording in `messages.yaml`), G1.2 (split `tools.py`), G3.12 (customization recipes), F1 (live
pass), rest of G3.7 (ticket table / catalog item), then D ideas. Section C is folded into G2.
---

## H. Code review findings (2026-10-02)

A read-only review of the whole codebase (four parallel reviewers, the top findings re-checked by hand).
Live revision `00030` at review time. Numbers match the review.

### H0. Correctness and security (P0): **fixed 2026-10-02, tests added; deployed (revision 00031), committed `4b74c9a`.
H0.6 also needs the create script re-run in ServiceNow (elevated).**
- [x] **H0.1 Typed address swapped for a saved one** (`app/tools.py` `_canonical_place`/`_match_saved`): labels and
  aliases match as substrings, so "500 Warehouse Ave" -> Home ("house"), "1 Network Way" -> office ("work").
  Match whole words, and never let a label match override text that is already a street address.
- [x] **H0.2 Encoded-query injection** (`app/servicenow.py` `my_incident`, `find_asset` serial): ticket numbers and
  serials go into `sysparm_query` unescaped; `INC1^NQnumber=INC2` escapes the "mine" filter. Validate numbers,
  filter serials like asset tags.
- [x] **H0.3 Followers can change status of others' tickets** (`update_ticket` -> `_state_change`): any of the six
  states is accepted; only `cancel_ticket` checks the reporter. Allow status/urgency/ship-to changes on own
  tickets only, and only the four documented statuses.
- [x] **H0.4 Mobile text empty on a v0.8 registration** (`app/agent.py` `render_staged_card`): `to_text` runs on
  the v0.8 messages, which it can't read. Render text from the v0.9 messages.
- [x] **H0.5 Error reported after the ticket was filed** (`submit_ticket`): follow-up notes run before
  `submitted_number` is saved. Save the number right after create; make follow-up notes best-effort.
- [x] **H0.6 Custom role: watch list rule too broad** (`scripts/servicenow/create_hardware_requester_role.js`):
  anyone with the role can replace the whole watch list of any open hardware ticket, and being on it grants read.
  Limit to: own tickets, or open tickets on equipment (`cmdb_ci` set), and require the change to keep existing
  entries. Needs a re-run of the background script by an admin.

### H1. Robustness (P1): **done 2026-10-03** (tests for each; `f03bbb2` and next commit)
- [x] H1.1 ServiceNow timeouts/connection errors escape every soft-fail handler; HTML error pages read as
  "Hibernating" before the status is checked (`servicenow._request`).
- [x] H1.2 A transient session-read failure re-asks the desktop/mobile question mid-conversation (`server.py`).
- [x] H1.3 Answers like "I'm on desktop", "1 - mobile", "1)" loop the display question (`inbound.py`).
- [x] H1.4 Stale numbered options: a later bare "1" can trigger an old button (clear options on turns without a card).
- [x] H1.5 Photo decode/upload failure fails the whole turn and loses the text (`inbound.py`, `server.py`).
- [x] H1.6 Editing after submit edits only the local draft (`update_request`, `choose_ship_to`, ...): point to `update_ticket`.
- [x] H1.7 A later photo without visible damage overwrites earlier damage evidence (`analyze_photos`).
- [x] H1.8 Profile values that validate but crash at submit: positional `{}` in `ticket_fields`, unknown
  placeholders in `recommendations`.
- [x] H1.9 `seed reset` deletes all tickets of pre-existing users the seed only updated; filter by the seed's tickets.
- [x] H1.10 `remove_hardware_requester_role.js` ignores delete results and doesn't check elevation (done with H0.6: verifies each delete, keeps the role if a rule remains, removes group grants).
- [x] H1.11 Concurrent submits can file two tickets (no lock between the "already submitted" check and create).
- [x] H1.12 Old device's photo warnings/evidence carry over after the device changes (`_set_device`).
- [x] H1.13 Ship-to change on a ticket without a Ship-to line (equipment, or description dropped) is reported as
  "ServiceNow policy", and the note says "changed" even when it wasn't.

### H2. Lower priority (P2): **done 2026-10-03 except the decision below**
Truncation: ticket list says when there are more; group memberships read up to 500; device list stays at 20.
- [x] "Suite 200" treated as a temporary address (office addresses never saved).
- [x] `normalize_address` drops non-ASCII letters (non-Latin addresses collide).
- [x] Memory recall slices before filtering out delivery memories.
- [x] Device/group/ticket lists truncate silently (20/50/10).
- [x] Notes fallback sorted on display strings.
- [x] ServiceNow text not markdown-escaped in mobile text mode (links/images in notes render).
- [x] JWT-shaped ServiceNow access tokens rejected by `identity.bearer()` (check `iss`, not dots).
- [x] Photo-label device match skips the "is this the right device?" confirmation. **Kept by design:** an exact
  asset-tag or serial match from inventory goes straight on; the review card shows model, tag and serial before
  submit, and a fuzzy match (model only) still asks. Fewer steps on the happy path (D1).
- [x] Same issue key in both groups: equipment settings ignored.
- [x] Logs: user email and full request metadata at INFO.
- [x] One shared `httpx.AsyncClient`; evict `_user_cache`.
- [x] Container runs as root (add `USER`).
- [x] `sn_doctor`: temp user created outside `try`; cleanup deletes unchecked; end impersonation explicitly.
- [x] `sn_seed` token cache briefly world-readable; `chat.py` takes tokens as CLI args.
- [ ] **Needs your decision.** Personal identifiers in tracked docs (`HANDOFF.md`, `BACKLOG.md`, `CLAUDE.md`, one screenshot) and in
  git history of `demo/DEMO.html` (needs a history rewrite to remove; repo is private).

### H3. Test gaps (P1)
- [ ] `server.preprocess`/`to_run_request`: token never in state, identity refreshed per turn, display-mode handling.
- [ ] `sn_doctor` negative checks: writes to other fields on others' tickets; reading non-hardware incidents.
- [ ] `memory` save/delete of addresses; `seed reset` with pre-existing users.

---

## G. Adoption kit: easy to understand, install and customize

Goal: another organization can read how the agent works in an hour, install it on their own
Google Cloud and ServiceNow in an afternoon, and adapt it to their fields, wording and colours
**without editing Python**.

Today the things an organization would change are spread across code:

| What | Where it lives today |
|---|---|
| Brand colour, issue lists, safety wording, step count | `app/cards.py` constants |
| Photo rules, urgency rules, response targets, refresh ages, temporary-place words | `app/tools.py` constants (`PHOTO_POLICY`, `_PRIORITY`, `_EQUIPMENT_SLA`, `REFRESH_YEARS`, `_TEMPORARY`...) |
| ServiceNow category, states, priority matrix, laptop model names | `app/servicenow.py` constants |
| Persona ("hospital staff"), tone, routing rules | `app/agent.py` `INSTRUCTION` |
| Desktop/mobile question and its answers | `app/agent.py` `ASK_TEXT`, `app/inbound.py` word lists |
| Agent name, description, examples | `app/server.py` `build_agent_card` |
| Project, engine, instance defaults (ours) | `app/config.py`, `scripts/deploy.sh` |

### G1. Understand: documentation and structure (P1)

| ID | Item | Size |
|---|---|---|
| G1.1 | **Done 2026-09-30.** **`docs/ARCHITECTURE.md` with diagrams** (Mermaid, render on GitHub): (a) components: Gemini Enterprise → Cloud Run → ServiceNow / Agent Runtime / GCS / Gemini; (b) one turn, sequence: identity, photos, display mode, model, tools, card; (c) the request flow as a state diagram: device → confirm → already reported? → problem → photo → review → submit; (d) ticket changes: attempt → read back → note what was refused → tell the user; (e) sign-in: GE authorization → token pass-through → ServiceNow ACLs. | M |
| G1.2 | **Split `app/tools.py` (1,300 lines)** into a package: `wizard/` (steps), `devices.py` (lookup, matching, ownership), `addresses.py`, `tickets.py` (list/get/update/follow), `policy.py` (urgency, response targets, photo rules). No behaviour change; tests stay green. | M |
| G1.3 | **Done (module map in ARCHITECTURE.md and the HTML docs).** **Module guide**: a one-screen map in the README ("to change X, look in Y") plus a docstring at the top of every module saying what it owns. | S |
| G1.4 | **Decision records** (`docs/decisions/`), one page each: Cloud Run not Agent Runtime (identity), per-user ServiceNow OAuth, deterministic cards (model never writes UI JSON), A2UI v0.9 with v0.8 fallback, the desktop/mobile question, verbatim saved addresses, "attempt, verify, note". Most of the reasoning is already in HANDOFF; this makes it findable. | S |
| G1.5 | **Glossary**: draft, card, surface, device kind, relation, follower, correlation id, display mode, CI. | S |
| G1.6 | **User guide** (1 page, for end users and help desks): what to say, what the buttons do, how to get updates, mobile vs desktop. Could double as the GE agent description. | S |
| G1.7 | **Troubleshooting guide for operators**, from the HANDOFF problems table rewritten as symptom → cause → fix, plus the log queries that find each. | S |

### G2. Install: from zero to a working agent (P1)

| ID | Item | Size |
|---|---|---|
| G2.1 | **Done, plus `docs/site/index.html` (fill-in values, copy buttons, checked links).** **`docs/INSTALL.md`**: prerequisites → ServiceNow setup → Google Cloud setup → deploy → register in Gemini Enterprise → smoke test → optional demo data. Every step with the exact command or screen, and how to verify it worked. | M |
| G2.2 | **Done: `.env` / `.env.example`, `config.require()`, our defaults removed.** **One settings file** (`.env` from `.env.example`, or `config/deployment.yaml`) with every value explained: project, region, service name, ServiceNow instance, model, locations. Remove our defaults (`PROJECT_ID`, engine id, `INSTANCE`) from code; fail at startup with a clear message when something is missing. (Was C1.) | S |
| G2.3 | **Done: `scripts/setup.sh` + `scripts/create_state_engine.py` (tested: created and deleted an instance).** **`scripts/setup.sh`** (idempotent): enable APIs, create the bucket, runtime service account and roles, the code-less Agent Runtime engine for sessions and memory, and print what to put in the settings file. (Was C2 + C3; the engine is hand-made today.) | M |
| G2.4 | **Deploy options, documented side by side**: `scripts/deploy.sh` (gcloud, supported), plain `gcloud run deploy` for people who want to see it, and why `adk deploy cloud_run` / `agents-cli deploy` don't fit (custom A2A server with identity pass-through). Optional Terraform module for organizations that require IaC. | S/M |
| G2.5 | **Gemini Enterprise registration**: a script that prints the agent card JSON and the authorization values (auth URL, token URL, scope) ready to paste, and optionally registers the agent + authorization through the Discovery Engine API. Document the gotchas (re-add to change the card, one authorization per agent, never authorize as admin). (Was C5.) | M |
| G2.6 | **Done (INSTALL step 2; screenshots still to add).** **ServiceNow setup guide** (was C4): OAuth client in Application Registry (authorization code, the two GE redirect URLs), what the connector client is, the seed-script client, how to test sign-in. Screenshots. | S |
| G2.7 | **Done: `docs/ROLES.md`, measured on INSTANCE.** **Role matrix, measured, not guessed.** For each thing the agent does (read own profile, list own devices, read department equipment, read group memberships, create incident, set description / urgency / assignment group / location / watch list, add comment, change state, read journal, attach photo, read other people's open tickets on a CI), record the minimum role and ACL. Two personas: **admin** (one-time setup: OAuth client, groups, categories, seed) and **requester** (day to day). Document what degrades gracefully without each role. | M |
| G2.8 | **Done: `scripts/sn_doctor.py` (impersonation; `--as`, `--matrix`, `--read-only`).** **`sn_doctor` permission checker**: `python scripts/sn_doctor.py --as <user>` signs in as a test user and tries each operation from G2.7 (creating and deleting a test incident), then prints a pass/fail table with the fix for each failure. Turns "why is urgency ignored?" into a one-minute check. | M |
| G2.9 | **Post-deploy smoke test**: `scripts/smoke.sh` fetches the agent card, checks the declared A2UI versions, and runs a scripted conversation through `scripts/chat.py` (no submit). | S |
| G2.10 | **Upgrade notes / CHANGELOG** with a line per release that needs action (e.g. "re-add the agent: agent card changed"). | S |

### G3. Customize without code (P1)

One **organization profile**, `config/organization.yaml` (validated at startup with a schema, with
clear errors), read by the agent instead of the constants above. A sample profile for "generic
office" and one for "hospital" (today's behaviour).

| ID | Item | Size |
|---|---|---|
| G3.1 | **Done: `app/profile.py`, `config/organization.yaml`, office example, 15 tests.** **Profile loader + schema** (pydantic): load, validate, show the effective config in the logs at startup, and a `--check` mode. Defaults reproduce today's behaviour exactly. | M |
| G3.2 | **Branding**: agent display name, description and examples (agent card), brand colour (`theme.primaryColor`; document the measured limits: only primary buttons take it, secondary stay grey, GE controls fonts, `MaterialButton` ignores it), which buttons are primary, optional `iconUrl` / `agentDisplayName` theme fields (untested). | S |
| G3.3 | **Wording**: every user-facing string (card titles, subtitles, button labels, the desktop/mobile question and accepted answers, safety text, confirmation text) in `config/messages.yaml`. Keys, not Python. Opens the door to G5.1 (languages). | M |
| G3.4 | **Problem choices**: per device kind, the list of issue options, each with its label, the ServiceNow value it writes (category / subcategory / custom field), whether a photo is required / recommended / not asked, what to photograph, and a minimum urgency (e.g. safety concern = critical). Replaces `ISSUE_CATEGORIES`, `EQUIPMENT_ISSUES`, `PHOTO_POLICY`, `_BLOCKING`. | M |
| G3.5 | **Done via option B + import: `scripts/sn_profile.py import-issues` / `check`; no run-time reads.** **Use the organization's own ServiceNow choice lists.** Option A: `source: servicenow` reads `sys_choice` for a field (e.g. `incident.subcategory` where `dependent_value=hardware`, or a custom `u_hardware_type`) and shows exactly those, with their labels and order, cached. Option B: a static list in the profile, each mapped to a choice value. Either way the agent only ever writes values that exist; `--check` flags profile values missing from ServiceNow. | M |
| G3.6 | **Device types from ServiceNow**: map model categories to device kinds (personal / shared / clinical) and to the organization's hardware-type values, instead of the laptop-name heuristics and `CLINICAL_CATEGORIES`. | S |
| G3.7 | **Partly done: `ticket_fields` (templated), `device_values`, `urgency_matrix`, refused fields noted; table / catalog item and state codes still fixed.** **Ticket mapping**: which table (incident today; option for a Service Catalog item / `sc_req_item`, see D4), which category, which fields get description, urgency/impact, CI, location, assignment group, correlation id; the state labels and codes; the priority matrix (urgency × impact → label). Organizations with custom fields add them here (e.g. `u_cost_center`, `u_building`). | M |
| G3.8 | **Service rules**: response targets per priority, refresh ages per device type, recommendation texts, "who handles it" fallback group, whether requesters may change urgency / reopen / cancel (or only request it via a note). | S |
| G3.9 | **Feature switches**: equipment reporting, photo analysis, followers / duplicate detection, saved addresses, memory, the desktop/mobile question (off for web-only organizations), safety concern category, ownership check. | S |
| G3.10 | **Persona and tone**: industry (hospital / office / university / field service), audience, tone, and extra rules appended to the instruction from the profile, with the tool-routing part of the instruction kept in code so customizations can't break the flow. | S |
| G3.11 | **Demo data per organization**: the seed files (`users.json`, `catalog.json`, `equipment.json`) documented as templates, with a generic-office example next to the hospital one. | S |
| G3.12 | **Customization guide** (`docs/CUSTOMIZE.md`): one recipe per common change, e.g. "show only our hardware types", "change the button colour", "add a problem type with a required photo", "route printers to the Print team", "turn off equipment reporting", each with the YAML snippet and how to verify. | M |

### G4. Operate and trust (P2)

| ID | Item | Size |
|---|---|---|
| G4.1 | **Privacy and security notes**, especially for hospitals: photos may capture patients or screens with patient data (add a reminder on the photo step; optional face/PHI blur); photo retention (GCS lifecycle rule, attachment copied to ServiceNow); what is stored where (sessions, memory, saved addresses) and how to delete a person's data; tokens never logged; VPC-SC / CMEK / data residency options. | M |
| G4.2 | **Observability**: a Cloud Logging dashboard (turns, tickets filed, paths taken (D10), errors, ServiceNow refusals, model latency) and alerting on error rate and ServiceNow sign-in failures. | M |
| G4.3 | **Cost guide**: per-ticket cost of model calls, vision, Cloud Run min-instance, Agent Runtime sessions/memory; knobs to reduce it. | S |
| G4.4 | **Partly done: `evals/run.py` runs against whatever profile is loaded.** **Regression safety for customizations**: evals (A2) run against the organization's profile; `pytest` fixtures that load any profile, so a customization that breaks the flow fails in CI. | M |
| G4.5 | **Support runbook**: rotate the OAuth client secret, re-register after a card change, instance hibernation, user reports "wrong person", clearing a stuck conversation. | S |

### G5. Brainstorm: other customizations organizations will ask for (P2, pick later)

- **G5.1 Languages**: messages per locale from G3.3; detect from the Gemini Enterprise request or the ServiceNow user's language.
- **G5.2 Approvals**: manager approval above a cost or for non-warranty replacements (catalog item approvals, D4).
- **G5.3 Loaners and pickup**: offer a loaner for blocking issues (D3); courier pickup / return label (D7).
- **G5.4 Locations and delivery rules**: ship only to company sites, or allow home delivery per policy; pick from `cmn_location` instead of free text.
- **G5.5 Assignment**: use ServiceNow assignment rules / data lookup instead of setting the group, or a per-device-type routing table in the profile.
- **G5.6 Self-help first**: per problem type, 2–3 checks before filing (D6), from a knowledge base article.
- **G5.7 Other ticket systems**: keep the ServiceNow client behind an interface so Jira Service Management or Freshservice could be added later.
- **G5.8 Accessibility**: text mode as a user choice for screen readers (E open question), plain-language review.
- **G5.9 Multiple organizations / business units** from one deployment: profile chosen by the user's company or domain.
- **G5.10 Branding beyond colour**: agent icon in Gemini Enterprise, custom greeting, sign-off text.

### Status (2026-09-30)
Done: G1.1, G1.3, G2.1, G2.2, G2.3, G2.6 (text), G2.7, G2.8, G3.1, G3.5, most of G3.7, A2. Custom role
`u_hardware_requester` scripted (docs/ROLES.md), waiting to be created in ServiceNow and measured. Already covered by the profile: the colour and
agent card text (G3.2), problem choices with photo and urgency rules (G3.4, static lists), device categories (part of
G3.6), service texts and response targets (G3.8), persona (G3.10). Measured finding: `itil` alone covers every
feature; with no roles, requesters can still file (details go in a note) but can't change, follow or be matched to
open tickets; `itil` is a licensed role (see ROLES.md).

### Suggested order inside G
1. G2.2 settings file and G3.1 profile loader: everything else builds on them.
2. G1.1 architecture diagrams and G2.1 install guide: the first things a new organization reads.
3. G2.7 role matrix and G2.8 `sn_doctor`: the most common adoption blocker is ServiceNow permissions.
4. G3.2–G3.5: branding, wording, problem choices and ServiceNow choice lists (your examples).
5. G1.2 split `tools.py`, then G3.12 customization guide with recipes.
6. The rest as organizations ask.

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
| 9 | Photo of maker label with model/part number but **no readable serial** | Auto-picks the one owned device with that model, with a confirm note (A3); otherwise the device picker |
| 10 | Photo of damage with sticker visible | Device and evidence both filled in one step → review |
| 11 | Photo of damage, no label, device already selected | Evidence only; make/type mismatch check |
| 12 | Photo of damage, no label, nothing selected | Findings card with the user's devices |
| 13 | Label serial ≠ inventory serial for that tag | Serial-mismatch warning |
| 14 | Photo shows a different owned device than selected | A0.4 behaviour |
| 15 | Unrelated or unreadable photo | Friendly retake message, draft unchanged |
| 16 | Two photos in one message (label + damage) | Both applied; device from the label, evidence from the damage |
| 17 | Photo first, before any request | Fresh draft (A0.2) |
| 18 | Serial with OCR-confusable characters (`0/O`, `1/I`, `5/S`) or Apple's leading `S` | Fuzzy match against the user's own devices only, with a confirm note (A3) |
| 19 | Device not in inventory at all (read from the photo) | `in_inventory=False`, warning, ticket filed without `cmdb_ci` |
| 20 | Second request in the same conversation | New ticket (A0.1) |

### A2. Model-routing evals (P1, M): **done, `evals/run.py` + `evals/cases.yaml` (12 cases, real model, fake ServiceNow)**
The table above tests the tools; whether the **model** calls the right tool from free text needs
evals. Use `agents-cli eval` with an evalset of about 15 one-line openers ("asset 123456 won't turn on",
"S/N FCPJ2GJTHC cracked screen, demo tomorrow", "here's the sticker" + photo marker,
"file it", "show my tickets", "the tracking number doesn't work, reopen it"). Assert the tool
trajectory, and assert that no tool is called that the user didn't ask for (e.g. no submit without consent).

### A3. Matching improvements (P1, S): **done (revision 00021)**

`tools.match_own_asset`, used for typed values and photos when the exact lookup fails. Only the user's own devices are considered, a single candidate is required, and a note on the review card asks the user to confirm. `ASSET_TAG_HINT` in config replaces the hard-coded tag format. Tests are in `tests/test_paths.py`.

- If an exact lookup fails, fall back to the user's **own** assets. Match by model or part number
  when exactly one fits, and by serial with edit distance ≤ 1 after mapping confusable characters. Always confirm
  on the review card ("Matched to your MacBook Air, serial FCPJ2GJTHC").
- Say in the vision prompt that Apple labels print "Serial (S)" and that barcodes can't be decoded, so
  only printed text counts.
- Remove the hard-coded "IT-#####" hint; read the tag format from config.

---

## B. Demo script with the two test users (P1, S): **done, rewritten for the hospital in `demo/DEMO.html`**

The plan below was the first version; the hospital demo (section F) replaced it.

Write `demo/DEMO-2-USERS.md` (keep `demo/DEMO.html` for the one-user flow). Prerequisites:
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

## C. Replicate in someone else's Google Cloud (P1, M): **folded into G2**

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
- **D9.** Warn on duplicates: **done for shared and clinical equipment** (F). Still open for personal devices
  (e.g. a second ticket for the same laptop by the same person).

**Quality and operations**
- **D10.** Structured logging of each path (intake source: typed/photo-label/photo-damage/picker)
  to measure which paths users actually take.
- **D11.** Admin/system-account guard: if the resolved ServiceNow user has the `admin` role, warn
  (problem 12 in the handoff).
- **D12.** Cost: skip the vision call when the photo is a duplicate (same hash) within a request.

---

## F. Hospital equipment (built 2026-09-26, revision 00023)

Goal: anyone in the hospital reports broken shared or medical equipment as easily as their own
laptop, the report reaches the team that fixes it, and everyone affected gets updates.

**How hospitals usually keep equipment in ServiceNow, and what the agent assumes**
- Equipment is an asset (`alm_hardware` by default; `ASSET_TABLES` adds e.g. a clinical device table),
  **owned by a department** (`department`), at a **room-level location** (`location`), **supported by a
  group** (`support_group`): Clinical Engineering / biomed for patient-care devices, Imaging Engineering
  (or the vendor) for imaging, the IT Service Desk for IT equipment in clinical areas.
- Asset tags are Clinical Engineering control numbers (CE-#####) on a sticker with a barcode.
- Medical equipment is recognized by model category (`CLINICAL_CATEGORIES`); other unassigned
  department equipment is "shared"; anything assigned to a person is "personal".

**Built**
| Item | Where |
|---|---|
| Device list shows model, tag and serial; every chosen, typed, described or fuzzy-matched device gets a yes/no "Is this the right device?"; an exact photo match skips it | `cards.device_picker`, `cards.confirm_device`, `tools.confirm_device` |
| Find equipment by description ("the MRI in room 104", "infusion pump in ED bay 7"): own devices and department equipment first, then the whole inventory | `tools.find_device`, `servicenow.department_assets`, `servicenow.search_assets` |
| Ownership, checked but never enforced: yours / your department / your group supports it / could not be confirmed (noted on the ticket) | `tools._relation` |
| Equipment problems (not working, error or alarm, damaged, safety concern, other); safety concern is always critical, no photo step, and tells the reporter to take it out of service | `cards.EQUIPMENT_ISSUES`, `tools._refresh` |
| Repair, not replacement: on-site repair by the support group, the equipment's location, bill to its cost center | `cards.review`, `tools._ticket_description` |
| Ticket: assignment group = support group, location, CI, watch list = the device's owner/manager; comments if ownership is unconfirmed or routing was refused | `tools.submit_ticket` |
| Already reported: open ticket on the same CI → "Add my note" makes the reporter a follower (watch list); followers see, get and update the ticket, only the reporter can cancel | `tools.follow_ticket`, `servicenow.follow_incident`, `_mine` |
| CI lookup through the CI's `asset` reference when ServiceNow keeps the asset's `ci` empty | `servicenow._link_cis` |
| Seed: departments, rooms, support groups, 8 pieces of equipment with CIs, Jane in Radiology, John in IT and Service Desk; `clear-tickets` between demo runs; report has an equipment page | `seed/equipment.json`, `seed/sn_seed.py` |
| Demo: 6 scenes with both users, phone and desktop, tag stickers `demo/tag-mri.jpg`, `demo/tag-pump.jpg` | `demo/DEMO.html` |
| Tests: 13 equipment path tests (plus the confirm step across the intake tests), 3 seed tests | `tests/test_paths.py`, `tests/test_sn_seed.py` |

**F-next (not built yet)**
- **F1 (P1, S). Live test pass** of `demo/DEMO.html` on phone and desktop; tune the instruction where the
  model routes wrong (e.g. uses select_device for a description), and add those phrases to the A2 evals.
- **F2 (P1, M). Production access.** Staff without `itil`/`asset` usually can't read other departments'
  assets, group memberships or other people's incidents, and may not be allowed to set assignment group,
  location or watch list. Needs a scripted REST API (search equipment, open tickets on a CI, follow) or a
  Service Catalog "Report equipment problem" item that sets these server-side (see D4/D5). The agent already
  degrades gracefully: unreadable data is skipped, refused fields are noted on the ticket.
- **F3 (P2, S). Take out of service.** For a safety concern, offer to set the asset's status to
  "In maintenance" so the next person sees it's tagged out (needs asset write access).
- **F4 (P2, S). Vendor-serviced equipment.** Imaging often has a vendor service contract; show the vendor and
  contract number on the review card and ticket (`ast_contract` / `service_contract` on the asset).
- **F5 (P2, S). Safety event reporting.** Link to the hospital's safety event system (or open the event)
  when a patient or staff member was harmed; the agent only reminds today.
- **F6 (P2, S). Configurable CI class.** Hospitals with a clinical device plugin keep CIs in their own class;
  confirm `_link_cis` and `ASSET_TABLES` cover it on a real instance.
- **F7 (P2, S). Unfollow.** "Stop following INC…" removes the user from the watch list.

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

### Measured 2026-09-30: A2UI v0.9 and client capabilities (throwaway agent `a2ui-v09-probe`)
Tested because a note claimed the mobile app renders the A2UI v0.9 basic catalog and that clients
advertise different catalogs (`a2uiClientCapabilities`), which would allow automatic detection. **Neither holds:**
- An agent declaring v0.9 **and** v0.8 gets v0.9 requested. **Web and mobile both advertise** the v0.9 basic
  catalog **and** the Gemini Enterprise composite catalog, with identical headers. Nothing tells them apart.
  (Our v0.8-only agent sees the v0.8 basic catalog from both: Gemini Enterprise echoes what the agent declares.)
- **Mobile shows the red "unsupported content" box for v0.9 too.** Web renders v0.9 fully.
- A v0.9 click arrives as a DataPart `{"action": {"name", "context": {..}, "sourceComponentId", "surfaceId",
  "timestamp"}}` plus a text part "User action triggered.". Useful if the web cards ever move to v0.9 (v0.8 is
  deprecated upstream); it would not help mobile.
- **Decision: keep the first-turn "Desktop or Mobile App?" question.** Re-test when the mobile app changes.

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
