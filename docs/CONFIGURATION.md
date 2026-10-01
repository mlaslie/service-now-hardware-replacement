# Configuration

Two files, two purposes:

| File | What it holds | Who edits it | Committed? |
|---|---|---|---|
| `.env` (from `.env.example`) | **Where** the agent runs and what it connects to: Google Cloud project, region, ServiceNow instance, model | Whoever deploys | No |
| `config/organization.yaml` | **What** the agent says and does for your organization: names, colour, problem choices, rules, texts | The service owner | Yes |

Check both at any time:

```bash
uv run python -m app.profile            # validates config/organization.yaml and prints a summary
./scripts/deploy.sh                     # stops early, naming any missing setting
```

## Deployment settings (`.env`)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | yes | | Project that runs the agent |
| `SN_INSTANCE_URL` | yes | | `https://<name>.service-now.com` |
| `AGENT_ENGINE_ID` | yes | | Agent Runtime instance for conversations and memory; `scripts/setup.sh` creates it and writes this |
| `REGION` | | `us-central1` | Cloud Run, bucket and Agent Runtime region |
| `SERVICE_NAME` | | `hardware-replacement-agent` | Cloud Run service name |
| `MODEL` | | `gemini-3.8-flash` | Model for chat and photos (`VISION_MODEL` overrides photos only) |
| `GOOGLE_CLOUD_LOCATION` | | `global` | Where the model is served |
| `AGENT_ENGINE_LOCATION` | | `REGION` | Agent Runtime instance location |
| `ARTIFACT_BUCKET` | | `<project>-hardware-ticket-photos` | Bucket for users' photos |
| `ORGANIZATION_PROFILE` | | `config/organization.yaml` | Which profile to load |
| `SERVICE_URL` | | set by `deploy.sh` | Public URL advertised in the agent card |

`.env` is read by the agent when run locally and by every script. It is never committed and never
copied into the container; `scripts/deploy.sh` passes the values to Cloud Run.

## Organization profile (`config/organization.yaml`)

Validated when the agent starts. A misspelled key or a bad value stops start-up with a message
naming the field. Two examples ship: `config/organization.yaml` (a hospital: today's demo) and
`config/examples/office.yaml` (a generic office).

> After changing the `agent` block (name, description, examples), **delete and re-add the agent in
> Gemini Enterprise**: it keeps the agent card from registration. Everything else takes effect on
> the next deploy.

### `organization`
Your organization's name (shown in logs and the check summary).

### `agent`
| Field | Meaning |
|---|---|
| `name` | Agent name in Gemini Enterprise |
| `persona` | The opening of the agent's instructions: who it helps, what it covers, how it talks. The routing rules stay in code so a customization can't break the flow. |
| `description`, `skill_name`, `skill_description`, `examples` | Agent card text shown in Gemini Enterprise |

### `branding`
| Field | Meaning |
|---|---|
| `primary_color` | `#RRGGBB` colour of forward buttons (Submit, Yes, device choices...). Measured in the Gemini Enterprise web app: it applies to primary buttons only; other buttons stay grey; fonts are Gemini Enterprise's; the mobile app shows text. |

### `servicenow`
| Field | Meaning |
|---|---|
| `ticket_category` | `incident.category` set on every ticket. Users only ever see tickets in this category. |
| `asset_tables` | Tables searched for devices and equipment (default `alm_hardware`). Add a clinical device table if medical equipment lives elsewhere. |

### `devices`
| Field | Meaning |
|---|---|
| `clinical_categories` | Model categories treated as medical equipment (repaired on site by the asset's support group). Empty for offices. |
| `asset_tag_hint` | How asset tags look, in plain words, so the photo reader reads the right number |
| `refresh_years`, `default_refresh_years` | Age (by device type) at which a personal device is refresh-eligible |

### `issues`
Two lists: `personal` (devices assigned to a person) and `equipment` (shared and clinical). Each
entry is one button:

| Field | Meaning |
|---|---|
| `key` | Stable id stored on the request (`lowercase_with_underscores`). A key in both lists must have the same label. |
| `label` | Button text |
| `photo` | `required`, `recommended`, `optional` or `none` |
| `photo_of` | What to photograph (required unless `photo: none`) |
| `min_urgency` | Urgency is raised to at least this (`low`/`normal`/`high`/`critical`) |
| `replace` | Personal devices: the problem alone justifies a replacement |
| `safety` | Shows `service.safety_text`; pair with `min_urgency: critical` |
| `recommendation` | A fixed recommendation for this problem (e.g. remote diagnostics first) |

### `service`
| Field | Meaning |
|---|---|
| `personal_response_targets`, `equipment_response_targets` | Text per ServiceNow priority `"1"`..`"4"`, shown as Expected / Target |
| `safety_text` | What a person reporting a safety concern is told (and what the ticket records) |
| `recommendations` | Texts for each fulfilment recommendation; `{cost_center}` is filled in |

## Recipes

**Change the button colour.** Set `branding.primary_color: "#0B5FFF"`, deploy.

**Rename a problem or add one.** Edit or add an entry under `issues.personal` / `issues.equipment`,
check with `uv run python -m app.profile`, deploy.

**Require a photo for keyboard problems.** `{key: keyboard_trackpad, ..., photo: required, photo_of: "the keyboard"}`.

**An office with no medical equipment.** Start from `config/examples/office.yaml`: `clinical_categories: []`.

**Use a different profile per environment.** `ORGANIZATION_PROFILE=config/examples/office.yaml` in `.env`.

Not configurable yet (see `docs/BACKLOG.md` section G3): showing your ServiceNow choice lists
directly, custom ticket fields, the desktop/mobile question wording, languages.
