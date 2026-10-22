"""A dashboard create answered with a failure leaves no session behind.

``api_chat_slot_create`` mints the slot inside its ``suspend_slots_push`` block,
then pins the member's private store, resolves the agent bindings and records
the selection. Two failures come back after the mint: ``store_unavailable``
(503) when the pin or the resolution raises, and ``session_rebound`` (409) when
the newborn changed while those awaits ran. The client is told the create
failed, so a newborn still in ``state._slots`` goes out in the block's owed
slots frame as a session nobody asked to keep, and every retry adds another
empty row. These tests force each failure and require that the key is neither
live nor listed in any frame.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers


def _member_config() -> KiroCrewConfig:
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "worker": KiroCrewAgentConfig(kiro_agent="kirocrew"),
    }
    cfg.default_agent = "default"
    provision_member_memory(cfg, "worker")
    return cfg


def _record_frames(state: Any) -> list[list[dict]]:
    seen: list[list[dict]] = []

    def capture(payload: dict) -> None:
        if payload.get("_type") == "slots":
            seen.append(json.loads(payload["slots"]))

    state._broadcast = capture  # type: ignore[method-assign]
    # Reopen the coalescing window so the block's owed push is sent, not absorbed.
    state._slots_broadcast_last = float("-inf")
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito"])
@pytest.mark.parametrize("failure", ["store_unavailable", "session_rebound"])
async def test_a_failed_create_neither_keeps_nor_announces_its_newborn(
    tmp_path, monkeypatch, failure, memory_mode
) -> None:
    from kiro_crew.session_agent_selection import session_agent_selection_name

    state = _make_state(tmp_path)
    cfg = _member_config()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    loop = asyncio.get_running_loop()
    slots_before = set(state._slots)
    restricted_before = set(state._restricted_keys)
    pinned: list[str] = []
    real_resolve = chat_handlers.resolve_agent_bindings

    async def pin(state_arg, session_key, agent, config, **kwargs):
        pinned.append(session_key)
        if failure == "store_unavailable":
            raise OSError("private store unavailable")
        # The store this test is not about: assign none and let the selection run.
        return ""

    def resolve(config, agent, project=None, **kwargs):
        result = real_resolve(config, agent, project, **kwargs)
        # The handler also resolves once before the mint; only the post-mint
        # call has a newborn to move. Another request moves it while this
        # resolution is off the loop.
        newborn = next((s for k, s in state._slots.items() if k not in slots_before), None)
        if newborn is not None:
            loop.call_soon_threadsafe(setattr, newborn, "project", str(tmp_path / "elsewhere"))
        return result

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    if failure == "session_rebound":
        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    frames = _record_frames(state)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"agent": "worker", "memory_mode": memory_mode}
        )
        data = await response.json()
        # Let any trailing coalesced push fire before reading the frames.
        await asyncio.sleep(0.05)

    expected_status = 503 if failure == "store_unavailable" else 409
    assert response.status == expected_status, data
    assert data["code"] == failure
    # Positive control: the create got as far as the pin, so a slot was minted.
    assert len(pinned) == 1
    session_key = pinned[0]
    key = session_key.removeprefix("dashboard:")
    announced = [row for frame in frames for row in frame if row["key"] == key]
    assert announced == [], "a slots frame listed a session whose create failed"
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert session_agent_selection_name(session_key) is None
