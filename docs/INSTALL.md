# Install

From nothing to a working agent in Gemini Enterprise, about an hour. The HTML version
(`docs/site/index.html`) fills your values into every command and has copy buttons.

**You need**
- A Google Cloud project with billing, and an account that can enable APIs and grant IAM roles
  (project Owner is simplest). [Install the gcloud CLI](https://docs.cloud.google.com/sdk/docs/install-sdk).
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs Python 3.12 and the dependencies).
- A [Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs) app you administer.
- A ServiceNow instance where you are an admin.

Placeholders below: `PROJECT_ID`, `REGION` (e.g. `us-central1`), `INSTANCE` (e.g.
`https://acme.service-now.com`), `SERVICE_NAME` (default `hardware-replacement-agent`).

## 1. Get the code

```bash
git clone https://github.com/OWNER/service-now-hardware-replacement.git
cd service-now-hardware-replacement
uv sync
```

## 2. ServiceNow: the sign-in client and roles

1. **OAuth client for Gemini Enterprise.** In ServiceNow: *System OAuth > Application Registry >
   New > Create an OAuth API endpoint for external clients*. Name it (e.g. "Gemini Enterprise"),
   keep it active, grant type **authorization code**, and set the redirect URLs to
   `https://vertexaisearch.cloud.google.com/console/oauth/default_oauth.html,https://vertexaisearch.cloud.google.com/oauth-redirect`.
   Note the **client ID** and **client secret**. Google's walkthrough of the same client, for the
   ServiceNow connector: [ServiceNow configuration](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/servicenow/third-party-config).
   If you already use the Gemini Enterprise ServiceNow connector, its client works too.
2. **Requester roles.** Decide what requesters get; see [ROLES.md](ROLES.md). `itil` gives every
   feature; no roles works with limits; mind licensing.
3. **Optional, for demo data and the permission checker:** a second OAuth client the same way, with
   redirect URL `http://localhost:8765/callback`. Store it in Secret Manager:
   ```bash
   printf '%s' 'INSTANCE|CLIENT_ID|CLIENT_SECRET' | gcloud secrets create servicenow-seed-oauth --data-file=- --project PROJECT_ID
   ```

## 3. Settings

```bash
cp .env.example .env
```
Fill in `GOOGLE_CLOUD_PROJECT` and `SN_INSTANCE_URL` (and `REGION` if not `us-central1`).
Every variable: [CONFIGURATION.md](CONFIGURATION.md).

## 4. Google Cloud setup (once)

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project PROJECT_ID
./scripts/setup.sh
```
`setup.sh` [enables the APIs](https://docs.cloud.google.com/service-usage/docs/enable-disable)
(Cloud Run, Cloud Build, Artifact Registry, Vertex AI / Agent Platform, Cloud Storage, Secret
Manager, Discovery Engine), [creates the photo bucket](https://docs.cloud.google.com/storage/docs/creating-buckets),
[creates the service account](https://docs.cloud.google.com/iam/docs/service-accounts-create)
`hardware-agent` with `roles/aiplatform.user`, `roles/logging.logWriter` and object access on the
bucket, and creates the Agent Runtime instance that holds
[Sessions](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sessions) and
[Memory Bank](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank),
writing `AGENT_ENGINE_ID` into `.env`. Safe to rerun.

## 5. Your organization profile

Edit `config/organization.yaml` (a hospital example) or start from `config/examples/office.yaml`:
agent name, colour, problem choices, texts. Then:
```bash
uv run python -m app.profile
```
Every field: [CONFIGURATION.md](CONFIGURATION.md).

## 6. Deploy

```bash
./scripts/deploy.sh
```
It checks the profile, [deploys from source](https://docs.cloud.google.com/run/docs/deploying-source-code)
to Cloud Run (private: `--no-allow-unauthenticated`, one minimum instance), passes the settings as
[environment variables](https://docs.cloud.google.com/run/docs/configuring/services/environment-variables),
and [grants `roles/run.invoker`](https://docs.cloud.google.com/run/docs/securing/managing-access) on the
service to Gemini Enterprise's service agent `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`.
Rerun after any code or configuration change.

**Other ways to deploy.** The script is a readable wrapper around `gcloud run deploy --source .`;
run its commands by hand if you prefer. `adk deploy cloud_run` and `agents-cli deploy` don't fit:
this agent is a custom A2A server that passes the user's ServiceNow token through, which those
tools don't set up. Terraform is on the backlog (G2.4).

## 7. Register in Gemini Enterprise

1. Get the agent card:
   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app/.well-known/agent-card.json
   ```
2. Google Cloud console > **Gemini Enterprise** > your app > **Agents** > **Add Agents** > **Custom agent
   via A2A** > **Add**, and paste the card JSON into **Agent card JSON**. ([Register and manage A2A agents](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-a2a-agent),
   [agents using A2UI](https://docs.cloud.google.com/gemini/enterprise/docs/a2ui-agents/register-and-manage-an-a2ui-agent).)
3. Add an **authorization** with the ServiceNow client from step 2:

   | Field | Value |
   |---|---|
   | Client ID / secret | from step 2 |
   | Authorization URL | `INSTANCE/oauth_auth.do?response_type=code&client_id=CLIENT_ID&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=useraccount` |
   | Token URL | `INSTANCE/oauth_token.do` |
   | Scope | `useraccount` |

Good to know: one authorization belongs to one agent; to change an agent's card or authorization,
delete and re-add the agent; users authorize on first use and should do so **not** signed in to
ServiceNow as an admin in the same browser (the agent would act as the admin).

## 8. Try it

In Gemini Enterprise, open the agent, send "my laptop screen is cracked", click **Authorize** and
sign in to ServiceNow as yourself, answer **2** (desktop). You should see your devices.
From a terminal (no Gemini Enterprise needed; answers as "anonymous" without a user token):
```bash
uv run python scripts/chat.py --url https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app --token "$(gcloud auth print-identity-token)"
```
Logs: [Cloud Run logging](https://docs.cloud.google.com/run/docs/logging),
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE_NAME"' --project PROJECT_ID --freshness=1h
```

## 9. Check your users' permissions

```bash
uv run --group seed python seed/sn_seed.py login
uv run python scripts/sn_doctor.py --as some.user
```
See [ROLES.md](ROLES.md).

## 10. Optional: demo data

```bash
uv run --group seed python seed/sn_seed.py set seed/users.example.json
uv run --group seed python seed/sn_seed.py report
```
See the README's demo data section and `demo/DEMO.html`.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `deploy.sh` stops: "Set ... in .env" | Fill that variable; see CONFIGURATION.md |
| Start-up error "Organization profile ... is not valid" | Run `uv run python -m app.profile`; it names the field |
| Gemini Enterprise: agent doesn't answer / 403 in logs | `run.invoker` missing for the Discovery Engine service agent: rerun `deploy.sh` |
| "unauthorized_client" when authorizing | Wrong client ID/secret, or the client's redirect URLs are missing |
| The agent answers as `admin` | Re-add the agent; authorize in a browser not signed in to ServiceNow as admin |
| Urgency or description missing on tickets | The user lacks roles: `sn_doctor.py --as <user>` |
| "ServiceNow is waking up" | A developer instance hibernated: open it in a browser and retry |
| Red "unsupported content" box on a phone | The conversation was answered as desktop; start a new one and answer 1 |
