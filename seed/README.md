# Demo data

`sn_seed.py` loads demo people, their devices and (for a hospital) shared and clinical equipment
into a ServiceNow instance, and removes exactly what it created. Sign in once as an admin:

```bash
uv run --group seed python seed/sn_seed.py login
uv run --group seed python seed/sn_seed.py set seed/users.json                 # hospital: people + equipment
uv run --group seed python seed/sn_seed.py set seed/users.json --no-equipment  # an office: people + own devices
uv run --group seed python seed/sn_seed.py clear-tickets --yes                 # between demo runs
uv run --group seed python seed/sn_seed.py reset --yes                         # remove everything it created
```

Every command that deletes is a dry run until `--yes`. `reset` and `clear-tickets` delete the agent's
tickets for people who existed before the seed, and every ticket of people the seed created.

## The three files (templates)

| File | What it holds | Adapt it by |
|---|---|---|
| `users.example.json` | Demo people: name, email, title, department, cost center, manager, location, groups, roles, `demo_role` (shown on the run sheet). Copy to `users.json` (not committed) and use your own test accounts. | Changing people, departments and locations; `roles` (`itil`, or `u_hardware_requester`, see docs/ROLES.md) |
| `catalog.json` | Standard kit issued to every seeded person (`hardware`, `software`): model, `category` (matched to ServiceNow model categories), `age_days`, `warranty_years`. A person can take a subset with an `items` list. | Your standard laptop, monitor, phone; ages that show refresh eligibility |
| `equipment.json` | Shared and clinical equipment: `site`, model `categories`, support `groups`, `departments`, and `equipment` items (asset tag, model, category, department, room, support group). | Your own equipment kinds; for an office use `--no-equipment` |

Model categories listed in the profile's `devices.clinical_categories` are treated as clinical
equipment (repaired on site by their support group).

Keep personal data out of committed files: `users.json` is ignored by git; committed examples use
Jane Doe and John Doe at example.com.
