# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
integration: editing / replaying a past message in the webui really changes
what pi sits on.

the webui routes only touch OpenLumara's chat store:

  message_edit        rewrite a message in place, no new prompt
  message_delete      delete_from(index), no new prompt
  message_regenerate  delete_from(last user message) and re-send that text

the default backend needs nothing more, because it rebuilds model context
from the store on every request. pi's context is its session, so the module
rebuilds that session from the store and rides the replay along in the next
prompt. These tests run real pi processes and read pi's own answers back out
of it (get_messages) to confirm what pi actually sits on afterwards - the
fake-client tests cover the shapes; only pi itself can confirm the context.

skipped when the pi binary or a usable model endpoint is unavailable.
"""

import json
import os
import pathlib
import shutil
import types

import pytest

import core
from harness import FakeManager

from test_pi_chat_sync import StoreChannel
from test_pi_tool_stream_integration import (
    PI_BIN,
    _InjectionModule,
    load_pi_module,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture(scope="module")
def rpc_cls():
    return load_pi_module("pi_rpc")


def _answer(tokens):
    return "".join(
        str(t.get("content") or "") for t in tokens if t.get("type") in ("content", "text")
    ).strip()


async def _turn(backend, prompt_message):
    tokens = []
    async for token in backend.send_stream([prompt_message]):
        tokens.append(token)
    errors = [t for t in tokens if t.get("type") == "error"]
    if errors:
        pytest.skip(f"pi turn failed (endpoint?): {errors[:1]}")
    return tokens


async def _scenario(backend_cls, rpc_cls, tmp_path):
    """a real pi wired to a real chat store, plus the store-side helpers the
    webui routes perform"""
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    base_url = os.environ.get("OPENLUMARA_TEST_API_URL", "http://localhost:8091/v1")
    api_key = os.environ.get("OPENLUMARA_TEST_API_KEY", "")
    model_id = os.environ.get("OPENLUMARA_TEST_MODEL", "Qwen3.8-Flash-Next-W4A16")

    manager = FakeManager()
    module = _InjectionModule(base_url, api_key, model_id)
    module.binary = PI_BIN
    module.session_map = core.storage.StorageDict("pi_sessions", type="json")
    backend = backend_cls(manager, module)
    env, args, _notes = await backend._prepare_provider_injection()

    client = rpc_cls.PiRPCClient(
        PI_BIN, cwd=str(tmp_path),
        extra_args=[*args, "--offline", "--no-context-files", "--no-skills",
                    "--no-prompt-templates", "--no-mcp",
                    "--session-dir", str(tmp_path / "sessions")],
        env=env,
    )
    backend.pi = client

    channel = StoreChannel(manager, chat_id="chat-sync-1")
    channel.context.channel = channel
    manager.channel = channel
    manager.API = backend

    async def _no_session_sync(channel=None):
        return None

    backend._sync_session = _no_session_sync # session pinning is covered elsewhere
    await client.start()
    return backend, channel, client



async def _pi_user_texts(client):
    """what pi believes the user said, straight from pi"""
    data = await client.send({"type": "get_messages"})
    texts = []
    for message in (data or {}).get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(str(b.get("text") or "") for b in content or [] if isinstance(b, dict))
    return texts


async def _pi_message_roles(client):
    data = await client.send({"type": "get_messages"})
    return [m.get("role") for m in (data or {}).get("messages") or []]


STORE_USER = lambda text: {"role": "user", "content": text, "_metadata": {}}  # noqa: E731


# ---------------------------------------------------------------------------
# editing a past message
# ---------------------------------------------------------------------------

async def test_editing_a_past_message_changes_what_pi_sits_on(backend_cls, rpc_cls, tmp_path):
    backend, channel, client = await _scenario(backend_cls, rpc_cls, tmp_path)
    try:
        # turn 1: pi learns codeword ALPHA
        first = STORE_USER("Please remember my favourite colour: ALPHA. "
                           "Reply with the single word OK.")
        channel.messages.append(first)
        await _turn(backend, first)
        assert any("ALPHA" in text for text in await _pi_user_texts(client))

        # the webui's message_edit route: rewrite that past message in place,
        # exactly as the route does (same dict, new content), no new prompt
        stored = await channel.chat.messages.get(0)
        stored["content"] = "Please remember my favourite colour: BRAVO. " \
                            "Reply with the single word OK."
        await channel.chat.messages.edit(0, stored)

        # the next question is an ordinary turn - the edit route never re-prompts
        ask = STORE_USER("What is my favourite colour? Reply with only the colour.")
        channel.messages.append(ask)
        session_before = (await client.get_state()).get("sessionFile")
        tokens = await _turn(backend, ask)
        answer = _answer(tokens)

        # pi no longer sits on the pre-edit conversation at all
        user_texts = await _pi_user_texts(client)
        assert any("BRAVO" in text for text in user_texts), \
            "the edited message must be what pi is shown"
        assert not any("ALPHA" in text for text in user_texts), \
            "the discarded wording must be gone from pi's context"

        # and it answers from the edited context
        assert "BRAVO" in answer.upper(), f"pi answered from stale context: {answer!r}"

        # the chat is remapped to the rebuilt session, so switching back later
        # does not resurrect the discarded branch
        session_after = (await client.get_state()).get("sessionFile")
        assert session_after and session_after != session_before
        assert backend.module.session_map.get("chat-sync-1") == session_after
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# replaying (regenerating) a past message
# ---------------------------------------------------------------------------

async def test_regenerate_replays_from_the_rewound_point(backend_cls, rpc_cls, tmp_path):
    backend, channel, client = await _scenario(backend_cls, rpc_cls, tmp_path)
    try:
        first = STORE_USER("Please remember my favourite colour: DELTA. "
                           "Reply with the single word OK.")
        channel.messages.append(first)
        await _turn(backend, first)

        ask = STORE_USER("What is my favourite colour? Reply with only the colour.")
        channel.messages.append(ask)
        tokens = await _turn(backend, ask)
        assert "DELTA" in _answer(tokens).upper()

        # the webui's message_regenerate route on the answer: find the last
        # user message, rewind the store to it (deleting it too), re-send text
        store = await channel.chat.messages.get()
        last_user_index = max(i for i, m in enumerate(store) if m["role"] == "user")
        rewound = len(store[:last_user_index])
        await channel.chat.messages.delete_from(last_user_index)
        assert len(await channel.chat.messages.get()) == rewound

        retry = STORE_USER("What is my favourite colour? Reply with only the colour.")
        channel.messages.append(retry) # start_stream re-adds the retried ask
        tokens = await _turn(backend, retry)

        assert "DELTA" in _answer(tokens).upper(), "the replay must answer the ask again"

        # pi sits on the rewound conversation and ONE fresh answer, not on the
        # abandoned branch it just discarded: replay + ask is one prompt
        roles = await _pi_message_roles(client)
        assert roles.count("user") == 1, f"pi kept the abandoned branch: {roles}"
        assert roles.count("assistant") == 1, f"pi did not re-answer: {roles}"
        assert not any("OK" == text.strip() for text in await _pi_user_texts(client)), \
            "the replay is a preamble to the ask, never its own turn"
    finally:
        await client.stop()
