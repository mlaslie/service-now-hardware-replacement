# ServiceNow roles and permissions

The agent calls ServiceNow **as the signed-in user**, with their own OAuth token. It has no
service account in ServiceNow, so what each person can do is exactly what their ServiceNow roles
allow. This page says which roles are needed, measured on a real instance, and how to check your
own users.

## Admin: one-time setup

| Task | Role | Where |
|---|---|---|
| Create the OAuth client Gemini Enterprise uses (authorization code) | `admin` (or `oauth_admin`) | System OAuth > Application Registry |
| Create the OAuth client for the seed and doctor scripts (redirect `http://localhost:8765/callback`) | `admin` | same |
| Grant roles to requesters, create groups and departments | `admin` or `user_admin` | User Administration |
| Load demo data (`seed/sn_seed.py`) | `admin` | signs in through the browser |
| Run the permission checker (`scripts/sn_doctor.py`), which impersonates users | `admin` (impersonation) | signs in through the browser |

## Requesters: what each role set allows

Measured with `scripts/sn_doctor.py --matrix`, which creates temporary users with each role set,
impersonates them, performs every call the agent makes and reads back what ServiceNow stored.
Your instance's ACLs may differ: run it yourself (below).

<!-- The table below is docs/role-matrix.md, generated on a ServiceNow developer instance on 2026-09-30. -->
| What the agent does | Used for | no roles (typical employee) | itil | itil + sn_incident_write | itil + sn_incident_write + asset |
|---|---|---|---|---|---|
| Who am I | knowing who is asking (every turn) | yes | yes | yes | yes |
| Read own group memberships | recognising equipment the user's group supports | no | yes | yes | yes |
| List own devices | the device list (step 1) | yes | yes | yes | yes |
| Find a device by tag | typed or photographed asset tags and serials | yes | yes | yes | yes |
| List a department's equipment | finding equipment by description ("the MRI") | yes | yes | yes | yes |
| Read the device record (CI) | linking tickets to equipment; duplicate detection | no | yes | yes | yes |
| Create a ticket | filing a request | partly (drops urgency, description) | yes | yes | yes |
| Add a note | notes, and requests the user can't make directly | yes | yes | yes | yes |
| Read notes | "show me the notes" | yes | yes | yes | yes |
| Attach a photo | copying the user's photos onto the ticket | yes | yes | yes | yes |
| Change status (reopen) | "move it back to in progress" | no | yes | yes | yes |
| Change urgency | "make it urgent" | no | yes | yes | yes |
| Change ship-to | "ship it to my house" | no | yes | yes | yes |
| See someone else's open ticket | "this is already reported" on shared equipment | no | yes | yes | yes |
| Follow someone else's ticket | joining an open ticket on shared equipment | no | yes | yes | yes |
| Cancel own ticket | "cancel it, I found a spare" | no | yes | yes | yes |

**Reading it:**
- **`itil` alone is enough for everything the agent does** on this instance; `sn_incident_write`
  and `asset` add nothing on top of it.
- **With no roles** a person can still report problems: the ticket is created with their device,
  photos and notes. What ServiceNow refuses, the agent handles:

| Refused | What the agent does |
|---|---|
| Description, urgency on the new ticket | Puts the details in the first note; explains the priority ServiceNow set |
| Status, urgency, ship-to changes, cancel | Adds a note asking the service desk, and tells the user plainly |
| Group memberships | Treats "supported by your group" as "not confirmed" (the ticket says so) |
| Device records (CI) | Files without the CI link when the asset has none; no "already reported" check |
| Others' tickets | No "already reported" offer: each reporter files their own ticket |

> **Licensing.** `itil` is a fulfiller role in ServiceNow and is usually licensed per user.
> Granting it to every employee just for this agent is rarely the right answer. For production,
> plan either (a) no roles for requesters, accepting the table above, or (b) a small scripted REST
> API or Service Catalog item that sets ticket fields server-side (`docs/BACKLOG.md` F2, D4, D5).
> Check with your ServiceNow owner.

## A custom role instead of `itil`: `u_hardware_requester`

A role that allows exactly what the agent does for a requester, on hardware tickets, and nothing else:

