# Handoff: ServiceNow Hardware Replacement agent

Background for anyone (or any Claude session) picking this project up. It records what was
built, where everything lives, what went wrong and how it was fixed, and what's still open.
Last updated 2026-10-05. `main` at `0751e01` or later (all pushed); live Cloud Run revision `00033` (deployed 2026-10-05 from `f730e3b`: R1-R4; smoke test passed).

**No secret values are in this file.** Secrets are named with where they live.

---

## 1. What this is

An ADK agent that lets an employee replace broken work hardware from Gemini Enterprise in a
few taps. It runs on **Cloud Run**, is served to a **Gemini Enterprise** app over **A2A**, draws
**A2UI v0.9** cards (buttons; v0.8 when a client negotiates it), reads photos with **gemini-3.8-flash**, and files and manages
**ServiceNow incidents as the signed-in employee**, using their own ServiceNow OAuth token.

Status (2026-10-01): working end to end in Gemini Enterprise (web: A2UI v0.9 cards with green primary
buttons; mobile: numbered text after a "Desktop or Mobile App?" question) for `john.doe` (itil) and
`jane.doe` (custom role `u_hardware_requester`, no itil). Covers personal devices and the demo hospital
"Riverside Medical Center"'s shared/clinical equipment. Behaviour per organization is in
`config/organization.yaml`; settings in `.env`. Docs: README (overview), `docs/INSTALL.md`,
`CONFIGURATION.md`, `ROLES.md`, `ARCHITECTURE.md`, `docs/site/index.html`. Tests: 184 unit/path tests +
12 model-routing evals (`evals/run.py`).

### History (milestones)
| Date | Milestone |
|---|---|
| 09-25 | First version: Cloud Run A2A agent, A2UI v0.8 wizard, photo reading, Firestore mock tickets |
| 09-25/26 | ServiceNow per-user OAuth (Table API as the user); Agent Runtime used only for Sessions + Memory Bank; read-back "attempt, verify, note" rule; compact ticket views |
| 09-26 | Seed tool (users, kit, PDF, reset); handoff/backlog; mobile text mode (desktop/mobile question, numbered replies) |
| 09-26 | Intake bugs fixed + 31 path tests; fuzzy matching against own devices |
| 09-26 | Hospital equipment: device kinds, confirm step, find by description, ownership check, routing, followers, safety concern; seed equipment; `demo/DEMO.html` run sheet |
| 09-30 | A2UI v0.9 (v0.8 fallback), green primary buttons (theme.primaryColor); throwaway `a2ui-v09-probe` showed mobile can't be detected and renders no A2UI |
| 09-30 | Verbatim saved delivery addresses (no hotels; never applied on "looks good") |
| 09-30 | Adoption kit: `.env` settings, organization profile, setup.sh, docs + HTML site, `sn_doctor` role matrix |
| 10-01 | README overview rewritten (also in the HTML docs); demo users made generic and configurable (Jane/John Doe, `demo_role`, `demo/build_demo.py`); agent-card updates by editing the agent in GE (no re-add) |
| 10-01 | Ticket field mapping + `sn_profile.py`; model-routing evals (found and fixed a dropped-problem bug); custom role `u_hardware_requester` created and measured; Jane moved to it; registration screenshots; README overview rewritten |
| 10-02 | Full code review (4 parallel reviewers); backlog section H; H0 fixes deployed (revision 00031): substring address swap, query injection via ticket numbers/serials, followers changing others' tickets, empty mobile text on v0.8, error after filing, follower-list rule in the custom role |
| 10-05 | Personal data removed (local `*.local.md` files, history rewritten), repo made public; review fixes R1-R4 (revision 00033); fuzzing round 2 (T9-T11): 119/119 and 84/84 |
| 10-03 | Fuzzing: `fuzz/` (Hypothesis + 84 model conversations), 11 defects found and fixed, report published as a private artifact; evals 14/14 |
| 10-03 | H1-H3 robustness and tests; `app/tools/` package; card wording in `config/messages.yaml`; profile `features` + `requester_changes`; D1, D6, D8-D12, F5, F7; docs set (decisions, user guide, runbook, privacy, costs, troubleshooting, customize, changelog); `scripts/smoke.py`, `print_registration.py`, `ops/observability.sh`; fuzzing harness (`fuzz/`) |

