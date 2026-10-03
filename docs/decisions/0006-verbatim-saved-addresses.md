# 0006. Save permanent delivery addresses verbatim, in their own scope

**Status:** Accepted (since Cloud Run revision 00026, 2026-09-30).

## Context

Memory Bank extracts memories from conversations and paraphrases them. A delivery address came
back as "a Marriott in Chicago": not a street address, and a hotel, which should never be reused.
Earlier, a remembered "Denver home office" from test data was applied to a real request without
asking.

## Decision

- The ship-to is always a street address: the one in ServiceNow, a saved one, or one the user
  typed.
- **Permanent** places (home, office) are saved on submit **verbatim**, one fact per address, in
  a separate Memory Bank scope: `app_name = hardware_replacement_addresses`, `user_id = email`
  (`memory.save_address`). A new address for the same label replaces the old one.
- **Temporary** places (hotels, events, trips) are used once and never saved.
- Saved addresses are only **offered**, as grey buttons on the review card. "Looks good" or
  "submit" never switches the address.
- Extracted conversation memories that mention shipping or delivery are filtered out of
  `memory.recall`, so the model never sees vague address memories.

## Consequences

- Users can pick "Ship to Home instead" in one tap, and the address is exactly what they typed.
- Saved addresses are personal data in Memory Bank. To delete a person's data, remove both
  scopes (see [PRIVACY.md](../PRIVACY.md)).
- The model can't apply an address on its own. The instruction says so, and evals check it.

## Alternatives

- **Rely on Memory Bank extraction.** Rejected: paraphrased, and can't tell a hotel from a home.
- **Write addresses back to the ServiceNow user record.** Not done: needs write access to
  `sys_user` that requesters don't have.
