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
| `ORGANIZATION_PROFILE` | | `config/organization.yaml` | Which profile to load (a file under `config/`; deploy passes it to Cloud Run) |
| `MESSAGES_FILE` | | `config/messages.yaml` | Which wording file to load (under `config/`) |
| `VISION_MODEL` | | `MODEL` | Model for reading photos only |
| `SERVICE_URL` | | set by `deploy.sh` | Public URL advertised in the agent card |

`.env` is read by the agent when run locally and by every script. It is never committed and never
copied into the container; `scripts/deploy.sh` passes the values to Cloud Run.

## Organization profile (`config/organization.yaml`)

Validated when the agent starts. A misspelled key or a bad value stops start-up with a message
naming the field. Two examples ship: `config/organization.yaml` (a hospital: today's demo) and
`config/examples/office.yaml` (a generic office).

> After changing the `agent` block (name, description, examples), deploy, then **edit the agent in Gemini
> Enterprise, paste the updated agent card and save** (no need to delete and re-add it). Everything else takes effect on
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
| `ticket_fields` | Extra incident fields set on every new ticket: fixed text, or placeholders filled per request: `{issue_key}` `{issue_value}` `{issue_label}` `{device_value}` `{device_type}` `{device_kind}` `{model_category}` `{department}` `{location}`. A field that comes out empty is left unset. Fields the agent manages itself (caller, category, description, impact, urgency, CI, watch list, state, correlation) can't be configured. If ServiceNow refuses a field for a user, the ticket gets a note asking the service desk to set it. Default: `contact_type: self-service`. |
| `device_values` | Device type (`laptop`, `desktop`, `monitor`, `phone`, `tablet`, `medical equipment`...) or model category name -> the value for `{device_value}`. Unlisted types write nothing. |
| `urgency_matrix` | The agent's urgency (`critical`/`high`/`normal`/`low`) -> incident `impact` and `urgency` (`"1"`-`"3"`); ServiceNow derives the priority from the pair. |

### `devices`
| Field | Meaning |
|---|---|
| `clinical_categories` | Model categories treated as medical equipment (repaired on site by the asset's support group). Empty for offices. |
| `asset_tag_hint` | How asset tags look, in plain words, so the photo reader reads the right number |
| `refresh_years`, `default_refresh_years` | Age (by device type) at which a personal device is refresh-eligible |
| `category_types` | Model category -> device type (`laptop`, `desktop`, `monitor`, `phone`, `tablet`, `other`) when your categories say it; unlisted categories are guessed from the category and model name |

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
| `servicenow_value` | What `{issue_value}` writes for this problem (default: the key); set by `import-issues` |

### `service`
| Field | Meaning |
|---|---|
| `personal_response_targets`, `equipment_response_targets` | Text per ServiceNow priority `"1"`..`"4"`, shown as Expected / Target |
| `safety_text` | What a person reporting a safety concern is told (and what the ticket records) |
| `recommendations` | Texts for each fulfilment recommendation; `{cost_center}` is filled in |

### `features`
Switch parts of the agent off; all are on by default.

| Field | Off means |
|---|---|
| `equipment_reporting` | Only people's own devices; shared and clinical equipment is sent to the service desk |
| `photo_analysis` | Photos are attached to the ticket as they are; no model reads labels or damage |
| `follow_open_tickets` | No "already reported" offer; every report is its own ticket |
| `saved_addresses` | Delivery addresses are neither remembered nor offered |
| `memory` | No recall of preferences or history across conversations |
| `ask_display_mode` | No desktop/mobile question; cards always (for web-only organizations) |

### `requester_changes`
What requesters may change on their own tickets themselves: `urgency`, `status` (reopen, hold,
resolve), `ship_to`, `cancel`. A change that is off is not attempted; it goes on the ticket as a note
asking the service desk to make it, and the user is told so.

## Wording (`config/messages.yaml`)

Every text the cards show (titles, buttons, field labels, the desktop/mobile question, the
confirmation) is a line in `config/messages.yaml`, by key:

```yaml
review.submit: Send to the service desk
done.title: "Ticket {number} is in"
display.ask: |-
  Which app are you using?

  1 = Phone

  2 = Computer
```

A text may use only the `{placeholders}` its default uses (for example `done.title` has `{number}`);
anything else is refused with the key named. Remove a line to get the built-in default
(`app/messages.py`). Check after editing:

```bash
uv run python -m app.messages
```

Problem labels, the safety text and response targets stay in `config/organization.yaml`.

## Your ServiceNow's own options

The agent has no ServiceNow credentials of its own, so it never reads choice lists at run time.
An admin checks the profile against the instance, and imports a choice list when the
organization wants exactly its own options (signs in like the seed tool):

```bash
uv run python scripts/sn_profile.py check                                   # every value vs. your instance
uv run python scripts/sn_profile.py choices subcategory --dependent hardware  # what a field allows
uv run python scripts/sn_profile.py import-issues subcategory --dependent hardware --group personal
```

`check` confirms the ticket category is a real category, the asset tables exist, every
`ticket_fields` field exists, and every value the profile can write (fixed values,
`device_values`, each problem's `servicenow_value`, the urgency matrix) is a valid choice.

`import-issues` prints an `issues:` group built from the choice list (labels and order from
ServiceNow, `servicenow_value` set, photo and urgency rules of matching existing entries kept).
Paste it into the profile and map it onto the ticket, e.g. `ticket_fields: {subcategory: "{issue_value}"}`.

**Example: "only show our hardware types".** The out-of-box `incident.subcategory` (dependent on
category `hardware`) has CPU, Disk, Keyboard, Memory, Monitor, Mouse. The hospital profile maps
device types onto it (`subcategory: "{device_value}"` with `laptop: cpu`, `monitor: monitor`...).
A custom field such as `u_hardware_type` works the same way.

## Recipes

**Change the button colour.** Set `branding.primary_color: "#0B5FFF"`, deploy.

**Rename a problem or add one.** Edit or add an entry under `issues.personal` / `issues.equipment`,
check with `uv run python -m app.profile`, deploy.

**Require a photo for keyboard problems.** `{key: keyboard_trackpad, ..., photo: required, photo_of: "the keyboard"}`.

**An office with no medical equipment.** Start from `config/examples/office.yaml`: `clinical_categories: []`.

**Use a different profile per environment.** `ORGANIZATION_PROFILE=config/examples/office.yaml` in that
environment's `.env`, then `./scripts/deploy.sh` (it passes the setting to Cloud Run; the file must be under `config/`).

Not configurable yet (see `docs/BACKLOG.md` section G3): showing your ServiceNow choice lists
directly, languages (one `messages.yaml` per language is the planned route).
