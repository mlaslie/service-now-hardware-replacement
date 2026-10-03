# Troubleshooting

For operators. Each entry: **symptom → cause → fix**, and the log query that finds it. Most
entries come from problems already hit and solved (`docs/HANDOFF.md` section 5).

## Reading the logs

All queries use these placeholders: `PROJECT_ID`, `SERVICE_NAME` (default
`hardware-replacement-agent`). The base filter is the service's logs:

```bash
BASE='resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE_NAME"'
gcloud logging read "$BASE" --project PROJECT_ID --freshness=1h --limit 200 \
  --format='value(timestamp,textPayload)'
```

Add a text match with `AND textPayload:"..."`. Useful lines the agent writes:

| Line | Meaning |
|---|---|
| `turn start context=...` | A message arrived (one per turn) |
| `turn context=... user=j***@example.com(servicenow) photos=N` | Who it resolved, and how many photos |
| `resolved end user j***@example.com (SYS_ID)` | Identity from the token; the sys_id is the ServiceNow user |
| `display mode context=... {'ui_mode': ...}` | The desktop/mobile answer |
| `priority N requested, ServiceNow assigned M` | ServiceNow lowered the priority |
| `description not accepted by ServiceNow; details added as a note` | The user lacks write access to the description |
| `update refused (...); retrying with the note only` | A ticket change was refused |
| `notes from journal fields` / `notes from journal table` | How notes were read |

Emails are masked in logs (`j***@example.com`). Tokens are never logged.

Request logs (HTTP status codes) are in a separate log:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE_NAME" AND logName="projects/PROJECT_ID/logs/run.googleapis.com%2Frequests" AND httpRequest.status>=400' \
  --project PROJECT_ID --freshness=1d --format='value(timestamp,httpRequest.status,httpRequest.requestUrl)'
```

## Symptoms

### The agent never answers in Gemini Enterprise
- **Cause:** Gemini Enterprise's service agent can't invoke the Cloud Run service (403), or the
  agent card points at the wrong URL.
- **Fix:** rerun `./scripts/deploy.sh` (it grants `roles/run.invoker` to
  `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`). Check the card's
  `url` is the Cloud Run URL, not `localhost`:
  `curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" https://SERVICE_URL/.well-known/agent-card.json | jq .url`.
- **Find it:** the request-log query above with `httpRequest.status=403`. No `turn start` lines.

### The agent says the ServiceNow sign-in is missing or expired, every turn
- **Cause:** no user token arrived (the agent was added with *Skip & Finish*, without an
  authorization), or ServiceNow rejected it (expired, revoked, wrong client).
- **Fix:** the user reconnects ServiceNow for the agent in Gemini Enterprise. If no one is ever
  asked to authorize, re-add the agent with the authorization (INSTALL step 7c).
- **Find it:** `AND textPayload:"(anonymous)"` (the `turn` line) and
  `AND textPayload:"ServiceNow rejected the forwarded user token"`.

### The agent acts as the wrong person (often `admin`)
- **Cause:** identity comes from the token. The user authorized while their browser was signed
  in to ServiceNow as someone else, and ServiceNow reused that session.
