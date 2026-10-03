# Decision records

One short page per design decision: the context, what was decided, what follows from it, and
what else was considered. The reasoning comes from the project history in
[HANDOFF.md](../HANDOFF.md) and the measurements in [BACKLOG.md](../BACKLOG.md).

| # | Decision | Status |
|---|---|---|
| [0001](0001-cloud-run-not-agent-runtime.md) | Run the agent on Cloud Run, not Agent Runtime | Accepted (measured) |
| [0002](0002-per-user-servicenow-oauth.md) | Call ServiceNow with each user's own OAuth token | Accepted |
| [0003](0003-deterministic-cards.md) | The model never writes UI JSON; tools stage cards | Accepted |
| [0004](0004-a2ui-v09-with-v08-fallback.md) | A2UI v0.9 cards, translated to v0.8 when a client asks | Accepted |
| [0005](0005-desktop-or-mobile-question.md) | Ask "Desktop or Mobile App?" on the first turn | Accepted (measured) |
| [0006](0006-verbatim-saved-addresses.md) | Save permanent delivery addresses verbatim, in their own scope | Accepted |
| [0007](0007-attempt-verify-note.md) | Every ticket change: attempt, read back, note what was refused | Accepted |
| [0008](0008-custom-requester-role.md) | Offer a custom role `u_hardware_requester` instead of `itil` | Accepted, licensing to confirm per customer |

## Adding a record

Copy any page, give it the next number, and keep the four headings: **Context**, **Decision**,
**Consequences**, **Alternatives**. Say what was measured and when. If a later decision replaces
one, change its status to "Replaced by NNNN" rather than deleting it.
