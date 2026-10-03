# Runbook

Routine operations for whoever runs the agent. Placeholders: `PROJECT_ID`, `REGION`,
`SERVICE_NAME` (default `hardware-replacement-agent`), `https://INSTANCE.service-now.com`.
Symptoms and log queries are in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Rotate the ServiceNow OAuth client secret

The agent itself holds no ServiceNow secret. The client secret lives in two places:

| Where | Used for |
|---|---|
| **Gemini Enterprise**: the agent's authorization (client ID, client secret, authorization URL, token URL) | Signing users in and refreshing their tokens |
| **Secret Manager** `servicenow-seed-oauth` (`INSTANCE\|CLIENT_ID\|CLIENT_SECRET`), only if you use the seed / doctor / profile scripts | The admin scripts' own client (a separate client) |

Steps for the Gemini Enterprise client:

1. In ServiceNow: **System OAuth > Application Registry**, open the client, set a new client
   secret, save.
2. In Gemini Enterprise, update the agent's authorization with the new secret. In the console,
   an agent's authorization can't be edited in place: delete the agent and add it again with the
   same agent card and the new secret (INSTALL step 7). Agent IDs change on re-add.
3. Users authorize again on their next message.
4. Test: send a message, check the logs show `resolved end user` for you.

If the same client also serves the Gemini Enterprise ServiceNow connector, update the connector
too.

For the scripts' client: set a new secret in ServiceNow, then add a new secret version:

```bash
printf '%s' 'https://INSTANCE.service-now.com|CLIENT_ID|NEW_SECRET' | \
  gcloud secrets versions add servicenow-seed-oauth --data-file=- --project PROJECT_ID
uv run --group seed python seed/sn_seed.py login     # signs in again with the new client
```

## Update the agent card

Needed after changing the `agent` block of the profile (name, description, skill, examples), or
the declared A2UI versions or input types.

1. Deploy: `./scripts/deploy.sh`.
2. Copy the new card:
   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app/.well-known/agent-card.json
   ```
3. In Gemini Enterprise: your app > **Agents** > the agent > edit, paste the card into
   **Agent Card JSON**, save. **No need to delete and re-add.** The authorization stays.

## ServiceNow developer instance hibernation

Personal developer instances sleep when idle, and are reclaimed if unused for a long time.

- **Symptom:** the agent says "ServiceNow is waking up".
- **Fix:** open `https://INSTANCE.service-now.com` in a browser and wait until it loads (or wake
  it from the developer portal). Then try again.
- **Before a demo:** open the instance 10 minutes ahead. Log in regularly so it isn't reclaimed.
- Production instances don't hibernate.

## User says "wrong person"

Identity comes from the token, nothing else. The agent asks ServiceNow who the token belongs to
on every turn (`/api/now/ui/user/current_user`). If the agent greets the wrong person, the user
authorized as that person.

1. Find which ServiceNow account authorized:
   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE_NAME" AND textPayload:"resolved end user"' \
     --project PROJECT_ID --freshness=1h --format='value(timestamp,textPayload)'
   ```
   The line shows a masked email and the ServiceNow sys_id. Look the sys_id up in ServiceNow
   (`sys_user`).
2. Usual cause: the browser was signed in to ServiceNow as someone else (often `admin`) when the
   user clicked **Authorize**, and ServiceNow reused that session.
3. Fix: revoke that token in ServiceNow (**System OAuth > Manage Tokens**, if your instance shows
   it), or reset the agent's authorization by deleting and re-adding the agent. Then the user
   authorizes again in a private window, signed in as themselves.
4. Check any tickets filed during that time: their caller is the wrong account. Correct them in
   ServiceNow.

Single sign-on between Google and ServiceNow prevents this.

## Clear a stuck conversation

Each Gemini Enterprise conversation is its own session (keyed by the conversation id). The user
starts a **new conversation** with the agent. Nothing carries over except Memory Bank (memories
and saved addresses, keyed by email) and the tickets in ServiceNow.

Lighter fixes inside the conversation:

- Typing **text** or **buttons** switches the display and redraws the current step.
- "Start over" starts a new request.
- On a phone with a red "unsupported content" box: type **text**, or start a new conversation and
  answer 1.

To remove the old session, see [PRIVACY.md](PRIVACY.md#deleting-one-persons-data).

## Re-run the custom role script after updates

When an update changes `scripts/servicenow/create_hardware_requester_role.js` (see
[CHANGELOG.md](CHANGELOG.md), "Action needed"), the rules in ServiceNow must be updated:

1. In ServiceNow as an admin: user menu > **Elevate role** > `security_admin`.
2. **System Definition > Scripts - Background**: paste the script, **Run script**. It prints one
   line per rule. Safe to run again.
3. Check:
   ```bash
   uv run --group seed python seed/sn_seed.py login
   uv run python scripts/sn_custom_role.py status
   uv run python scripts/sn_doctor.py --matrix --persona none --persona u_hardware_requester --persona itil
   ```

If the ticket category isn't `hardware`, set `CATEGORY` at the top of the script first.

## Roll back a deploy

Each deploy creates a new Cloud Run revision and sends it all traffic.

```bash
gcloud run revisions list --service SERVICE_NAME --region REGION --project PROJECT_ID
gcloud run services update-traffic SERVICE_NAME --to-revisions REVISION_NAME=100 \
  --region REGION --project PROJECT_ID
```

- The previous revision runs with its own settings and its own copy of the profile.
- While traffic is pinned to a revision, a new deploy doesn't take traffic automatically. After
  the fix is deployed, send traffic back to the latest:
  ```bash
  gcloud run services update-traffic SERVICE_NAME --to-latest --region REGION --project PROJECT_ID
  ```
- If the bad release changed the agent card, paste the old card in Gemini Enterprise too.
- Rolling back code doesn't undo ServiceNow changes (role script, tickets).
- See [Cloud Run rollbacks](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).

## After any change

```bash
uv run python -m app.profile          # profile valid
uv run python -m app.messages         # wording valid
uv run --group seed pytest            # tests (check the exit code)
uv run python evals/run.py --repeat 2 # after persona, instruction or problem-choice changes
./scripts/deploy.sh
```
