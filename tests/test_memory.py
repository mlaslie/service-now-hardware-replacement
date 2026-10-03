"""Saved delivery addresses: verbatim, per person, one per label, never lost on a failed save."""

import json
from types import SimpleNamespace

import pytest

from app import memory


class FakeMemories:
    def __init__(self, fail_create=False):
        self.rows: dict[str, dict] = {}
        self.fail_create = fail_create
        self.n = 0

    def retrieve(self, name, scope, simple_retrieval_params):
        return [SimpleNamespace(memory=SimpleNamespace(name=k, fact=v["fact"]))
                for k, v in self.rows.items() if v["scope"] == scope]

    def create(self, name, fact, scope, config):
        if self.fail_create:
            raise RuntimeError("Memory Bank unavailable")
        self.n += 1
        self.rows[f"m{self.n}"] = {"fact": fact, "scope": scope}

    def delete(self, name):
        self.rows.pop(name)


@pytest.fixture
def bank(monkeypatch):
    fake = FakeMemories()
    monkeypatch.setattr(memory, "_client", lambda: SimpleNamespace(agent_engines=SimpleNamespace(memories=fake)))
    monkeypatch.setattr(memory, "_engine", lambda: "engines/1")
    return fake


HOME = "742 Evergreen Terrace, Kansas City, MO 64110"


async def test_saved_verbatim_and_per_person(bank):
    await memory.save_address("jane@example.com", "Home", HOME)
    assert await memory.saved_addresses("jane@example.com") == [{"label": "Home", "address": HOME}]
    assert await memory.saved_addresses("john@example.com") == []
    fact = next(iter(bank.rows.values()))["fact"]
    assert json.loads(fact[len(memory._ADDRESS_PREFIX):])["address"] == HOME  # exactly as typed


async def test_same_address_is_not_saved_twice(bank):
    await memory.save_address("jane@example.com", "Home", HOME)
    await memory.save_address("jane@example.com", "Home", HOME.upper().replace(",", ""))
    assert len(bank.rows) == 1


async def test_moving_replaces_the_address_for_that_label(bank):
    await memory.save_address("jane@example.com", "Home", HOME)
    await memory.save_address("jane@example.com", "Office", "1 Main St, Denver, CO 80202")
    await memory.save_address("jane@example.com", "Home", "9 New Rd, Austin, TX 78701")
    saved = await memory.saved_addresses("jane@example.com")
    assert {a["label"]: a["address"] for a in saved} == {"Home": "9 New Rd, Austin, TX 78701",
                                                        "Office": "1 Main St, Denver, CO 80202"}


async def test_a_failed_save_keeps_the_old_address_and_never_raises(bank):
    await memory.save_address("jane@example.com", "Home", HOME)
    bank.fail_create = True
    await memory.save_address("jane@example.com", "Home", "9 New Rd, Austin, TX 78701")
    assert await memory.saved_addresses("jane@example.com") == [{"label": "Home", "address": HOME}]


async def test_memory_outage_means_no_saved_addresses(monkeypatch):
    def broken():
        raise RuntimeError("down")
    monkeypatch.setattr(memory, "_client", broken)
    assert await memory.saved_addresses("jane@example.com") == []