| Access rule | Condition | Allows |
|---|---|---|
| `incident` write | hardware, and mine, followed, or open on a device record | updating own/followed tickets; following open equipment tickets |
| `incident.description`, `.urgency`, `.impact`, `.state`, `.close_code`, `.close_notes`, `.hold_reason` write | hardware and mine | details, ship-to, urgency, reopen, hold, cancel: own tickets only |
| `incident.watch_list` write | hardware, and mine or open on a device record | following open equipment tickets |
| `incident.comments` write | hardware, and mine, followed or open on a device record | notes |
| `incident` read | hardware, and mine, followed, or open on a device record | own tickets; "already reported" on equipment |
| `cmdb_ci` read | (role only) | linking tickets to equipment |
| `sys_user_grmember` read | the row is mine | "supported by your group" |

Followers can add notes and follow, but only the reporter can change details or status.

An access rule can't see the value being written, so a **business rule** (`u_hardware_requester: follow
only`, before update on `incident`) checks watch list changes on other people's tickets: a requester
without `itil` may add or remove only themselves, never anyone else. The create script installs it and
`sn_custom_role.py status` checks it.

**Measured** (2026-10-01, `sn_doctor.py --matrix --persona none --persona u_hardware_requester --persona itil`,
temporary users, everything deleted afterwards): **`u_hardware_requester` passes all 16 checks, the same as
`itil`**, where a user with no roles fails 8.

| What the agent does | no roles | `u_hardware_requester` | `itil` |
|---|---|---|---|
| Who am I; own devices; find by tag; department equipment; add/read notes; attach photo | yes | yes | yes |
| Read own group memberships | no | yes | yes |
| Read the device record (CI) | no | yes | yes |
| Create a ticket | partly (drops urgency, description) | yes | yes |
| Change status, urgency, ship-to; cancel own ticket | no | yes | yes |
| See and follow someone else's open ticket | no | yes | yes |

Note: ServiceNow rewrites a new rule's description ("Allow write for ..."), so the scripts find
their rules through the rules' link to the role, not by description.

**Create it** (once; ServiceNow lets only a session elevated to `security_admin` create access rules,
so this is a background script rather than an API call):

1. In ServiceNow as an admin: user menu > **Elevate role** > `security_admin`.
2. **System Definition > Scripts - Background**: paste `scripts/servicenow/create_hardware_requester_role.js`, **Run script**.
   It prints one line per rule. Safe to run again (run it again after updating this repo to apply rule changes).
   `remove_hardware_requester_role.js` undoes everything, and keeps the role if any rule could not be deleted.
3. Check, grant, and measure:
   ```bash
   uv run python scripts/sn_custom_role.py status
   uv run python scripts/sn_custom_role.py grant jane.doe
   uv run python scripts/sn_doctor.py --matrix --persona none --persona u_hardware_requester --persona itil
   ```

If your ticket category isn't `hardware`, change `CATEGORY` at the top of the script to match
`servicenow.ticket_category` in `config/organization.yaml`.

> Licensing still applies: whether a role that can change incidents counts as a licensed fulfiller
> role is decided by your ServiceNow contract, not the role's name. Confirm with your account team.

## Check your own users

Sign in once as an admin (the same sign-in as the seed tool), then:

```bash
uv run --group seed python seed/sn_seed.py login                 # browser sign-in as an admin
uv run python scripts/sn_doctor.py --as jane.doe                  # one user; creates and deletes a test ticket
uv run python scripts/sn_doctor.py --as jane.doe --read-only      # production: no test ticket
uv run python scripts/sn_doctor.py --matrix --markdown docs/role-matrix.md   # re-measure the table above
```

Sample output:

```
jane.doe  (roles: none)
  PASS  Who am I                         name, email, department, address readable
  FAIL  Read own group memberships       access denied
        -> sys_user_grmember read: needs itil, group_viewer, sn_cmdb_user or user_admin.
  PART  Create a ticket                  dropped: urgency, description
        -> incident.urgency write: sn_incident_write (priority falls back to the default); ...
```

Three **Limit** checks try what a requester should not be able to do: edit someone else's ticket,
remove other followers, read a non-hardware ticket. PASS means ServiceNow refused; PART means the role
allows it (expected for `itil`, a fulfiller role; for `u_hardware_requester` it means the role's rules or
business rule aren't installed). Anything changed is put back.

What it writes: one test ticket per user checked (plus two created as admin to test "someone
else's ticket" and a non-hardware ticket), deleted at the end. `--matrix` also creates temporary users (no password) with a
temporary device and group membership, all deleted at the end. Impersonation appears in
ServiceNow's logs.
