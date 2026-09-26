# Handoff: ServiceNow Hardware Replacement agent

Background for anyone (or any Claude session) picking this project up. It records what was
built, where everything lives, what went wrong and how it was fixed, and what's still open.
Last updated 2026-09-26.

**No secret values are in this file.** Secrets are named with where they live.

---

## 1. What this is

An ADK agent that lets an employee replace broken work hardware from Gemini Enterprise in a
few taps. It runs on **Cloud Run**, is served to a **Gemini Enterprise** app over **A2A**, draws
**A2UI v0.8** cards (buttons), reads photos with **gemini-3.8-flash**, and files and manages
**ServiceNow incidents as the signed-in employee**, using their own ServiceNow OAuth token.

Status: working end to end in Gemini Enterprise for `john.doe`. Create, list, status, notes,
note, ship-to change, urgency, reopen and cancel are all tested by hand. The GE mobile app works
in text mode (first reply asks mobile vs desktop).

---

## 2. Where things live

### Code and GitHub
| Item | Value |
|---|---|
| Local path | `~/ADK/hardware_replacement` |
| GitHub | https://github.com/OWNER/service-now-hardware-replacement (**private**), branch `main` |
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
| Cloud Run service | `hardware-replacement-agent` | URL `https://hardware-replacement-agent-PROJECT_NUMBER.us-central1.run.app` (the card advertises this form). Latest revision `00019`. `--no-allow-unauthenticated`, min 1 instance, concurrency 4, 1 GiB. |
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
| Agent | "Hardware Replacement", registered as **A2A** from the card at `<service URL>/.well-known/agent-card.json`. Current registration ID `15007067269672713406`; it changes every time the user deletes and re-adds it. |
| Authorization | ServiceNow OAuth (authorization code), resource `hardware-replacement_1790341756507` (also changes on re-add). Built from the **ServiceNow connector's own OAuth client** (redirects include `https://vertexaisearch.cloud.google.com/oauth-redirect`). Scope `useraccount`; auth URL `https://INSTANCE.service-now.com/oauth_auth.do?response_type=code&client_id=<ID>&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=useraccount`; token URL `https://INSTANCE.service-now.com/oauth_token.do`. |
| Other | The ServiceNow connector `service-now-INSTANCE_1789764864639` (federated + actions). Old Cloud Run probe agent `a2a_probe` still registered. |
| Look up current IDs | `GET https://discoveryengine.googleapis.com/v1alpha/projects/PROJECT_NUMBER/locations/global/collections/default_collection/engines/GE_APP_ID/assistants/default_assistant/agents` (header `x-goog-user-project: PROJECT_ID`) |

Each authorization resource can be attached to **one** agent only. In the UI, changing an
agent's authorization means deleting and re-adding the agent.

### ServiceNow (`https://INSTANCE.service-now.com`, personal developer instance)
| Account | Purpose / state |
|---|---|
| `admin` | Instance admin. **Don't be logged in as admin when authorizing the agent**; see problem 12. |
| `john.doe` | The human test user (email `john.doe@example.com`, name "John Doe", Kansas City location). Roles `itil`, `asset`, `sn_incident_write`, `snc_platform_rest_api_access`, `rest_service`. MacBook Air 13" asset tag `123456`, serial `FCPJ2GJTHC`. |
| `OWNER` | Created with **Identity type = AI** and **Internal Integration User** checked: **cannot log in interactively**. Don't use it. |
| `inventory_admin` | Demo user; unused |
| `jane.doe` | To be created by the seed script (`jane.doe@example.com`); needs **Set Password** manually |

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
        ├─ app/tools.py       wizard (start/select_device/set_issue/analyze_photos/skip_photo/
        │                     update_request/show_review/submit_ticket) + tickets (list/get/
        │                     update_ticket/add_note/change_shipping/request_urgent/cancel)
        ├─ app/cards.py       deterministic A2UI v0.8 cards (the model never writes UI JSON)
        ├─ app/servicenow.py  Table API as the user; every ticket query scoped caller_id+hardware
        ├─ app/vision.py      structured photo findings (pydantic schema)
        ├─ app/memory.py      Memory Bank keyed by email; offers, never auto-applies
        └─ app/identity.py    bearer -> /api/now/ui/user/current_user (cached 5 min)
   Agent Runtime engine ENGINE_ID: VertexAiSessionService + VertexAiMemoryBankService
   GCS bucket: GcsArtifactService (photos), copied onto the incident as attachments on submit
```

Key design rules:
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

```bash
cd ~/ADK/hardware_replacement
uv run pytest                                   # core tests (seed tests skip if reportlab is absent)
uv run --group seed pytest                      # everything: 48 tests
./scripts/deploy.sh                             # build + deploy Cloud Run (needs gcloud on PATH)
```
- Logs: `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="hardware-replacement-agent"' --project PROJECT_ID --freshness=1h`.
  Useful lines: `turn start`, `resolved end user`, `notes from …`, `priority N requested, ServiceNow assigned M`.
- Agent card JSON: `curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" <service URL>/.well-known/agent-card.json`
- Local A2A client: `uv run python scripts/chat.py --url <service URL> --token "$(gcloud auth print-identity-token)" --user-token <SN token>`.
  Without a user token the agent answers as anonymous, which is expected.
- Seed tool (see README): `uv run --group seed python seed/sn_seed.py login | set <users.json> | report | reset [--user X] [--yes] [--memory]`.
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
- **GE + A2UI v0.8:** `beginRendering` first, fresh `surfaceId` per card, no markdown, no empty
  Text, basic catalog only. `primaryColor`/`font`/`primary` are **ignored** by GE, so button colours can't be changed.
- **GE photos** arrive inline (base64) with sentinel text parts, sometimes with duplicated text.
  Stage them before the runner (the session event cap is 10MB) and raise the A2A body limit to 32MB.
- **User-facing honesty rule:** changed vs requested-and-noted must always be explicit.
- **Test data hygiene:** tests run as the real user's email pollute Memory Bank; use test emails.
- **Permission classifier:** deleting secrets or DBs, IAM grants and new deploys need the user to
  name the specific resource or action.

---

## 7. Open items / next steps

The prioritized backlog is in `docs/BACKLOG.md`.

1. Seed changes are committed. Run:
   `reset --user dana.whitfield --yes` → `set seed/users.example.json` → `report`.
   Set Jane's password in ServiceNow.
2. **Cleanup** (needs explicit user naming): the `servicenow-integration` and `servicenow-oauth`
   secrets, the `hardware-tickets` Firestore DB, the ServiceNow clients "Hardware Replacement
   Agent" and "…Agent 2", and the `a2a_probe` GE registration + its Cloud Run service if unused.
   The local probe folder `~/ADK/a2a-runtime-probe` is also unused.
3. **Production hardening:** a Service Catalog item for replacements; a scripted REST "my devices"
   endpoint; SSO between Google and ServiceNow (removes the wrong-account risk); optionally a
   guard warning when the ServiceNow user looks like a system account.
4. Branding: not possible via A2UI v0.8 in GE (see lessons).
5. Evals: none yet; `agents-cli` eval could cover the wizard shortcuts and the honesty rule.

---

## 8. Related local references
- Skills used: `a2a-cloud-run-gemini-enterprise`, `a2ui-gemini-enterprise` (in `~/.claude/skills`); their
  `references/lessons-learned.md` has the measured GE/A2A facts this project relies on.
- `~/ADK/adk-a2a-agent-runtime-template`: the A2A-on-Agent-Runtime template the probe was built from.
- `demo/DEMO.md`: the 5-minute demo script.
