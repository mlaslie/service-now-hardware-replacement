"""Followers on one ticket, changed by several people at once, possibly from different servers (R3, T11).

The watch list is a single text field: following is read, add, write. In one process the agent's changes
to a ticket take turns; across server instances they can't, so each change is read back, retried, and
looked at again shortly after. This generates interleavings of follows and unfollows by several people,
each served by its own simulated server, with random request delays, with or without the custom role's
follow-only business rule, and checks the outcome:

- T11: nobody's membership ends up different from what they last asked for, and the follower who never
  acted is never dropped.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import servicenow
from fuzz.invariants import check
from fuzz.strategies import examples

pytestmark = [pytest.mark.fuzz, pytest.mark.invariants("T11")]

USERS = ["u1", "u2", "u3"]
BYSTANDER = "keep"  # follows from the start and never acts


class RacyTicket:
    """One incident whose watch list several simulated servers read and write, each request delayed."""

    def __init__(self, rng: random.Random, max_delay: float, business_rule: bool):
        self.watch_list = BYSTANDER
        self.rng, self.max_delay, self.business_rule = rng, max_delay, business_rule
        self.comments: list[str] = []

    async def _pause(self):
        await asyncio.sleep(self.rng.random() * self.max_delay)

    async def __call__(self, method, path, **kw):
        await self._pause()  # the request travels
        if method == "GET":
            snapshot = self.watch_list
            await self._pause()  # ... and the answer travels back: it may be stale on arrival
            return {"result": {"sys_id": "s1", "number": "INC0000001", "state": "2", "caller_id": "reporter",
                               "watch_list": snapshot}}
        body = kw.get("json") or {}
        if "watch_list" in body:
            actor = servicenow.user_token.get()
            before = {w for w in self.watch_list.split(",") if w}
            after = {w for w in body["watch_list"].split(",") if w}
            if self.business_rule and not (before ^ after) <= {actor}:
                raise servicenow.ServiceNowError("aborted by business rule: follow only")
            self.watch_list = body["watch_list"]
        if "comments" in body:
            self.comments.append(body["comments"])
        return {"result": {"sys_id": "s1"}}


class EveryCallItsOwnServer(dict):
    """The per-ticket lock map of a single server, made to hand out a new lock every time: as if each
    request were served by a different server instance, so nothing takes turns."""

    def setdefault(self, key, default=None):
        return asyncio.Lock()


@settings(max_examples=examples(150, 1500), deadline=None)
@given(plans=st.fixed_dictionaries({u: st.lists(st.booleans(), min_size=1, max_size=4) for u in USERS}),
       seed=st.integers(0, 2**32 - 1), max_delay=st.sampled_from([0.0, 0.002, 0.005]),
       business_rule=st.booleans(), separate_servers=st.booleans())
def test_concurrent_follows_never_lose_anyone(monkeypatch, plans, seed, max_delay, business_rule, separate_servers):
    ticket = RacyTicket(random.Random(seed), max_delay, business_rule)
    monkeypatch.setattr(servicenow, "_request", ticket)
    monkeypatch.setattr(servicenow, "WATCH_RECHECK_SECONDS", 0.02)
    monkeypatch.setattr(servicenow, "_WATCH_LOCKS", EveryCallItsOwnServer() if separate_servers else {})

    async def person(user: str, actions: list[bool]):
        servicenow.user_token.set(user)  # whose token the request carries (the business rule checks it)
        for follow in actions:
            if follow:
                await servicenow.follow_incident(user, "s1", f"{user} follows")
            else:
                await servicenow.unfollow_incident(user, "s1")

    async def everyone():
        await asyncio.gather(*(person(u, plans[u]) for u in USERS))

    asyncio.run(everyone())
    final = {w for w in ticket.watch_list.split(",") if w}
    expected = {BYSTANDER} | {u for u in USERS if plans[u][-1]}
    check(final == expected, "T11", "the watch list doesn't match what each person last asked for",
          final=sorted(final), expected=sorted(expected), plans=plans, separate_servers=separate_servers,
          business_rule=business_rule, max_delay=max_delay)
