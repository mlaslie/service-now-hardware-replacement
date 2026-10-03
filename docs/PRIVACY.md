# Privacy and data handling

What the agent stores, where, for how long, and how to delete one person's data. Placeholders:
`PROJECT_ID`, `REGION`, `BUCKET` (default `PROJECT_ID-hardware-ticket-photos`), `ENGINE_ID`
(`AGENT_ENGINE_ID` in `.env`). This page describes the code as built; it is not legal advice.
Check it against your own policies.

## What is stored where

| Where | What | Keyed by | Kept until |
|---|---|---|---|
| **Agent Runtime Sessions** | The conversation: messages, tool calls and results, the draft (device, problem, urgency, ship-to address), the user's ServiceNow profile refreshed each turn (name, email, phone, title, department, cost center, manager, location, groups), photo references, display mode | `user_id = A2A_USER_<contextId>`, session id = the Gemini Enterprise conversation id | Deleted, or the session's expiry. This agent sets no TTL; check the Sessions defaults. |
| **Memory Bank**, scope `hardware_replacement` | Memories Memory Bank extracts after a ticket is filed, limited to the topics set in `scripts/create_state_engine.py`: user preferences, delivery and contact preferences, hardware history | the user's email | Deleted. No TTL is configured. |
| **Memory Bank**, scope `hardware_replacement_addresses` | Saved permanent delivery addresses (home, office), verbatim, one per label. Hotels and events are never saved. | the user's email | Replaced when the user saves a new address for the same label, or deleted |
| **Cloud Storage** bucket | Every photo the user sends, as sent | path `hardware_replacement/A2A_USER_<contextId>/<contextId>/<photo file>/<version>` | Deleted. No lifecycle rule by default. |
| **ServiceNow** | The incident, its notes, and copies of the photos as attachments | the incident | Your ServiceNow retention rules |
| **Cloud Logging** | One line per turn: conversation id, masked email (`j***@example.com`), ServiceNow sys_id, photo count, safe request headers (user agent, A2A extensions; others by name only), warnings | time | Your log bucket's retention |
| **Cloud Run memory** | Identity cache (per token hash, 5 minutes); in-flight A2A tasks | instance | Instance restart |

Not stored anywhere:

- **ServiceNow tokens.** The user's token lives for one request in a `ContextVar`. It is never
  written to session state, Memory Bank or logs. HTTP client logging is kept at WARNING so request
  URLs can't leak it.
- **Photo bytes in the session.** Photos are saved to Cloud Storage before the model runs; the
  session only holds a reference.
- **Any ServiceNow secret.** The agent has none.

Model calls (chat and photo reading) go to Vertex AI Gemini in `GOOGLE_CLOUD_LOCATION`
(default `global`), under your Google Cloud terms.

## Retention knobs

**Photos.** A lifecycle rule deletes photos after N days. Photos are copied to ServiceNow when the
ticket is filed, so N only needs to cover the time between sending a photo and submitting.

```bash
cat > lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}
JSON
gcloud storage buckets update gs://BUCKET --lifecycle-file=lifecycle.json --project PROJECT_ID
gcloud storage buckets describe gs://BUCKET --format='default(lifecycle_config)' --project PROJECT_ID
```

**Memory Bank.** Memory Bank supports a time-to-live in its configuration
(`memory_bank_config.ttl_config`). `scripts/create_state_engine.py` doesn't set one. Add it there
for new instances, or update the instance; see the
[Memory Bank docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank).
A TTL also expires saved addresses.

**Sessions.** A session can carry a TTL (minimum 24 hours). ADK's session service creates
sessions without one here. To expire them, delete old sessions on a schedule (below), or set a
TTL in a custom session service.

**Logs.** Set the retention of the log bucket the service writes to
([Cloud Logging retention](https://docs.cloud.google.com/logging/docs/buckets)).

**Turning memory off.** `features: {memory: false}` in `config/organization.yaml` stops recall and stops
sending conversations to Memory Bank; `saved_addresses: false` stops remembering delivery addresses.
Existing memories stay until deleted (below).

## Deleting one person's data

Run from the repo root (`.env` provides project and engine). Replace `jane.doe@example.com`.

**1. Memories (both scopes).** Same approach as `seed/sn_seed.py reset --memory`, for anyone:

```bash
uv run python - <<'PY'
from app import config
from app.memory import _client, _engine
EMAIL = "jane.doe@example.com"
for m in _client().agent_engines.memories.list(name=_engine()):
    if (m.scope or {}).get("user_id") == EMAIL:
        print("delete", m.name, (m.scope or {}).get("app_name"))
        _client().agent_engines.memories.delete(name=m.name)
PY
```

**2. Sessions, and 3. their photos.** Sessions are keyed by conversation, not by person. The
user's email is in each session's state (`end_user.email`), and the same conversation id names
the photo folder.

```bash
uv run python - <<'PY'
from app import config
from app.memory import _client, _engine
EMAIL = "jane.doe@example.com"
for s in _client().agent_engines.sessions.list(name=_engine()):
    if ((s.session_state or {}).get("end_user") or {}).get("email") == EMAIL:
        sid = s.name.rsplit("/", 1)[-1]
        print(f"gs://{config.ARTIFACT_BUCKET}/{config.APP_NAME}/A2A_USER_{sid}/")
        _client().agent_engines.sessions.delete(name=s.name)
PY
# then, for each folder printed:
gcloud storage rm --recursive gs://BUCKET/hardware_replacement/A2A_USER_CONTEXT_ID/ --project PROJECT_ID
```

Print first and delete second if you want to review the list: comment out the `delete` lines on
the first run. These snippets use the same SDK calls as the agent and the seed tool but have not
been run as written; try them on a test user first.

**4. ServiceNow.** Incidents, notes and attachments follow your ServiceNow processes. The agent
can't delete tickets.

**5. Logs.** Log lines hold a masked email and the ServiceNow sys_id. They expire with the log
bucket's retention.

## Hospitals and other sensitive settings

- **Photos can capture patients or patient data** (a monitor showing a chart, a patient in the
  background). Tell staff not to photograph patients or screens with patient information. The
  default wording has no such warning. Add one without code: in `config/messages.yaml`, extend
  `common.attach_hint` (shown on every photo request), e.g.
  `common.attach_hint: "Use the attach (+) button to add the photo. Keep patients and patient details out of the shot."`
  Check with `uv run python -m app.messages`, then deploy. Or put it in a problem's `photo_of`
  text in the profile.
- **No blurring.** Optional face or PHI blurring before storage is not built (BACKLOG G4.1).
- Photos reach three places: the bucket, the model call, and the ServiceNow ticket. Apply your
  PHI rules to all three.
- Ticket text comes from what the user typed. The agent doesn't screen it.
- A safety concern is reported as a ticket. It does not replace your safety event reporting
  process; the safety text in the profile says so.

## Options to evaluate

Not configured by this repo. Each needs checking against the products' current support.

- **VPC Service Controls** around Vertex AI, Cloud Storage and Agent Runtime, to keep data inside a
  perimeter. Gemini Enterprise calls Cloud Run from outside the project; test that path.
- **CMEK** (customer-managed encryption keys) for the bucket, Cloud Run and Agent Runtime.
- **Data residency.** Pick `REGION` for Cloud Run, the bucket and Agent Runtime. The model is
  served from `global` by default; use a regional model location if residency requires it and the
  model is offered there.
- **Gemini Enterprise** keeps its own conversation history; that is governed by the Gemini
  Enterprise settings, not this agent.