---

## 2. Where things live

### Code and GitHub
| Item | Value |
|---|---|
| Local path | `~/ADK/hardware_replacement` |
| GitHub | https://github.com/OWNER/service-now-hardware-replacement (**public** since 2026-10-05; history rewritten to remove personal data), branch `main` |
| Push | `git push` from the repo (remote `origin` is set; `gh` CLI is logged in as `OWNER`) |
| Commit identity | Repo-local config: `John Doe <23639657+OWNER@users.noreply.github.com>`. There's no global git identity on this Mac, so keep committing from this repo, or set it again with `git config user.name` / `user.email`. |
| Attribution | **No Claude / Anthropic co-author trailers or "Generated with" footers**, per the user's global `~/.claude/CLAUDE.md`. |

### Tools on this Mac
- `gcloud` is **not on PATH**: use `~/google-cloud-sdk/bin/gcloud`, or
  `export PATH=~/google-cloud-sdk/bin:$PATH`.
- Account `john.doe@example.com`. The gcloud default project config is
  `PROJECT_ID-01`, but **this project uses `PROJECT_ID`**, so always pass `--project`.
- `uv` for Python (3.12). `agents-cli` 1.7.0 (`uv tool upgrade google-agents-cli`). Versions
  before 1.x wrongly refuse A2A on Agent Runtime.

### Google Cloud (`PROJECT_ID`, number `PROJECT_NUMBER`, region `us-central1`)
| Resource | Name / ID | Notes |
|---|---|---|
| Cloud Run service | `hardware-replacement-agent` | URL `https://hardware-replacement-agent-PROJECT_NUMBER.us-central1.run.app` (the card advertises this form). Latest revision `00033`. `--no-allow-unauthenticated`, min 1 instance, concurrency 4, 1 GiB. |
| Runtime service account | `hardware-agent@PROJECT_ID.iam.gserviceaccount.com` | `aiplatform.user`, `logging.logWriter`, `storage.objectUser` on the bucket |
| Cloud Run invoker | `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com` | `run.invoker` on the service only. This is how Gemini Enterprise calls it. |
| Agent Runtime "state engine" | `projects/PROJECT_NUMBER/locations/us-central1/reasoningEngines/ENGINE_ID` (`hardware-replacement-state`) | **Runs no code.** Hosts managed Sessions + Memory Bank (topics: USER_PREFERENCES, delivery_and_contact, hardware_history). |
| Artifact bucket | `gs://PROJECT_ID-hardware-ticket-photos` | Photos via ADK `GcsArtifactService`; private, uniform access |
| Model | `gemini-3.8-flash`, location `global` | Chat and vision |
| Firestore DB `hardware-tickets` | **unused leftover** | From the first (mock) version. Safe to delete once the user names it explicitly. |

### Secrets (Secret Manager, `PROJECT_ID`)
| Secret | Format | Used by | Status |
|---|---|---|---|
| `servicenow-seed-oauth` | `instance\|client_id\|client_secret` | `seed/sn_seed.py` (authorization-code client, redirect `http://localhost:8765/callback`) | **active** |
| `servicenow-integration` | `instance\|user\|password` | nothing | leftover from the failed basic-auth attempt; delete when the user names it |
| `servicenow-oauth` | `instance\|client_id\|client_secret` | nothing | leftover from the failed client-credentials attempt; delete when the user names it |

- The **agent itself uses no secret**: it receives each user's ServiceNow token from Gemini Enterprise.
- The seed script caches its admin token in `~/.config/hw-seed/token.json` (0600), outside the repo.
- The default compute service account has project-wide `secretAccessor`; `hardware-agent` does not need it.
- Deleting secrets or databases needs the user to **name them explicitly**; the permission
  classifier blocks vague "clean up" requests.

