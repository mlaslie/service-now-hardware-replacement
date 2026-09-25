# Hardware Replacement agent: demo script (about 5 minutes)

## Before you start

- Sign in to the Gemini Enterprise app as **john.doe@example.com**. When the agent asks
  you to authorize ServiceNow, sign in as **john.doe** (use a private window if your browser
  is logged in to ServiceNow as admin).
- In ServiceNow, john.doe has a location with a street address, a department and cost
  center, and assets assigned (e.g. the MacBook Air 13", asset tag 123456, serial FCPJ2GJTHC).
- Have `demo/cracked.jpg` and `demo/label.jpg` on hand. A real phone photo works even better.
- Open ServiceNow in a second tab on the incident list:
  https://INSTANCE.service-now.com/incident_list.do?sysparm_query=caller_id.user_name=john.doe
- Start each scene in a **new conversation**.

---

## Opening (20 seconds)

> "Replacing a broken laptop usually means a portal, a form with 15 fields, looking up your
> asset tag, and a follow-up call to prove it's broken. This agent does it in a conversation.
> It already knows who I am, because Gemini Enterprise passes my identity through to it."

---

## Scene 1: The one-sentence request (1 minute)

**Type:**
```
My laptop won't turn on and I have a client demo tomorrow
```

**What happens:** the agent jumps straight to **Step 4 of 4: Review your request**.

**Point out:**
- "I never said which laptop. It knows which one is mine from ServiceNow and picked it."
- "Priority is **2 - High**. It picked up 'demo tomorrow' on its own."
- "Coverage and the recommendation come from the ServiceNow asset: under warranty, so it's a
  warranty replacement."
- "No photo step. A dead laptop doesn't need a picture, so it skipped it."

**Click:** **Submit request**. The card shows the incident number (INC00100xx).

**Switch to ServiceNow** and open the new incident. Show:
- **Caller** is John Doe: the ticket was filed with my ServiceNow sign-in, not a service account
- **Configuration item** is the laptop, **Priority** matches what the agent requested
- **Description** has device, coverage, recommendation, ship-to and bill-to
- The photo is attached (Scene 2)

> "A real ServiceNow incident, filed as me, with the device, warranty status, priority and
> shipping details filled in without a form."

---

## Scene 2: Show, don't tell (1.5 minutes)

**Attach** `cracked.jpg` without typing anything and send it.

**What happens:** the card says **Here's what I found in your photo**. It shows the device and a
damage description (severe spiderweb cracking), with the laptop highlighted among your devices.

> "I didn't even say what was wrong. The photo says it."

**Click:** the highlighted **MacBook Air**. The review card appears with:
- Issue: Cracked or broken screen
- **Photo evidence**: the damage description and its severity

> "That photo is now evidence on the ticket. The service desk can approve a replacement
> without calling me to check."

**Click:** **Submit request**.

---

## Scene 3: Let the camera read the label (1 minute)

New conversation. **Type:**
```
I need to replace a laptop but I don't know the asset tag
```
Then **attach** `label.jpg`, or a real photo of an asset sticker.

**What happens:** the agent reads the asset tag (123456), model and serial number (FCPJ2GJTHC)
from the photo, matches it in ServiceNow's asset table, and moves to **Step 2: What's wrong with it?**

**Point out:**
- "It read the tag and serial number off the sticker and found the device in inventory."
- If a warning shows that the serial differs from inventory: "It also cross-checks the
  label against the inventory record."

**Click:** **Battery or charging**. It asks for an optional photo. **Click:** **Skip the photo**.
Then submit.

---

## Scene 4 (optional): Change your mind (30 seconds)

On any review card, **type:**
```
Actually it's urgent, and ship it to 1200 Larimer St, Denver CO 80202
```
The review card comes back with a higher requested priority and the new ship-to.

## Scene 5 (optional): Manage the ticket (1 minute)

```
Show my tickets
```
Click **Details**, then try: "the tracking number doesn't work, move it back to In Progress".
Point out that anything ServiceNow's policy doesn't allow from an employee account is added to
the ticket as a note for the service desk, and the agent says so plainly.

---

## Closing (20 seconds)

> "Three requests in about three minutes. No forms and no asset lookups, and anyone who
> can take a photo can use it. It runs on Cloud Run as an ADK agent, connects to Gemini
> Enterprise over A2A, and keeps the signed-in user's identity all the way to the ticket."

---

## If something goes wrong

| What you see | Fix |
|---|---|
| "reconnect ServiceNow" / sign-in expired | Re-authorize the agent's ServiceNow connection in Gemini Enterprise. |
| Agent greets you as the wrong person (e.g. System Administrator) | The authorization used the browser's ServiceNow session. Revoke it and re-authorize while logged in as john.doe. |
| "ServiceNow is waking up" | The developer instance hibernated. Wake it at developer.servicenow.com. |
| Photo upload fails with a Content-Type error | The photo is over about 24MB. Retake it at a lower resolution. |
| A card looks stale or the flow is confused | Start a new conversation. |
| Want to reset the demo tickets | Cancel them from the agent, or close them in ServiceNow. |
