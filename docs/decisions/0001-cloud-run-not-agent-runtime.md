# 0001. Run the agent on Cloud Run, not Agent Runtime

**Status:** Accepted. Measured with a probe agent (September 2026).

## Context

The agent must act in ServiceNow as the person who is chatting. Gemini Enterprise signs each
user in to ServiceNow through the agent's authorization resource and sends that user's token
with every A2A message, in the `Authorization` header.

Agent Runtime can host A2A agents (agents-cli 1.x allows it). A probe agent was deployed there
with an authorization attached. Its gateway replaced `Authorization` with its own token, with the
same subject for every caller. The user's credential never reached the agent code.

## Decision

The agent runs on **Cloud Run** as a custom A2A server (`app/server.py`). Cloud Run IAM checks
`x-serverless-authorization` (Gemini Enterprise's Discovery Engine service agent, which holds
`roles/run.invoker` on the service). The `Authorization` header is passed through untouched and
read in `app/identity.py`.

Agent Runtime is still used, but only for its managed **Sessions** and **Memory Bank**. The
instance runs no code (`scripts/create_state_engine.py`).

## Consequences

- Identity works: every turn resolves the user from their own token
  (`/api/now/ui/user/current_user`).
- We run our own A2A server, so `adk deploy` and `agents-cli deploy` don't fit
  (see [DEPLOY_OPTIONS.md](../DEPLOY_OPTIONS.md)).
- Cloud Run settings are ours to manage: minimum instances, concurrency, body size (32 MB for
  inline photos), timeouts.
- Two Google resources instead of one: the Cloud Run service and the Agent Runtime instance.

## Alternatives

- **A2A agent on Agent Runtime.** Rejected: no user identity (measured).
- **ADK agent registered directly on Agent Runtime**, reading the Gemini Enterprise
  authorization token from session state. Documented by Google but never tested here. It would
  also lose the custom A2A server (photo staging, click rewriting, display mode).
- **A shared ServiceNow service account.** Rejected; see [0002](0002-per-user-servicenow-oauth.md).