- **Fix:** see [RUNBOOK.md](RUNBOOK.md#user-says-wrong-person). Authorize in a private window,
  as the real user.
- **Find it:** `AND textPayload:"resolved end user"`; compare the sys_id with the user's record.

### `unauthorized_client` when authorizing
- **Cause:** the wrong OAuth client (for example a client-credentials one), a typo or spaces in
  the client ID, or missing redirect URLs on the client.
- **Fix:** use an authorization-code client with both Gemini Enterprise redirect URLs (INSTALL
  step 2). Re-enter the client ID and secret.
- **Find it:** not in the agent's logs: it fails before any request reaches Cloud Run.

### "ServiceNow is waking up"
- **Cause:** a developer instance hibernated. It answers with an HTML page instead of JSON.
- **Fix:** open the instance in a browser, wait until it loads, try again. See
  [RUNBOOK.md](RUNBOOK.md#servicenow-developer-instance-hibernation).
- **Find it:** `turn` lines with `(anonymous)` and no "rejected" line.

### "ServiceNow didn't respond, so this step may not have completed"
- **Cause:** a timeout or connection error to ServiceNow (20 s timeout per call).
- **Fix:** try again. A retry never files a second ticket (correlation id check).
- **Find it:** `AND textPayload:"ServiceNow unavailable in"`, or
  `AND textPayload:"ServiceNow identity lookup failed"` when it happens at the start of a turn.

### Urgency, description or status changes don't stick
- **Cause:** the user's ServiceNow roles don't allow writing those fields. ServiceNow drops them
  without an error. The agent adds a note and says so.
- **Fix:** expected without roles. Check the user with
  `uv run python scripts/sn_doctor.py --as USER_NAME --read-only`. Grant `u_hardware_requester`
  or `itil` if the organization wants these changes to work ([ROLES.md](ROLES.md)).
- **Find it:** `AND textPayload:"ServiceNow assigned"`, `AND textPayload:"description not accepted"`,
  `AND textPayload:"update refused"`.

### "My devices" is empty, or no "already reported" offer for shared equipment
- **Cause:** assets aren't assigned to the user in ServiceNow, they live in a table not in
  `servicenow.asset_tables`, or the user can't read CIs (no CI link means no duplicate check).
- **Fix:** check assignments; add the table to the profile; check the user with `sn_doctor`.
- **Find it:** `AND textPayload:"CIs not readable"`, `AND textPayload:"open tickets for the device not readable"`.

### Photo turn fails, or the photo is ignored
- **Cause:** photos arrive inline as base64. The A2A default body limit (10 MB) used to reject a
  phone photo; the server now allows 32 MB per request and 15 MB per photo. A photo that is too
  large, of an unsupported type (not JPEG/PNG/WebP/HEIC), sent as a link, or that can't be saved
  is reported to the user instead of failing the turn.
- **Fix:** ask for a smaller photo, or check the runtime service account has
  `roles/storage.objectUser` on the bucket (rerun `deploy.sh`).
- **Find it:** `AND textPayload:"photo could not be saved"`, `AND textPayload:"photo analysis failed"`,
  `AND textPayload:"could not attach"`.

### Red "Response contains unsupported content" box on a phone
- **Cause:** the conversation was answered as desktop (2), so it sends cards. The mobile app
  can't show them.
- **Fix:** type **text**, or start a new conversation and answer 1.
- **Find it:** `AND textPayload:"display mode"`.

### A typed address was replaced by a saved one, or an address appeared that the user never gave
- **Cause:** fixed. A memory from test data used to be applied automatically; label matching used
  substrings ("Warehouse" matched "house"). Now saved addresses are only offered as buttons, and
  typed street addresses are never swapped.
- **Fix:** if it happens again, check which Memory Bank memories exist for that user (both scopes,
  see [PRIVACY.md](PRIVACY.md)). Don't run tests with real users' emails: they pollute Memory Bank.

### The model stops at "Is this the right device?" and forgets the problem
- **Cause:** the confirm card read as the end of the turn. Fixed in the instruction and the step
  result; it can come back after prompt or profile changes.
- **Fix:** run `uv run python evals/run.py --repeat 2` after changing the persona, the instruction
  or the problem choices.

### The agent answers a status question from old information
- **Cause:** the model reused an earlier tool result. The instruction says to re-fetch every time.
- **Fix:** if it recurs, add an eval case for the phrasing.

### Start-up fails: "Organization profile ... is not valid" or "The agent is missing settings"
- **Cause:** a typo or bad value in `config/organization.yaml`, or a missing `.env` value.
- **Fix:** `uv run python -m app.profile` names the field. `deploy.sh` stops early on missing
  settings.
- **Find it:** `AND severity>=ERROR` around the deploy time.

### Agent card changes don't show in Gemini Enterprise
- **Cause:** Gemini Enterprise keeps the card that was pasted at registration.
- **Fix:** edit the agent in Gemini Enterprise, paste the new card, save. No need to re-add.

### ServiceNow: creating an access rule through the API returns 403
- **Cause:** only a session elevated to `security_admin` may create ACLs.
- **Fix:** run `scripts/servicenow/create_hardware_requester_role.js` as a background script after
  elevating. Don't work around the gate.

### Seed: creating a model category with a CI class returns 403 "Operation Failed"
- **Cause:** ServiceNow allows one model category per CI class.
- **Fix:** the seed creates equipment categories without a CI class and creates and links each CI
  itself. Nothing to do unless you changed the seed.

## When nothing above fits

1. `turn start` lines present? If not, it is Gemini Enterprise to Cloud Run (IAM, URL, card).
2. `turn ... user=...(servicenow)`? If not, it is the token (sign-in, hibernation).
3. Warnings in the same minute (`AND severity>=WARNING`) usually name the failing call.
4. Reproduce from a terminal: `CHAT_ID_TOKEN="$(gcloud auth print-identity-token)" CHAT_USER_TOKEN=<token> uv run python scripts/chat.py --url https://SERVICE_URL`.
5. Check the user's permissions: `uv run python scripts/sn_doctor.py --as USER_NAME --read-only`.
