# 0004. A2UI v0.9 cards, translated to v0.8 when a client asks

**Status:** Accepted (since Cloud Run revision 00024, 2026-09-30).

## Context

The first version drew A2UI v0.8 cards. In Gemini Enterprise, v0.8 ignores `primaryColor`, so
buttons couldn't take a brand colour, and v0.8 is deprecated upstream. A probe agent showed that
an agent declaring both v0.9 and v0.8 gets v0.9 requested by Gemini Enterprise, and the web app
renders v0.9 fully, including `theme.primaryColor` on primary buttons.

## Decision

- Cards are built as **v0.9** (`app/cards.py`), with `theme.primaryColor` from
  `branding.primary_color` in the profile.
- The agent card declares **both** v0.9 and v0.8 (`server.build_agent_card`).
- Each turn, `inbound.requested_a2ui_version` reads which version the client asked for. If it is
  v0.8, `cards.to_v08` translates the card.
- Forward buttons are primary (brand colour); ways back or out are grey.
- Clicks in both shapes (`{"action": ...}` for v0.9, `{"userAction": ...}` for v0.8) become a
  text line `[UI action] name {context}`. The "User action triggered." text part that comes with
  a v0.9 click is dropped.

## Consequences

- The colour applies to primary buttons only. Other buttons stay grey; fonts are Gemini
  Enterprise's (measured).
- Gemini Enterprise's agent preview shows two A2UI capability entries. Both are expected.
- Changing the declared versions changes the agent card. Edit the agent in Gemini Enterprise and
  paste the new card.
- Mobile is unaffected: it renders neither version (see [0005](0005-desktop-or-mobile-question.md)).

## Alternatives

- **Stay on v0.8.** Rejected: no colour, deprecated.
- **v0.9 only.** Possible, but the v0.8 fallback is cheap and keeps older registrations working.