### Gemini Enterprise
| Item | Value |
|---|---|
| App | `2H-2026` = `projects/PROJECT_NUMBER/locations/global/collections/default_collection/engines/GE_APP_ID` |
| Agent | "Hardware Replacement", registered as **A2A** from the card at `<service URL>/.well-known/agent-card.json`. Re-added on 2026-09-30 for A2UI v0.9 (old ID `15007067269672713406` is gone); IDs change on every re-add, so look them up (below). |
| Authorization | ServiceNow OAuth (authorization code), a new resource since the re-add (old `hardware-replacement_1790341756507`). Built from the **ServiceNow connector's own OAuth client** (redirects include `https://vertexaisearch.cloud.google.com/oauth-redirect`). Scope `useraccount`; auth URL `https://INSTANCE.service-now.com/oauth_auth.do?response_type=code&client_id=<ID>&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=useraccount`; token URL `https://INSTANCE.service-now.com/oauth_token.do`. |
| Other | The ServiceNow connector `service-now-INSTANCE_1789764864639` (federated + actions). Old Cloud Run probe agent `a2a_probe` may still be registered. **"A2UI v0.9 Probe"** (Cloud Run `a2ui-v09-probe`, code `~/ADK/a2ui-v09-probe`, no auth): kept on purpose for re-testing mobile/colour; the user may have it registered. |
| Look up current IDs | `GET https://discoveryengine.googleapis.com/v1alpha/projects/PROJECT_NUMBER/locations/global/collections/default_collection/engines/GE_APP_ID/assistants/default_assistant/agents` (header `x-goog-user-project: PROJECT_ID`) |

Each authorization resource can be attached to **one** agent only. In the UI, changing an
agent's authorization means deleting and re-adding the agent.

### ServiceNow (`https://INSTANCE.service-now.com`, personal developer instance)
| Account | Purpose / state |
|---|---|
| `admin` | Instance admin. **Don't be logged in as admin when authorizing the agent**; see problem 12. |
| `john.doe` | Test user "John Doe" (`john.doe@example.com`), IT Systems Analyst, department **IT**, member of the **Service Desk** group, location Riverside Medical Center. Roles `itil`, `asset`, `sn_incident_write`, `snc_platform_rest_api_access`, `rest_service`. MacBook Air 13" asset tag `123456`, serial `FCPJ2GJTHC`. |
| `OWNER` | Created with **Identity type = AI** and **Internal Integration User** checked: **cannot log in interactively**. Don't use it. |
| `inventory_admin` | Demo user; unused |
| `u_hardware_requester` (role) | Custom role with 13 ACLs (created 2026-10-01 by the background script `scripts/servicenow/create_hardware_requester_role.js`, elevated admin). Rules are found via `sys_security_acl_role` links (ServiceNow rewrites ACL descriptions). `scripts/sn_custom_role.py status|grant|revoke`. |
| `jane.doe` | Test user "Jane Doe" (`jane.doe@example.com`), MRI Technologist, department **Radiology**, created by the seed script; needs **Set Password** manually. Role **`u_hardware_requester` only** (no `itil`), since 2026-10-01: proves the agent works without `itil`. |

| OAuth client (Application Registry) | Use |
|---|---|
| The connector's client (redirects `…/console/oauth/default_oauth.html`, `…/oauth-redirect`) | Gemini Enterprise connector **and** the agent's authorization resource |
| `Hardware Seed Script` | Authorization code, redirect `http://localhost:8765/callback`, for `sn_seed.py` |
| `Hardware Replacement Agent`, `Hardware Replacement Agent 2` | Client-credentials attempts; **unused, can be deleted** |

---

## 3. Architecture

