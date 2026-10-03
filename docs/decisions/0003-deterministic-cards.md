# 0003. The model never writes UI JSON; tools stage cards

**Status:** Accepted.

## Context

A2UI cards are JSON. A model asked to write them can produce malformed JSON, invent values
(a serial number, a priority), or drift from the layout. Early on the model also imitated history
summaries and wrote fake "[Card shown to user...]" lines.

## Decision

- Python builds every card in `app/cards.py`, from the draft and ServiceNow data.
- A tool **stages** its card in session state (`temp:card`) and returns a short summary to the
  model. It never returns UI JSON.
- The model can chain several tools in one turn. When it stops calling tools,
  `agent.render_staged_card` (an `after_model_callback`) swaps the model's final text for the
  last staged card. The model's one sentence becomes the card's first line.
- `compact_card_history` replaces earlier cards in the history with that one sentence, so card
  JSON is not replayed to the model every turn.

## Consequences

- A card can't be malformed and can't show a value the tools didn't produce.
- The model still chooses the steps, so one sentence can jump straight to review.
- The same card model renders three ways: A2UI v0.9, v0.8 (`cards.to_v08`) and numbered text for
  mobile (`cards.to_text`).
- Layout changes are code changes in `app/cards.py`. Wording is partly in the profile.
- The instruction tells the model to reply with at most one short sentence and no markdown.

## Alternatives

- **Model writes A2UI with a schema (A2UI SDK prompt).** Rejected: more tokens per turn, and
  values on the card could be invented.
- **Tools return the card and end the turn.** Rejected: the model could no longer chain
  steps in one turn.
