# User guide

For employees, and for help desks who support them. The agent reports broken work hardware to
the service desk and follows up on it, from Gemini Enterprise. It works in the web app and the
mobile app.

## Before the first use

The first time, Gemini Enterprise asks you to **Authorize**. Sign in to ServiceNow **as
yourself**. If your browser is already signed in to ServiceNow with another account (for example
an admin account), the agent will act as that account. Use a private window if in doubt.

## Desktop or mobile?

The first reply in each conversation asks:

> Are you on the Gemini Enterprise Desktop or Mobile App?
> 1 = Mobile App, 2 = Desktop/Browser

- **2 (desktop/browser):** you get cards with buttons.
- **1 (mobile app):** you get text with numbered choices. Reply with the number (or the words of
  the choice) to pick it.

The agent asks because the mobile app can't show cards and the agent can't tell the two apps
apart. Your first message is kept and handled as soon as you answer. Type **text** or
**buttons** at any time to switch.

## What to say

One sentence is usually enough. Say what is broken and anything that makes it urgent.

- "My laptop screen is cracked and I have a presentation on Thursday"
- "My laptop won't turn on"
- "Asset 123456 has a broken keyboard" (an asset tag or serial number finds the device directly)
- "The infusion pump in ED bay 7 shows an error" (shared or medical equipment, in your own words)
- Or just send a photo of the device and its sticker.

The agent knows who you are, your department, location and devices from ServiceNow. It never
asks for them.

## The steps and the buttons

| Step | What you see | Buttons |
|---|---|---|
| 1. Device | Your devices, or the one it found | Pick a device; **None of these**; **Equipment or another device**; **View my tickets** |
| Check | "Is this the right device?" with model, tag and serial, and who it belongs to | **Yes, that's it** / **No, it's a different one** |
| Already reported | Shared equipment that already has an open ticket | **Add my note to INC...** (you follow that ticket) / **Report separately** |
| 2. Problem | The problem choices for this kind of device | One per problem |
| 3. Photo | What to photograph | **Skip the photo** (not shown when a photo is required) |
| Label | When the device can't be found | **I can't find a label** (then type the serial or describe it) |
| 4. Review | Everything filled in: device, problem, urgency, expected time, ship-to, bill-to | **Submit request**; **Change something**; saved addresses such as **Ship to Home instead** |
| Done | The ticket number | **View my tickets** / **Start another request** |

Steps are skipped when the agent already knows the answer. Anyone may report any equipment. If
it isn't yours or your department's, the card says so and the ticket notes it.

**Safety concern** (equipment): the request is always critical. Follow the safety steps shown
on the card, such as taking the equipment out of service.

**Where it ships.** Your address in ServiceNow, unless you pick a saved address or ask for
another ("ship it to 12 Oak St, Springfield"). Home and office addresses you type are saved for
next time. Hotels and event venues are used once and not saved. Saying "looks good" never changes
the address.

## Photos

- Use the attach (+) button in the chat. JPEG, PNG, WebP or HEIC, up to 15 MB.
- A photo can identify the device (asset tag or serial sticker) and show the damage at the same
  time.
- The agent reads printed text only, not barcodes or QR codes.
- Photos are copied onto the ticket.
- **In clinical areas, don't photograph patients, or screens showing patient information.**

## Getting updates

The agent does not send messages on its own. Ask it, any time:

- "Any update?" or "Show my tickets": your hardware tickets (reported or followed). **Details**
  opens one; **Include closed** shows older ones.
- "Show the notes on INC0012345", "What's the latest note?"

On a ticket you can:

| Button | What happens |
|---|---|
| **Notes**, **All details** | Shows the notes or the full ticket |
| **Add a note** | Adds your note to the ticket |
| **Change ship-to** | Changes where the replacement goes |
| **Request urgent handling** | Asks for higher urgency, with your reason |
| **Cancel request** | Cancels it, with your reason (tickets are cancelled, never deleted) |

You can also ask to move a ticket back to in progress, put it on hold, or mark it resolved.

**Following shared equipment.** If you chose **Add my note** on equipment someone else
reported, you follow that ticket. It shows up in your tickets and you can add notes. Only the
person who reported it can change it or cancel it; if you ask, the agent adds your request as a
note for the service desk.

**When a change isn't allowed.** Some changes need ServiceNow permissions you may not have. The
agent then says plainly that it was **not** changed and that a note asking the service desk to
make it was added to the ticket. It never says something changed unless it did.

## What it can't do

- Anything other than work hardware: it says so and stops.
- See other people's tickets, except ones you follow.
- Delete tickets (cancel instead), or stop following a ticket.
- Make a change your ServiceNow account isn't allowed to make (it notes the request instead).
- Read barcodes.
- Show buttons in the mobile app.
- Push updates to you: ask for them.

## For help desks

- Tickets come in as incidents in the configured hardware category, caller = the employee,
  marked with correlation display "Gemini Enterprise - Hardware Replacement agent".
- Notes from the agent explain anything ServiceNow refused (a lower priority than requested,
  fields that couldn't be set, ownership not confirmed). Please act on them.
- "ServiceNow is waking up": the instance was asleep (developer instances). Try again in a minute.
- "Sign-in missing or expired": the user reconnects ServiceNow for this agent in Gemini Enterprise.
- Red "unsupported content" box on a phone: the conversation was answered as desktop. Start a
  new conversation and answer 1.