```
Gemini Enterprise (2H-2026) ──A2A message/stream (SSE)──▶ Cloud Run: hardware-replacement-agent
   x-serverless-authorization = Discovery Engine SA  -> Cloud Run IAM
   authorization              = user's ServiceNow token -> passed through untouched
        │
        ├─ app/server.py      A2A app. before_agent interceptor: resolve the user from ServiceNow,
        │                     stage photos as artifacts, A2UI clicks -> "[UI action] name {ctx}" text.
        │                     Custom request converter puts identity + photo refs in state_delta.
        ├─ app/agent.py       LlmAgent + instruction; after_model_callback swaps the model's final
        │                     text for the card a tool staged in temp:card; history compaction
        ├─ app/tools/         intake.py wizard (start/select_device/set_issue/analyze_photos/...),
        │                     review.py next step, filing.py submit_ticket, tickets.py (list/get/
        │                     update_ticket/add_note/cancel/follow), devices.py, addresses.py
        ├─ app/cards.py       deterministic A2UI v0.9 cards (+ to_v08, to_text); the model never writes UI JSON
        ├─ app/servicenow.py  Table API as the user; every ticket query scoped caller_id+hardware
        ├─ app/vision.py      structured photo findings (pydantic schema)
        ├─ app/memory.py      Memory Bank keyed by email; offers, never auto-applies
        └─ app/identity.py    bearer -> /api/now/ui/user/current_user (cached 5 min)
   Agent Runtime engine ENGINE_ID: VertexAiSessionService + VertexAiMemoryBankService
   GCS bucket: GcsArtifactService (photos), copied onto the incident as attachments on submit
```

Key design rules:
- **Device kinds** (`servicenow._kind`): personal (assigned to a person: replaced and shipped), shared
  (department IT equipment) and clinical (model category in `CLINICAL_CATEGORIES`): both repaired on site
  by the asset's `support_group`, at its `location`, billed to its cost center.
- **Confirm the device** (yes/no card) whenever it was picked, typed, described or fuzzy-matched; an
  exact photo match of the tag skips it. Ownership (`tools._relation`) is shown and noted, never enforced.
- **Delivery addresses** (revision 00026): the ship-to is always a street address: ServiceNow's, a saved one, or
  one typed. Permanent ones (home, office) are saved **verbatim** on submit in their own Memory Bank scope
  (`app_name=hardware_replacement_addresses`, `memory.save_address`); hotels/events are used once, never
  saved. Saved ones are offered only as grey review-card buttons; "looks good" never switches. Extracted
  memories mentioning deliveries are filtered out of `recall` (they were vague, e.g. "a Marriott in Chicago").
