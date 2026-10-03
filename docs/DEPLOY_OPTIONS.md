# Deploy options

The agent is one Cloud Run service built from this repo's `Dockerfile`. There are three ways to
deploy it; the first is the supported one. One-time setup (APIs, bucket, service account, Agent
Runtime instance) is `scripts/setup.sh`, described in [INSTALL.md](INSTALL.md).

## 1. `scripts/deploy.sh` (supported)

```bash
./scripts/deploy.sh
```

Read the script before running it; it is short. In order, it:

1. Loads `.env` and stops if `GOOGLE_CLOUD_PROJECT`, `SN_INSTANCE_URL` or `AGENT_ENGINE_ID` is
   missing. Fills defaults for `REGION`, `SERVICE_NAME`, `MODEL`, `GOOGLE_CLOUD_LOCATION`,
   `ARTIFACT_BUCKET`, `AGENT_ENGINE_LOCATION`.
2. Validates the organization profile (`uv run python -m app.profile`) and stops if it is invalid.
3. Works out the service URL from the project number
   (`https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app`), so the agent card is right on the first
   deploy.
4. Creates the photo bucket if missing (uniform access, public access prevention).
5. Creates the runtime service account `hardware-agent` if missing, and grants it
   `roles/aiplatform.user`, `roles/logging.logWriter`, and `roles/storage.objectUser` on the
   bucket.
6. Deploys from source with `gcloud run deploy --source .` (Cloud Build builds the image).
7. Grants `roles/run.invoker` on the service to Gemini Enterprise's service agent
   `service-PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com`.
8. Prints the agent card URL and a `curl` to check it.

It is idempotent: rerun it after any code or configuration change.

Only these variables reach Cloud Run: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `MODEL`,
`ARTIFACT_BUCKET`, `AGENT_ENGINE_ID`, `AGENT_ENGINE_LOCATION`, `REGION`, `SERVICE_URL`,
`SN_INSTANCE_URL`, plus `ORGANIZATION_PROFILE`, `MESSAGES_FILE` and `VISION_MODEL` when they are set
(the two files must be under `config/`). The script uses `--set-env-vars`, which replaces the whole
list on every deploy, so a variable added later with `gcloud run services update --update-env-vars`
is removed by the next `deploy.sh`: put it in `.env` and the script instead.

`.env` is not in the image: the `Dockerfile` copies only `pyproject.toml`, `uv.lock`, `app/` and
`config/`.

## 2. Plain `gcloud run deploy`

The same deployment by hand, for people who want to see every flag or put it in their own
pipeline. Run the setup once (steps 4 and 5 above, or `scripts/setup.sh`), then:

```bash
PROJECT_ID=your-project
REGION=us-central1
SERVICE_NAME=hardware-replacement-agent
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
SERVICE_URL=https://$SERVICE_NAME-$PROJECT_NUMBER.$REGION.run.app

gcloud run deploy $SERVICE_NAME --source . --project $PROJECT_ID --region $REGION \
  --service-account hardware-agent@$PROJECT_ID.iam.gserviceaccount.com \
  --no-allow-unauthenticated --ingress=all \
  --memory=1Gi --cpu=1 --concurrency=4 --timeout=300 --min-instances=1 \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT_ID|GOOGLE_CLOUD_LOCATION=global|MODEL=gemini-3.8-flash|ARTIFACT_BUCKET=$PROJECT_ID-hardware-ticket-photos|AGENT_ENGINE_ID=ENGINE_ID|AGENT_ENGINE_LOCATION=$REGION|REGION=$REGION|SERVICE_URL=$SERVICE_URL|SN_INSTANCE_URL=https://INSTANCE.service-now.com"

gcloud run services add-iam-policy-binding $SERVICE_NAME --project $PROJECT_ID --region $REGION \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role=roles/run.invoker
```

Why these flags:

| Flag | Reason |
|---|---|
| `--no-allow-unauthenticated` | Only Gemini Enterprise's service agent may call it (IAM). |
| `--min-instances=1` | No cold start on the first message. Set 0 to save cost (see [COSTS.md](COSTS.md)). |
| `--concurrency=4`, `--memory=1Gi` | Each request may hold a large inline photo in memory. |
| `--timeout=300` | A turn can chain several tools, ServiceNow calls and a photo read. |
| `^\|^` in `--set-env-vars` | Uses `\|` as the separator, so values may contain commas. |
| `SERVICE_URL` | The agent card advertises it; Gemini Enterprise posts to whatever the card says. |

To build the image separately (for example for a pipeline that deploys images, not source),
build the `Dockerfile` with Cloud Build or Docker, push it to Artifact Registry, and use
`gcloud run deploy --image` with the same flags.

## 3. Why not `adk deploy` or `agents-cli deploy`

Both are good defaults for ADK agents, but they don't fit this one:

- **Custom A2A server.** `app/server.py` builds its own A2A application around the ADK runner. A
  `before_agent` interceptor (`preprocess`) runs on every turn before the model: it resolves the
  user from the ServiceNow token, stages photos in Cloud Storage, turns button clicks into text,
  and handles the desktop/mobile question. A custom request converter puts identity and photo
  references into session state. The generated servers don't have these hooks.
- **Identity pass-through.** The user's ServiceNow token arrives in the `Authorization` header and
  must reach the code untouched. On Agent Runtime the platform replaces that header (measured), so
  `agents-cli deploy` to Agent Runtime loses the user. See
  [decision 0001](decisions/0001-cloud-run-not-agent-runtime.md).
- **Body size.** Photos arrive inline; the server raises the A2A body limit to 32 MB.
- **Agent card.** Built by the server from the profile, declaring A2UI v0.9 and v0.8, image input
  types and the Cloud Run URL.

`adk deploy cloud_run` would produce a Cloud Run service, but with ADK's own server instead of
`app/server.py`, so none of the above would run.

## Terraform

Not provided. Organizations that require infrastructure as code can model the same resources:
the APIs, the bucket, the `hardware-agent` service account and its three role bindings, the
Agent Runtime instance (created by `scripts/create_state_engine.py` today), the Cloud Run service
with the flags above, and the `run.invoker` binding. The image still has to be built from the
`Dockerfile`. Gemini Enterprise registration stays a console step (INSTALL step 7).
