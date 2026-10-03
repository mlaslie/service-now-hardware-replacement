# 0002. Call ServiceNow with each user's own OAuth token

**Status:** Accepted.

## Context

Tickets must be filed and read as the real person: the right caller, their devices, their
department, and only their tickets. Early attempts with a shared integration account failed on
the test instance: basic auth (three users) and client-credentials tokens (two clients) both got
401 from the Table API. Authorization-code tokens worked, the same flow the Gemini Enterprise
ServiceNow connector uses.

## Decision

The agent's Gemini Enterprise authorization is a **ServiceNow OAuth client (authorization code)**,
scope `useraccount`. Each user signs in once. Gemini Enterprise forwards their token on every
message. The agent:

- resolves who they are from the token on every turn (`app/identity.py`);
- holds the token in a request-scoped `ContextVar` (`servicenow.user_token`), never in session
  state, memory or logs;
- scopes every ticket query to `caller_id = me OR watch_list LIKE me` and the configured
  category (`app/servicenow.py`).

The agent itself stores **no ServiceNow secret**.

## Consequences

- ServiceNow's own roles and ACLs decide what each person can do. Requesters often lack `itil`,
  so fields can be silently dropped; see [0007](0007-attempt-verify-note.md) and
  [0008](0008-custom-requester-role.md).
- The model can't act for someone else: the requester comes from the token, never a tool argument.
- If a user authorizes while their browser is signed in to ServiceNow as an admin, the agent acts
  as the admin. Tell users to authorize as themselves (a private window helps). Single sign-on
  between Google and ServiceNow removes this risk.
- Each authorization resource belongs to one agent. In the console, changing it means deleting
  and re-adding the agent.
- The agent can't read ServiceNow choice lists at run time. An admin checks the profile with
  `scripts/sn_profile.py` instead.

## Alternatives

- **Basic auth or client credentials with a service account.** Failed on the test instance (401),
  and would act as one account for everyone.
- **Google identity mapped to a ServiceNow user by email.** Needs a trusted service account in
  ServiceNow plus impersonation, and loses per-user ACLs.
