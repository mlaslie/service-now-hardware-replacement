# Rebuild on a new ServiceNow instance

Personal developer instances (PDIs) are reclaimed when unused, and any instance can be replaced. Moving the agent
to a new instance is one paste in ServiceNow and two commands, about 10 minutes, most of it waiting.

## Keep the instance you have

Since July 2026, ServiceNow reclaims a PDI that is **at least 90 days old** and has had **no explicit sign-in to the
instance in the last 10 days**. Signing in to the developer portal doesn't count, and neither do integrations,
scheduled jobs or API calls, including this agent's own traffic
([PDI reclamation rules](https://www.servicenow.com/community/developer-articles/servicenow-pdi-reclamation-rules-avoid-losing-access/ta-p/3572371)).
Sign in to the instance itself at least once a week; a calendar reminder is enough.

## What it does

| | Who | What |
|---|---|---|
| 1 | You, ServiceNow | Paste one background script: the requester role (`u_hardware_requester`, 13 access rules and the follow-only business rule) and the admin tools' OAuth client. It prints one `SEED_CLIENT=...` line. |
| 2 | `rebuild_instance.py run` | Everything else: local settings, the admin tools' secret, admin sign-in, profile and role checks, demo users, devices and equipment, the demo users' passwords, the permission checker, deploy, the Gemini Enterprise client and authorization, a smoke test and the demo run sheet. |

Not needed any more: creating OAuth clients by hand, copying the Gemini Enterprise client secret into the console,
deleting and re-adding the agent, and setting passwords with **Set Password**.

## Before you start

- `.env` has `GOOGLE_CLOUD_PROJECT`, `AGENT_ENGINE_ID`, and **`GE_APP_ID`** (your Gemini Enterprise app's ID; see
  [CONFIGURATION.md](CONFIGURATION.md)).
- `gcloud` is on your PATH and signed in (`gcloud auth login`), as an account that can deploy and manage Gemini
  Enterprise agents.
- The new instance is awake: open it in a browser and sign in as `admin`. Get the admin password from the
  developer portal (**Manage instance password**).

## 1. ServiceNow: one paste

```bash
uv run --group seed python scripts/rebuild_instance.py bootstrap https://NEW.service-now.com
```

It copies the script to the clipboard (and writes `seed/state/bootstrap_instance.js`). In ServiceNow, as `admin`:

1. User menu (top right) > **Elevate role** > tick **security_admin** > **Update**. Only an elevated session can
   create access rules.
2. Open `https://NEW.service-now.com/sys.scripts.do` (**System Definition > Scripts - Background**), paste, and
   click **Run script**.
3. Copy the last line it prints, `SEED_CLIENT=https://NEW.service-now.com|...|...`. It contains a client secret:
   paste it only into the next command, which asks for it with hidden input.

The script is safe to run again; it reuses what it already created.

## 2. Everything else

```bash
uv run --group seed python scripts/rebuild_instance.py run https://NEW.service-now.com
```

| Step | What happens | You |
|---|---|---|
| `config` | Points `.env` and `CLAUDE.local.md` (and `../servicenow-mcp`, if present) at the new instance, adds its name to `.pii-patterns`, sets the old seed manifest aside, and notes the change in `docs/HANDOFF.local.md` | |
| `secret` | Stores the `SEED_CLIENT` line in Secret Manager `servicenow-seed-oauth` | Paste the line |
| `login` | Admin sign-in for the admin tools (`sn_seed.py login`) | Click **Allow** in the browser |
| `check` | The role exists (`sn_custom_role.py status`); the profile's values exist on this instance (`sn_profile.py check`) | |
| `seed` | Demo users, their devices, the hospital equipment, the asset register PDF (`sn_seed.py set`, `report`) | |
| `passwords` | Sets each demo user's password and saves them in `~/.config/hw-seed/demo-passwords.json`, readable only by you, never printed. Repeat runs reuse them. | |
| `doctor` | Every agent action for every demo user (`sn_doctor.py --as ...`) | |
| `deploy` | `./scripts/deploy.sh` | Answer **y** (or pass `--yes`) |
| `gemini` | Creates the ServiceNow `Gemini Enterprise` OAuth client (or resets its secret), creates a new Gemini Enterprise authorization with that secret, and points the registered agent at it, with the live agent card (or registers the agent). The secret passes straight from the script to both systems. | |
| `verify` | Smoke test of the deployed agent; rebuilds `demo/DEMO.local.html` | |

It stops at the first failure. Fix the cause, then continue from that step:

```bash
uv run --group seed python scripts/rebuild_instance.py run https://NEW.service-now.com --from seed
```

or run a single step with `--only STEP`. Every step is safe to repeat. The `gemini` step rotates the Gemini
Enterprise client's secret each time, so everyone authorizes again on their next message.

## 3. Try it

Open the agent in Gemini Enterprise in a browser that is **not** signed in to ServiceNow as admin, send "my
laptop screen is cracked", and authorize as a demo user (passwords: `cat ~/.config/hw-seed/demo-passwords.json`).
The run sheet `demo/DEMO.local.html` has the new instance's links.

## Afterwards

- The `gemini` step prints the previous Gemini Enterprise authorization's name and a `curl` command to delete it;
  it isn't used any more.
- A Gemini Enterprise **ServiceNow connector** (data store) for the old instance, if you had one, still points
  there. Remove it, or create a new one, in the console.
- The MCP evaluation probe (`../servicenow-mcp`), if you use it: `./scripts/deploy_probe.sh` there, and update its
  data store's authorization.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `config`: "isn't answering" | The instance is asleep or still being set up: open it in a browser, wait, rerun |
| `secret`: "the line is for https://..." | You copied the line from another instance's output; run the bootstrap script on the new one |
| `check`: "Role u_hardware_requester not found" | The background script didn't run elevated; elevate to security_admin and run it again |
| `check`: profile values not found | Set the values that exist here in `config/organization.yaml` (`sn_profile.py choices <field>` lists them), then `--from check` |
| `gemini`: "Set GE_APP_ID" | Add the app ID to `.env` |
| `gemini`: listing or updating the agent fails with 403 | Your gcloud account needs Discovery Engine admin rights on the project |
| Gemini Enterprise asks to authorize again and again | ServiceNow rejected the token exchange: run `--only gemini` again, which sets a fresh secret on both sides |
