# Customize

Recipes for common changes, without editing Python. Each one: the YAML, then how to check it.
Every field is described in [CONFIGURATION.md](CONFIGURATION.md); the schema is `app/profile.py`.

The checks used below:

```bash
uv run python -m app.profile                    # the profile is valid; prints a summary
uv run python -m app.profile path/to/other.yaml # check another file
uv run --group seed python seed/sn_seed.py login  # once, as a ServiceNow admin (for the next line)
uv run python scripts/sn_profile.py check       # every value the profile writes exists in ServiceNow
```

Then deploy (`./scripts/deploy.sh`). After changing the problem choices or the persona, also run
the model-routing checks: `uv run python evals/run.py --repeat 2`.

A misspelled key or a bad value stops the agent at start-up with the field named, so always run
`python -m app.profile` before deploying. `deploy.sh` runs it too.

## Change the button colour

```yaml
branding:
  primary_color: "#0B5FFF"     # #RRGGBB
```

Check: `uv run python -m app.profile` prints `brand colour #0B5FFF`. After deploying, start a new
conversation in the web app.

Measured limits: Gemini Enterprise applies it to primary (forward) buttons only. Other buttons stay
grey, fonts are Gemini Enterprise's, and the mobile app shows text.

## Rename a problem, or add one with a required photo

Edit or add an entry under `issues.personal` (devices assigned to a person) or `issues.equipment`
(shared and medical equipment). The order is the button order.

```yaml
issues:
  personal:
    - {key: cracked_screen, label: Screen cracked or damaged, photo: required,
       photo_of: "the screen, switched on if possible, so the cracks are visible",
       min_urgency: high, replace: true}
    - {key: docking_station, label: Dock or charger, photo: required,
       photo_of: "the dock or charger, including its label"}
```

Rules the check enforces:
- `key` is `lowercase_with_underscores`, unique in its list. Don't change the key of an existing
  problem lightly: it is stored on requests and evals refer to it.
- `photo: required | recommended | optional | none`; anything but `none` needs `photo_of`.
- A key in both lists must have the same label and rules.

Check: `uv run python -m app.profile` lists the problem keys. Then `evals/run.py`. The photo
reader is told the problem keys automatically.

## Change the wording on the cards

Card texts and the desktop/mobile question are in `config/messages.yaml`, by key. Change a line;
remove it to get the default. Each text may use only the `{placeholders}` its default uses.

```yaml
review.title: Check your request
confirm_device.yes: Yes, this one
```

Check: `uv run python -m app.messages`. Every key and its default is in `app/messages.py`.

## Show only our hardware types

Use your ServiceNow choice list as the problem list, and write the chosen value on the ticket.

1. See what a field allows (here the out-of-box `incident.subcategory` under category `hardware`;
   a custom field such as `u_hardware_type` works the same way):
   ```bash
   uv run python scripts/sn_profile.py choices subcategory --dependent hardware
   ```
2. Print an `issues:` group built from it. Labels and order come from ServiceNow, each entry gets
   `servicenow_value`, and the photo and urgency rules of matching entries you already have are
   kept:
   ```bash
   uv run python scripts/sn_profile.py import-issues subcategory --dependent hardware --group personal
   ```
3. Paste the output over `issues.personal`. Add `photo`, `photo_of` and `min_urgency` where
   needed.
4. Map the problem onto the field:
   ```yaml
   servicenow:
     ticket_fields:
       contact_type: self-service
       subcategory: "{issue_value}"      # the chosen problem's servicenow_value
   ```

Check: `uv run python -m app.profile`, then `uv run python scripts/sn_profile.py check`: every
`servicenow_value` must be a valid choice. One field gets one mapping: if `subcategory` now holds
the problem, don't also map `{device_value}` onto it (next recipe).

## Map device types to a subcategory

Write a value per device type, instead of per problem. The hospital profile does this.

```yaml
servicenow:
  ticket_fields:
    subcategory: "{device_value}"
  device_values:              # device type, or a model category name -> value
    laptop: cpu
    desktop: cpu
    monitor: monitor
    keyboard: keyboard
    mouse: mouse
    Imaging Equipment: imaging   # a model category; must be a valid choice in your instance
```

Device types for inventory devices: laptop, desktop, monitor, phone, tablet, medical equipment,
other. The device type is looked up first, then the model category name. A device not listed
writes nothing (the field is left unset).

