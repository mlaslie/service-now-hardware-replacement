# 0007. Every ticket change: attempt, read back, note what was refused

**Status:** Accepted.

## Context

ServiceNow drops fields a user may not write **without an error**. Seen on the test instance:
priority 2 requested, 3 assigned (the `incident.urgency` write ACL needs `sn_incident_write`); an
empty description for a user without roles; a ship-to "changed" that only added a note; a reopen
request silently dropped.

## Decision

Nothing is reported as done until it has been read back.

- All changes to an existing ticket go through one function, `_apply_changes`
  (`app/tools/tickets.py`): PATCH as the user, read the ticket back, compare each field.
- What stuck is reported as **changed**. What didn't is added to the ticket as a note asking the
  service desk to make the change, and the user is told plainly that it was requested, not done.
- The same rule covers creation (`app/tools/filing.py`): a dropped description goes into the
  first note; a priority lower than requested is explained and noted; configured
  `ticket_fields` that ServiceNow refused are listed in a note for the desk.
- A follower's change request always becomes a note. Only the reporter can change status,
  urgency or ship-to.

## Consequences

- The user is never told something changed when it didn't.
- Works with any role set. Fewer roles means more notes for the desk, not failures.
- One extra read per change.
- The instruction makes the model's sentence match the result exactly ("changed" vs
  "not_permitted_note_added").

## Alternatives

- **Trust the PATCH response.** Rejected: ServiceNow returns 200 with the field unchanged.
- **Require `itil` for everyone.** Rejected: licensed role; see [0008](0008-custom-requester-role.md).
