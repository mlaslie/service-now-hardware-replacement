# ServiceNow Hardware Replacement agent

An ADK agent on Cloud Run, served to a Gemini Enterprise app over A2A, that walks an
employee through a hardware replacement request in a few taps and files it in ServiceNow
**as that employee**. Cards are A2UI v0.8.

## What it does

| Step | Card | Time savers |
|---|---|---|
| 1 Device | The user's ServiceNow assets as buttons | Identity, devices, ship-to and bill-to come from ServiceNow. A photo of the asset label also works. |
| 2 Problem | Eight issue buttons, or free text | One sentence ("my laptop won't turn on, demo tomorrow") fills steps 1 and 2 and sets urgency. |
| 3 Photo | Only when it helps: required for cracks and physical damage, skippable otherwise | Gemini reads device type, make, model, part number, serial and asset tag, and documents damage as evidence. |
| 4 Review | Everything pre-filled, then Submit | Warranty or refresh eligibility, recommended fulfilment, ship-to, bill-to, and warnings (owner, serial or make mismatch). |

After submitting, users can list their hardware tickets, check status, read notes, add notes,
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
 app/cards.py    deterministic A2UI v0.8 cards (the model never writes UI JSON)
 app/vision.py   structured photo analysis
 app/servicenow.py  Table API client, always as the signed-in user; ticket queries are
                    always scoped to caller_id = user and category = hardware
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
uv sync
uv run pytest
uv run uvicorn app.server:app --port 8080
uv run python scripts/chat.py --user-token "<servicenow access token>"
```

`chat.py` is an A2A client that behaves like Gemini Enterprise: type text,
`/photo path.jpg`, `/click N`.

## Deploy

```bash
./scripts/deploy.sh
```

Deploys with `--no-allow-unauthenticated`, grants `run.invoker` to the Discovery Engine
service agent (scoped to the service), and sets the environment from `app/config.py`.
Register the agent in Gemini Enterprise as A2A with the card at
`https://<service-url>/.well-known/agent-card.json` and the ServiceNow authorization.

## Configuration (`app/config.py`)

| Variable | Default |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | `PROJECT_ID` |
| `MODEL` / `VISION_MODEL` | `gemini-3.8-flash` (location `global`) |
| `SN_INSTANCE_URL` | `https://INSTANCE.service-now.com` |
| `AGENT_ENGINE_ID` / `AGENT_ENGINE_LOCATION` | code-less engine for Sessions + Memory Bank |
| `ARTIFACT_BUCKET` | `<project>-hardware-ticket-photos` |
| `SERVICE_URL` | the public Cloud Run URL (advertised in the agent card) |

## Known limits

- Gemini Enterprise renders A2UI v0.8 with its own theme; `primaryColor`, `font` and
  `primary` buttons are sent but not applied.
- ServiceNow developer instances hibernate; the agent reports "ServiceNow is waking up".