Check: `uv run python scripts/sn_profile.py check` reports any value that isn't a choice.

## Set extra ticket fields

Any incident field, as fixed text or a template:

```yaml
servicenow:
  ticket_fields:
    contact_type: self-service
    u_reported_via: "Hardware agent ({device_kind})"
    u_building: "{location}"
```

Placeholders: `{issue_key}` `{issue_value}` `{issue_label}` `{device_value}` `{device_type}`
`{device_kind}` `{model_category}` `{department}` `{location}`.

- A field that comes out empty is left unset.
- Fields the agent sets itself can't be configured: `caller_id`, `category`,
  `short_description`, `description`, `impact`, `urgency`, `cmdb_ci`, `watch_list`, `state`,
  `correlation_id`, `correlation_display`.
- If ServiceNow refuses a field for a user (no write access), the ticket gets a note asking the
  service desk to set it.

Check: `uv run python -m app.profile` rejects unknown placeholders and reserved fields.
`scripts/sn_profile.py check` confirms each field exists, and that choice fields get valid values.

## Change response targets

The text shown as Expected / Target on the review and confirmation cards, per ServiceNow
priority (1 = critical ... 4 = low). All four are required, for both lists.

```yaml
service:
  personal_response_targets:
    "1": Same day
    "2": Next business day
    "3": 2-3 business days
    "4": Within a week
  equipment_response_targets:
    "1": Response within 30 minutes
    "2": Same day
    "3": Next business day
    "4": Within 3 business days
```

The priority itself comes from ServiceNow, derived from impact and urgency. To change which
impact and urgency each agent urgency sends, edit `servicenow.urgency_matrix` (values `"1"` to
`"3"`).

Check: `uv run python -m app.profile`.

## Office or hospital

Two profiles ship:

| File | For |
|---|---|
| `config/organization.yaml` | A hospital (the demo): medical equipment categories, safety concern, clinical persona |
| `config/examples/office.yaml` | A generic office: no medical equipment (`clinical_categories: []`), printers and room displays as shared equipment |

For an office:

```bash
cp config/examples/office.yaml config/organization.yaml
uv run python -m app.profile
```

Then edit the `organization`, `agent` and `branding` blocks. The `agent` block is the agent card:
after deploying, edit the agent in Gemini Enterprise and paste the new card
([RUNBOOK.md](RUNBOOK.md#update-the-agent-card)).

## A different profile per environment

`ORGANIZATION_PROFILE` names the profile file (path relative to the repo root):

```bash
# .env
ORGANIZATION_PROFILE=config/examples/office.yaml
```

Check: `uv run python -m app.profile` (it reads `.env`), and
`uv run python scripts/sn_profile.py check --profile config/examples/office.yaml`.

`./scripts/deploy.sh` passes `ORGANIZATION_PROFILE` (and `MESSAGES_FILE`, `VISION_MODEL`) to Cloud Run
when set. The file must be under `config/`, the only configuration folder in the image; the script
stops if it isn't.

## Not configurable yet

See `docs/BACKLOG.md` section G3: a Service Catalog item instead of an incident, routing by device
type, languages. Switching features off and limiting what requesters may change themselves are in the
profile now (`features`, `requester_changes`; see the recipes below and `docs/CONFIGURATION.md`).

## Switch a feature off

```yaml
# config/organization.yaml
features:
  equipment_reporting: false   # people's own devices only
  ask_display_mode: false      # web only: no desktop/mobile question
```

Check: `uv run python -m app.profile`, deploy, then try it: an equipment tag now gets "contact the
service desk", and the first message goes straight to the device list.

## Let the service desk make some changes

```yaml
requester_changes:
  urgency: false   # "make it urgent" becomes a note asking the desk
  cancel: false
```

The user is told the request was sent to the service desk, and the ticket gets a note listing what
they asked for. Check: ask the agent to raise the urgency of a test ticket and read its notes.

## Offer a quick check before filing

```yaml
issues:
  personal:
    - key: wont_power_on
      label: Won't turn on
      self_help:
        - Plug in the charger and wait 15 minutes
        - Hold the power button for 10 seconds
```

Before filing, the user sees the checks with **Still not working** and **That fixed it**. A fix files
nothing (logged as `self_help_fixed`); otherwise the ticket says "Already tried: ...". Never shown for
equipment or safety concerns. Check: report "my laptop won't turn on".

