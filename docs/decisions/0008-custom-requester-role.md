# 0008. Offer a custom role `u_hardware_requester` instead of `itil`

**Status:** Accepted for the reference setup. Licensing must be confirmed per customer.

## Context

The agent acts with each user's own ServiceNow permissions ([0002](0002-per-user-servicenow-oauth.md)).
Measured with `scripts/sn_doctor.py --matrix`:

- `itil` alone covers every call the agent makes.
- With **no roles**, a person can still file a ticket (details go into a note), but can't change
  status, urgency or ship-to, cancel, read device records, read group memberships, or see and
  follow someone else's open ticket.

`itil` is a fulfiller role, usually licensed per user. Granting it to every employee for this
agent is rarely right.

## Decision

Ship a custom role, **`u_hardware_requester`**, that allows exactly what the agent needs on
tickets in the configured category, and nothing else:

- Access rules: read and write own tickets (details, urgency, state, close and hold fields);
  notes and following on own, followed, or open equipment tickets (`cmdb_ci` set); read
  `cmdb_ci`; read own `sys_user_grmember` rows.
- A **business rule** (`u_hardware_requester: follow only`, before update on `incident`): an
  access rule can't see the value being written, so this rule checks watch list changes on other
  people's tickets. A requester without `itil` may add or remove only themselves. Tightened on
  2026-10-02 (review finding H0.6).
- Installed by a background script an admin runs once, elevated to `security_admin`
  (`scripts/servicenow/create_hardware_requester_role.js`). ServiceNow only lets an elevated
  session create access rules, so this is not done through the API. `remove_hardware_requester_role.js`
  undoes it. `scripts/sn_custom_role.py status|grant|revoke` checks and assigns it.

Measured 2026-10-01: `u_hardware_requester` passes all 16 checks, the same as `itil`. The
`sn_doctor` Limit checks (edit someone else's ticket, remove other followers, read a non-hardware
ticket) confirm what it can't do.

## Consequences

- Requesters get every feature without `itil`.
- The script must be **re-run after updating the repo** when the rules change (it is safe to
  re-run). See [RUNBOOK.md](../RUNBOOK.md).
- The script assumes category `hardware`. If `servicenow.ticket_category` differs, change
  `CATEGORY` at the top of the script.
- **Licensing still applies.** Whether a role that can change incidents counts as a licensed
  fulfiller role depends on the ServiceNow contract, not the role's name. Confirm with the
  account team.
- ServiceNow rewrites new rule descriptions, so the scripts find their rules through the link to
  the role, not by description.

## Alternatives

- **`itil` for requesters.** Works; licensing cost.
- **No roles.** Works with limits (see [ROLES.md](../ROLES.md)).
- **A Service Catalog item or scripted REST API** that sets fields server-side. Likely the best
  production answer; not built (BACKLOG D4, D5, F2).
