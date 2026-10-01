# ServiceNow Hardware Replacement agent

An ADK agent on Cloud Run, served to a Gemini Enterprise app over A2A, that lets hospital staff
report broken hardware in a sentence or a photo and follow up on it, filed in ServiceNow **as that
person**. It covers personal devices (replaced and shipped) and shared or clinical equipment such as
MRI and CT scanners, infusion pumps, patient monitors and beds (repaired on site by the team that
supports them). Cards are A2UI v0.9 in the web app (v0.8 for a client that still negotiates it); the mobile app gets the same steps as numbered text.

## Documentation

| Read | For |
|---|---|
| **`docs/site/index.html`** | Everything below as one navigable page: fill in your values once, copy every command |
| [docs/INSTALL.md](docs/INSTALL.md) | Install from scratch, step by step |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works: diagrams and a module map |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `.env` settings and the organization profile (`config/organization.yaml`) |
| [docs/ROLES.md](docs/ROLES.md) | ServiceNow roles, measured, and the permission checker |
| [docs/HANDOFF.md](docs/HANDOFF.md), [docs/BACKLOG.md](docs/BACKLOG.md) | Project history, decisions, what's next |

## What it does

| Step | Card | Time savers |
|---|---|---|
| 1 Device | The user's devices (model, tag, serial), then "Is this the right device?" | Identity, devices, ship-to and bill-to come from ServiceNow. Equipment can be named in words ("the MRI in room 104"), by tag or serial, or by a photo of its sticker; an exact photo match skips the check. |
| 2 Problem | Issue buttons (laptop-style for personal devices; not working / error / damaged / safety concern for equipment), or free text | One sentence fills steps 1 and 2 and sets urgency. A safety concern is always critical and tells the reporter to take the equipment out of service. |
| 3 Photo | Only when it helps: required for cracks and physical damage, skippable otherwise | Gemini reads device type, make, model, part number, serial and asset tag, and documents damage as evidence. |
| 4 Review | Everything pre-filled, then Submit | Warranty or refresh eligibility, recommended fulfilment, ship-to, bill-to, and warnings (owner, serial or make mismatch). |

For equipment, ownership is checked, never enforced: the reporter's own device, their department's
equipment, equipment their group supports, or "could not be confirmed" (noted on the ticket). The
ticket goes to the equipment's support group (Clinical Engineering, Imaging Engineering, Service
Desk) with its location and CI. If the equipment already has an open ticket, the second reporter adds
a note to it and **follows** it (watch list) instead of filing a duplicate.

After submitting, users can list the hardware tickets they reported or follow, check status, read notes, add notes,
change the ship-to address, request urgent handling, reopen, and cancel with a reason, all
limited to their own hardware tickets.

**Every change is attempted, then verified against what ServiceNow stored.** Anything
ServiceNow's policy doesn't allow for that user (e.g. urgency without `sn_incident_write`,
reopening, canceling) is added to the ticket as a note for the service desk, and the user is
told plainly that it was requested, not done.

## Architecture

```
Gemini Enterprise ──A2A (message/stream)──▶ Cloud Run: this agent
   │  x-serverless-authorization: Discovery Engine SA (Cloud Run IAM)
   │  authorization: the user's ServiceNow OAuth token (pass-through)
   ▼
 app/server.py   resolve user from ServiceNow, stage photos as artifacts, A2UI clicks -> text
 app/agent.py    LlmAgent (gemini-3.8-flash, global), card swap-in callbacks
 app/tools.py    wizard + ticket tools, one attempt/verify/note path for every change
 app/cards.py    deterministic A2UI v0.9 cards (the model never writes UI JSON), v0.8 and text renderings
 app/vision.py   structured photo analysis
 app/servicenow.py  Table API client, always as the signed-in user; ticket queries are
                    always scoped to (caller_id = user OR watch_list has user) and category = hardware
 app/memory.py   Memory Bank, keyed by the user's email (offers, never auto-applies)

Agent Runtime (code-less engine): managed Sessions (keyed by A2A contextId) + Memory Bank
GCS bucket via ADK GcsArtifactService: photos, then attached to the ServiceNow incident
```

Why Cloud Run and not Agent Runtime for the agent itself: an A2A agent on Agent Runtime does
not receive the Gemini Enterprise user's credential (the platform replaces the Authorization
header), so identity could not be carried through. Agent Runtime is used only for its managed
Sessions and Memory Bank.

## Identity

The agent's Gemini Enterprise authorization resource is a **ServiceNow OAuth
(authorization code)** client. Each user signs in to ServiceNow once; Gemini Enterprise
forwards their token and the agent calls ServiceNow with it, so ServiceNow's own access
controls apply. The user is resolved with `/api/now/ui/user/current_user`. The token is held
in a request-scoped ContextVar and never written to state, memory or logs.

## ServiceNow setup

- An OAuth client (Application Registry, authorization code grant) whose redirect URLs include
  `https://vertexaisearch.cloud.google.com/oauth-redirect`.
- Gemini Enterprise authorization resource:
  - Authorization URL: `https://<instance>.service-now.com/oauth_auth.do?response_type=code&client_id=<CLIENT_ID>&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=useraccount`
  - Token URL: `https://<instance>.service-now.com/oauth_token.do`
  - Scope: `useraccount`
