# 0005. Ask "Desktop or Mobile App?" on the first turn

**Status:** Accepted. Measured 2026-09-26 and again 2026-09-30. Re-test when the mobile app changes.

## Context

The Gemini Enterprise mobile app can use A2A agents, forwards the user's ServiceNow token, and
can send photos. But any reply containing A2UI shows a red "Response contains unsupported
content" box, for v0.8 and v0.9 alike.

The agent can't tell the apps apart. Measured: web and mobile send identical requests: the same
`X-A2A-Extensions`, the same `a2uiClientCapabilities` (mobile advertises catalogs it can't
render), the same user agent, no distinguishing metadata.

## Decision

The first reply of every conversation asks:

> Are you on the Gemini Enterprise Desktop or Mobile App? 1 = Mobile App, 2 = Desktop/Browser

- The answer is stored in session state (`ui_mode`). The first message is kept and replayed once
  they answer, so nothing is lost.
- **Mobile (text mode):** each card is rendered as markdown with numbered options
  (`cards.to_text`). A number, or the option's words, acts as a click (`inbound.display_step`).
- **Desktop (cards mode):** A2UI cards.
- A button click on the first turn skips the question (only the web app can click).
- "text" or "buttons" switches mode at any time.
- If the question has been asked twice and the next message still isn't an answer, the agent
  uses text mode, which works in both apps.

The question is answered in `app/agent.py` (`ask_display_mode`) without a model call.

## Consequences

- One extra exchange per conversation.
- One agent, one registration, one authorization for both apps.
- A user who answers "desktop" on a phone sees the red box; they start a new conversation and
  answer 1.
- Markdown replies need blank lines between lines (Gemini Enterprise ignores single newlines).

## Alternatives

- **Auto-detect.** Not possible today (measured).
- **Two agents** (a rich one and a text-only one). Rejected: two listings, two authorizations, two
  deployments.
- **Text everywhere.** Rejected: loses the one-tap cards on the web.