- **Followers**: a second reporter of equipment with an open ticket joins it via the watch list.
  Every ticket query is `caller_id = me OR watch_list LIKE me`; only the caller can cancel or change
  status, urgency or ship-to (a follower's change request becomes a note for the desk).
- **Sessions keyed by A2A `contextId`** (user_id = `A2A_USER_<contextId>`), never by identity.
  Identity rides in state, refreshed every turn. Memory is keyed by email.
- **ServiceNow token**: request-scoped `ContextVar`, never in state, memory or logs.
- **Every ticket change goes through `_apply_changes`**: attempt → read back and verify → note
  whatever ServiceNow refused → tell the user exactly what was and wasn't done.
- **Ticket card views**: status header always; then `last_note` / `notes` / `details` / `change`.
- Wizard steps are skipped when already known. One sentence can go straight to review.
- **Display mode** (`inbound.display_step`, `cards.to_text`): the GE mobile app can't render A2UI
  ("Response contains unsupported content") and its requests are identical to the web app's, so
  the first reply asks "Desktop or Mobile App?". Mobile gets markdown with numbered options (a number
  acts as a click); desktop gets cards. GE renders markdown, so use blank lines, not single newlines.

---

## 4. How to

**This environment's demo users** (Jane Doe, John Doe) live in the local, uncommitted `seed/users.json`
(`"demo_role": "clinician"` / `"it"`); the committed `seed/users.example.json` and `demo/DEMO.html` use generic
Jane Doe / John Doe. Seed with `sn_seed.py set seed/users.json`; build the personal run sheet with
`uv run python demo/build_demo.py` (writes the uncommitted `demo/DEMO.local.html`).

Settings now live in `.env` (from `.env.example`; ours is filled in locally, not committed) and the
organization profile in `config/organization.yaml`. Install and operations docs: `docs/INSTALL.md`,
`docs/CONFIGURATION.md`, `docs/ROLES.md`, `docs/ARCHITECTURE.md`, and `docs/site/index.html`.


```bash
cd ~/ADK/hardware_replacement
uv run pytest                                   # core tests (seed tests skip if reportlab is absent)
uv run --group seed pytest                      # everything: 48 tests
./scripts/deploy.sh                             # build + deploy Cloud Run (needs gcloud on PATH)
```
- Logs: `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="hardware-replacement-agent"' --project PROJECT_ID --freshness=1h`.
  Useful lines: `turn start`, `resolved end user`, `notes from …`, `priority N requested, ServiceNow assigned M`.
- Agent card JSON: `curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" <service URL>/.well-known/agent-card.json`
- Local A2A client: `CHAT_ID_TOKEN="$(gcloud auth print-identity-token)" CHAT_USER_TOKEN=<SN token> uv run python scripts/chat.py --url <service URL>`.
  Without a user token the agent answers as anonymous, which is expected.
- Seed tool (see README): `uv run --group seed python seed/sn_seed.py login | set <users.json> | report | clear-tickets [--yes] | reset [--user X] [--yes] [--memory]`.
  `set` also creates the hospital equipment in `seed/equipment.json`. Between demo runs use `clear-tickets --yes`.
  Run it from the repo root (running it from `~` fails).

---

## 5. Problems hit and how they were resolved

| # | Problem | Root cause | Resolution |
|---|---|---|---|
| 1 | Firestore database `hardware_tickets` rejected | Database IDs allow only `[a-z0-9-]` | Used `hardware-tickets` (since retired) |
| 2 | Instruction crashed ADK: "Context variable not found: `context`" | ADK treats `{name}` in instructions as state injection | No braces in instruction text |
| 3 | Draft request lost between turns | `_next_step` only saved the draft on the review path | Save the draft at every step |
| 4 | Model wrote fake "[Card shown to user…]" lines | History summaries in that format were imitated | History keeps only the card's intro sentence |
| 5 | User's photo turn never reached the agent; token was in the logs | httpx INFO logs full URLs, and tokeninfo took the token as a query param | httpx/httpcore at WARNING; tokens POSTed in the body |
| 6 | Every turn anonymous | `OAUTH_CLIENT_IDS` unset (Google-token era) | Set it; later replaced entirely by ServiceNow identity |
| 7 | Card fields showed "•" | GE renders a bare "-" as a markdown bullet | "Not visible in photo" / "Not provided" |
| 8 | Photo of a MacBook filed against a ThinkPad | Only tag/serial were cross-checked | Make/type mismatch warning |
| 9 | "Denver home office" shown instead of the ServiceNow address | A Memory Bank memory from **my own test data** was auto-applied | Deleted the test memories; memories are now offered, never applied |
| 10 | **A2A on Agent Runtime gets no user identity** (probed with an authorization attached) | Agent Runtime's gateway replaces `Authorization` with its own token (same `sub` for every caller) | Keep the agent on Cloud Run; use Agent Runtime only for Sessions + Memory Bank |
| 11 | ServiceNow API 401 for basic auth (3 users) and client-credentials tokens (2 clients) | Unresolved instance-level behaviour; authorization-code tokens work | Switched to **per-user OAuth (authorization code)**, the same flow as the GE connector |
| 12 | Agent resolved the user as `admin` | GE's ServiceNow sign-in reused the browser's existing admin session | Authorize in a private window as the real user; delete and re-add the agent to reset |
| 13 | `unauthorized_client` on authorize | Wrong client used (a client-credentials client) | Use the connector's authorization-code client |
| 14 | `OWNER` couldn't log in | Identity type AI + Internal Integration User | Created the human user `john.doe` |
| 15 | Priority 2 requested, 3 assigned; description empty | `incident.urgency` write ACL requires `sn_incident_write`; the description was dropped for the user without roles | Roles added to john.doe; the agent reads back and notes mismatches; details go into the first note if the description is dropped |
| 16 | "No notes" / "(empty)" | Journal table not readable by the user; journal header is "(Comments)", not "(Additional comments)"; empty Text renders "(empty)" | Tolerant parser, raw fallback, never emit empty Text |
| 17 | "Still New" right after assignment | Model answered from an earlier tool result | Instruction: always re-fetch for status questions |
| 18 | Ship-to "changed" but not changed; reopen request silently dropped | Only a note was added; no tool for state | Generic `update_ticket` with attempt, verify, then note-and-tell |
| 19 | Laptops treated as desktops | Demo data files laptops under "Computer" | Model-name heuristics (`_device_type`) |
| 20 | Spaces in a copied client ID | Copy/paste | Credentials are stripped on read |
| 21 | Seed: creating a model category with `cmdb_ci_class=cmdb_ci_hardware` → 403 "Operation Failed" | ServiceNow allows one model category per CI class ("Hardware" already has it); a made-up class name is accepted but no table exists | New equipment categories get no CI class; the seed creates each CI (`cmdb_ci_hardware`) and links it |
| 22 | Equipment asset's `ci` stays empty after setting it | ServiceNow clears `alm_asset.ci` when the model category has no CI class; the CI keeps its `asset` reference | `servicenow._link_cis` finds the CI via `cmdb_ci.asset` |
| 23 | Seed summary said "0 pieces of equipment" | An edit dropped the line recording equipment in the manifest | Restored, and `set` re-records seed-marked equipment it finds; test added |
| 24 | Creating an ACL through the API → 403 | Only a session elevated to `security_admin` may create ACLs | `u_hardware_requester` is a background script the admin runs once (`scripts/servicenow/`); not bypassed |
| 25 | Evals: the model sometimes stopped at "Is this the right device?" and dropped a problem the user had already described | The confirm card read as the end of the turn | `_next_step` result says to call set_issue in the same turn; instruction rule; 9/9 on rerun |

---

## 6. Lessons learned

- **Measure, don't assume.** The A2A-on-Agent-Runtime identity question was settled by a probe
  in about 30 minutes; the documentation and CLI messages were out of date (agents-cli 0.4 said
  "not supported", 1.7 allows it, and it still drops identity).
- **ServiceNow silently drops fields a user can't write.** Always read back and compare; never
  report success from the request alone.
- **Roles for requesters:** normal employees won't have `itil` or `sn_incident_write`. The
  production path is a Service Catalog item (fields set server-side) plus a scripted REST
  endpoint for "my devices".
- **Mobile can't be detected** (measured 2026-09-30 with a throwaway v0.9 agent): web and mobile send identical
  `a2uiClientCapabilities`, and mobile renders neither v0.8 nor v0.9. Keep the first-turn question.
  Details: `docs/BACKLOG.md` section E.
- **A2UI version** (since revision 00024): cards are v0.9; the card declares v0.9 + v0.8 and the agent renders
  whichever the request asks for (`inbound.requested_a2ui_version` → `cards.to_v08`). A v0.9 click is
  `{"action": {...}}` plus a "User action triggered." text part, which `inbound.rewrite_parts` drops.
  To update the card in GE, edit the agent, paste the updated card and save (no re-add needed; user-confirmed 2026-10-01).
- **GE + A2UI v0.8 (history):** `beginRendering` first, fresh `surfaceId` per card, no markdown, no empty
  Text, basic catalog only. `primaryColor`/`font`/`primary` are **ignored** by GE, so button colours can't be changed.
- **GE photos** arrive inline (base64) with sentinel text parts, sometimes with duplicated text.
  Stage them before the runner (the session event cap is 10MB) and raise the A2A body limit to 32MB.
- **User-facing honesty rule:** changed vs requested-and-noted must always be explicit.
- **Test data hygiene:** tests run as the real user's email pollute Memory Bank; use test emails.
- **Agent card updates in GE:** edit the agent, paste the updated card and save; no delete and re-add (user-confirmed).
  Re-adding is only needed to reset a user's authorization (e.g. it signed in as admin).
- **Nothing personal in committed files:** demo users, instance names and local paths live in uncommitted files
  (`seed/users.json`, `.env`, `demo/DEMO.local.html`); committed examples use example.com users.
- **Configuration over code:** organization behaviour lives in `config/organization.yaml` (validated at
  start-up); settings in `.env`. Change the profile, run `uv run python -m app.profile`, deploy.
- **ServiceNow ACLs can't be created through the API** (needs an elevated `security_admin` session): ship them as a
  background script the admin runs; never work around the gate.
- **ServiceNow rewrites new ACL descriptions** ("Allow write for ..."): never use the description as a marker.
- **`itil` is a licensed fulfiller role.** `u_hardware_requester` does everything the agent needs (measured).
- **Admin impersonation works over REST** with a cookie session (`POST /api/now/ui/impersonate/<sys_id>` with the
  admin token, then cookies only): `sn_doctor.py` measures real permissions without passwords.
- **Evals catch what unit tests can't:** the model sometimes ended the turn at the confirm card and dropped the
  problem; only repeated real-model runs showed it. Run `evals/run.py --repeat 2` after prompt/profile changes.
- **Docs links:** check every URL (status + title) before publishing; cloud.google.com docs now redirect to
  docs.cloud.google.com, and some old paths land on generic pages.
- **Check the test result before committing:** a `pytest ... | tail` pipeline hides the exit code; one push went out
  with a failing test (fixed in the next commit).
- **Permission classifier:** deleting secrets or DBs, IAM grants and new deploys need the user to
  name the specific resource or action.

---

## 7. Open items / next steps

The prioritized backlog is `docs/BACKLOG.md` (section G = adoption kit, F = hospital follow-ups, D = ideas).

1. **Manual testing in Gemini Enterprise** by the user (web + mobile; Jane = custom role, John = itil), with the
   personal run sheet `demo/DEMO.local.html` (`uv run python demo/build_demo.py`).
2. **Needs a decision or a live system** (from `docs/BACKLOG.md`): personal identifiers in tracked docs and git
   history (H2: keep, move to an untracked file, or rewrite history); re-run the custom role script in ServiceNow
   for the follower rule (H0.6); F1 live pass; production access (F2/D4/D5: scripted REST API or Service Catalog
   item); take out of service (F3, asset write); vendor contract on the card (F4, which fields); auto-submit (D2)
   and loaner toggle (D3) are product choices; G2.5 registration through the API and the theme `iconUrl` (G3.2)
   need a Gemini Enterprise check; G5 ideas.
3. **Cleanup** (needs explicit user naming): secrets `servicenow-integration`, `servicenow-oauth`; Firestore DB
   `hardware-tickets`; ServiceNow clients "Hardware Replacement Agent" and "…Agent 2"; GE `a2a_probe` and
   `~/ADK/a2a-runtime-probe`. Keep `a2ui-v09-probe` (user's request).
4. **Production hardening:** decide `u_hardware_requester` vs a scripted REST API / Service Catalog item with the
   customer's ServiceNow team (licensing); SSO between Google and ServiceNow.

---

## 8. Related local references
- Skills used: `a2a-cloud-run-gemini-enterprise`, `a2ui-gemini-enterprise` (in `~/.claude/skills`); their
  `references/lessons-learned.md` has the measured GE/A2A facts this project relies on.
- `~/ADK/adk-a2a-agent-runtime-template`: the A2A-on-Agent-Runtime template the probe was built from.
- `demo/DEMO.html`: the 5-minute demo script.