- Users need a location (with street address), department and cost center, and assets in
  `alm_hardware` assigned to them. Without the asset role, "my devices" may be empty; the
  production answer is a scripted REST endpoint that returns only the caller's devices.
- Requesters typically can't set urgency (`incident.urgency` write ACL requires
  `sn_incident_write`) or change state; the agent handles that by noting the request. For
  production, a Service Catalog item for hardware replacement is the natural fit.
- Sign in to ServiceNow **as the person** when Gemini Enterprise asks: the authorization uses
  whatever ServiceNow session the browser has. Single sign-on between Google and ServiceNow
  removes that risk.

## Run locally

```bash
cp .env.example .env        # fill in the required values
uv sync
uv run --group seed pytest
uv run uvicorn app.server:app --port 8080
uv run python scripts/chat.py --user-token "<servicenow access token>"
```

`chat.py` is an A2A client that behaves like Gemini Enterprise: type text,
`/photo path.jpg`, `/click N`.

## Install and deploy

```bash
./scripts/setup.sh     # once: APIs, bucket, service account, Agent Runtime instance
./scripts/deploy.sh    # every change: checks the profile, deploys, grants Gemini Enterprise access
```

Full steps, including ServiceNow and Gemini Enterprise registration: [docs/INSTALL.md](docs/INSTALL.md).

## Configuration

- **`.env`** (from `.env.example`): project, region, ServiceNow instance, model. Never committed.
- **`config/organization.yaml`**: agent name and persona, button colour, problem choices with
  photo and urgency rules, device rules, response targets and texts. Check it with
  `uv run python -m app.profile`. Example for an office: `config/examples/office.yaml`.

Every field: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## ServiceNow permissions

The agent acts as each signed-in user. `itil` enables every feature; no roles works with limits.
Check any user with `uv run python scripts/sn_doctor.py --as <user>`: [docs/ROLES.md](docs/ROLES.md).

## Known limits

- The agent card declares A2UI v0.9 and v0.8; Gemini Enterprise picks v0.9 (sends `theme.primaryColor`).
  After changing the declared versions, delete and re-add the agent: GE keeps the card from registration.
- The Gemini Enterprise mobile app renders no A2UI, so the first reply asks desktop or mobile.
- ServiceNow developer instances hibernate; the agent reports "ServiceNow is waking up".

## Demo data: seed, report and reset (`seed/sn_seed.py`)

Creates realistic ServiceNow users with a standard office kit, a fictional hospital's shared and
clinical equipment (`seed/equipment.json`: departments, room-level locations, support groups, model
categories, eight pieces of equipment with CIs), prints an asset register PDF, and resets everything
afterwards. The demo script is `demo/DEMO.html`.

**One-time setup**
1. ServiceNow, as admin: Application Registry → New → **OAuth – Authorization code grant**,
   redirect URL `http://localhost:8765/callback`, scope `useraccount`.
2. Store the client in Secret Manager (or set `SN_INSTANCE_URL`, `SN_CLIENT_ID`, `SN_CLIENT_SECRET`):
   ```bash
   printf '%s' 'https://INSTANCE.service-now.com|CLIENT_ID|CLIENT_SECRET' | gcloud secrets create servicenow-seed-oauth --data-file=- --project PROJECT_ID
   ```
3. `uv run --group seed python seed/sn_seed.py login` opens the browser; sign in as **admin**.
   The token and refresh token are cached in `~/.config/hw-seed/token.json` (0600).

The instance rejects basic auth and client-credentials tokens for API calls, so the script
uses the same browser sign-in (authorization code) as Gemini Enterprise.

**Use**
```bash
uv run --group seed python seed/sn_seed.py set seed/users.example.json   # users, kit, groups, equipment
uv run --group seed python seed/sn_seed.py report                        # seed/state/office-assets.pdf
uv run --group seed python seed/sn_seed.py clear-tickets [--yes]         # demo tickets only, between runs
uv run --group seed python seed/sn_seed.py reset                         # dry run: shows the plan
uv run --group seed python seed/sn_seed.py reset --yes [--memory]        # apply
```

- **Users file:** one object per person (`user_name`, names, email, title, phone, department,
  cost_center, manager, location with address, `groups`, `roles`, optional `items` subset).
  Existing users are updated; their original values are saved for reset.
- **Equipment (`seed/equipment.json`):** owned by departments, not people, with a location and a
  support group. ServiceNow allows one model category per CI class, so new equipment categories
  have none and the script creates each item's CI (`cmdb_ci_hardware`, linked back to the asset).
- **Kit (`seed/catalog.json`):** laptop, monitor, dock, desk phone, mobile phone, printer,
  headset, keyboard/mouse, and software licences (Microsoft 365, Acrobat, Zoom, Slack). Each gets
  a unique asset tag, a serial number or licence key, model number, manufacturer, purchase date,
  warranty or term end and cost. Missing manufacturers and models are created.
- **Idempotent:** re-running `set` updates users and issues only items they don't have yet.
- **Reset** deletes every incident the seeded users are the caller on or opened (attachments
  go with them), all seeded assets and any models, manufacturers, locations, departments and
  cost centers the script created. It deletes users it created and restores pre-existing users
  to their original values. `--memory` also clears their Hardware Replacement agent memories.
  The plan is recorded in `seed/state/manifest.json`, and every seeded asset is also marked
  `[hw-seed]` in its comments.
