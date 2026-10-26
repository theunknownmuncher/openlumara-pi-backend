# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
pi backend: editing / deleting / regenerating messages and compaction must
behave exactly like they do on the default OpenLumara backend.

why this needs its own tests: the default backend is stateless per request -
core.context.Context.get() rebuilds model context from the chat store every
single time - so the webui's message_edit / message_delete / message_regenerate
routes (which only touch the chat store) simply apply, and Context.compress()
performs the OpenAI-style summarization pass. pi instead keeps its own
session; without help it would keep acting on edited-away or deleted
messages, and its compaction would ignore OpenLumara's settings.

intent under test:
- the module's restorable Context patches (module._patch_context) make the
  framework's compression trigger measure pi's real session fill, respect
  model.automatically_compress_context and model.context_compression_threshold,
  and route the compression itself to pi's native compaction - silently on
  automatic triggers (the default backend's automatic pass is silent too),
  with the original recursion guard and the same token-cache invalidation.
- automatic triggers never cut into a running pi turn.
- pi's own near-full auto-compaction mirrors the same OpenLumara switch.
- after the chat store diverged from what pi last saw (edit / delete-from /
  regenerate), the next turn rebuilds pi's session and prompts pi with a
  recap of exactly what the store still shows, with the trailing user
  message becoming the prompt (the regenerate shape) - so pi sees what the
  user sees.
- turns that did not touch the store never rebuild anything.
"""


import asyncio
import pytest

import core
from harness import FakeManager, apply_config

from test_pi_tool_stream import load_pi_module


@pytest.fixture(scope="module")
def pi_backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture
def patched_context():
    """apply the module's Context patches the way Pi.on_ready does, and
    restore the originals afterwards (module reload safety)"""
    module_cls = load_pi_module("module").Pi
    shell = module_cls.__new__(module_cls)
    shell._patches = []
    shell._patch_context()
    yield shell._patches
    for cls, name, original in reversed(shell._patches):
        setattr(cls, name, original)


class SyncPiClient:
    """PiRPCClient surface the history-sync / compaction paths touch"""

    def __init__(self, stats=None):
        self.alive = True
        self.calls = []
        self.stats = stats if stats is not None else {}
        self.session_seq = 0

    async def new_session(self):
        self.calls.append(("new_session",))
        self.session_seq += 1
        return {"cancelled": False}

    async def get_state(self):
        return {"sessionFile": f"/tmp/pi-session-{self.session_seq}.jsonl", "model": {}}

    async def set_session_name(self, name):
        self.calls.append(("set_session_name", name))

    async def get_session_stats(self):
        return self.stats

    async def compact(self, custom_instructions=None):
        self.calls.append(("compact",))
        return {"summary": "handoff summary", "tokensBefore": 9000, "estimatedTokensAfter": 800}

    async def set_auto_compaction(self, enabled):
        self.calls.append(("set_auto_compaction", enabled))

    def clear_events(self):
        pass


class SyncFakeModule:
    """the bits of the module the history-sync / compaction paths touch"""

    binary = "/bin/true"

    class _Config:
        def __init__(self, settings):
            self.settings = settings

        def get(self, key, default=None):
            return self.settings.get(key, default)

    def __init__(self):
        self.config = SyncFakeModule._Config({"session_per_chat": True, "forward_images": True})
        self.session_map = core.storage.StorageDict("pi_sessions", type="json")
        self.logs = []

    def log(self, category, message):
        self.logs.append((category, message))


class StoreChannel:
    """channel with a readable/writable chat store (what the sync paths use)"""

    def __init__(self, manager, chat_id="chat-1", title="test chat", messages=None):
        self.manager = manager
        self.messages = list(messages or [])
        self.pushes = []

        channel = self

        class _Messages:
            async def get(self, index=None):
                if index is not None:
                    return channel.messages[index]
                return channel.messages

            async def add(self, message, **kwargs):
                channel.messages.append(dict(message))

            async def edit(self, index, message):
                channel.messages[index] = dict(message)

            async def delete_from(self, index):
                del channel.messages[index:]

        class _Data(list):
            def save(self):
                pass

        class _Chat:
            def __init__(self):
                self.current = 0
                self.data = _Data([{"id": chat_id, "token_usage": 500, "token_usage_mark": 3}])

            def get(self, key=None, default=None):
                return {**{"id": chat_id, "title": title}, "current": 0}.get(key, default)

            messages = _Messages()

        self.chat = _Chat()

        class _Ctx(core.context.Context):
            """attribute-identical to the real Context (the patched methods
            only touch these), with the fake chat store attached"""

            def __init__(self):
                self.channel = None
                self.model_name = None
                self.using_api_token_data = False
                self.token_encoding = None
                self.compressing = False
                self._overflow_warned = False
                self._prompt_cache_state = {}
                self.chat = None

        self.context = _Ctx()
        self.context.chat = self.chat

    async def push(self, message, defer=True):
        self.pushes.append(message)

    def log(self, category, message):
        pass


def make_sync_backend(pi_backend_cls, messages=None, stats=None):
    manager = FakeManager()
    module = SyncFakeModule()
    backend = pi_backend_cls(manager, module)
    client = SyncPiClient(stats)
    backend.pi = client
    channel = StoreChannel(manager, messages=messages)
    channel.context.channel = channel
    manager.channel = channel
    return backend, channel, client, module


BASE = {
    "api": {"max_context": 10000},
    "model": {
        "name": "test-model",
        "automatically_compress_context": True,
        "context_compression_threshold": 0.8,
    },
    "core": {"cmd_prefix": "/"},
}


def conversation():
    return [
        {"role": "user", "content": "first ask", "_metadata": {}},
        {"role": "assistant", "content": "first answer", "_metadata": {}},
        {"role": "user", "content": "second ask", "_metadata": {}},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "ls"}'},
            }],
            "_metadata": {},
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "success", "content": "file.txt"}', "_metadata": {}},
        {"role": "assistant", "content": "second answer", "_metadata": {}},
    ]


# ---------------------------------------------------------------------------
# history sync after edit / delete / regenerate
# ---------------------------------------------------------------------------

async def test_no_rebuild_without_divergence(pi_backend_cls):
    backend, channel, client, _ = make_sync_backend(pi_backend_cls, conversation())

    # first turn: stamp the watermark (no rebuild - nothing changed yet)
    assert await backend._rebuild_if_history_diverged(channel) is False
    assert client.calls == []

    await backend._mark_history_synced(channel)
    assert await backend._rebuild_if_history_diverged(channel) is False
    assert client.calls == []


async def test_message_edit_rebuilds_session_with_recap(pi_backend_cls):
    messages = conversation()
    backend, channel, client, module = make_sync_backend(pi_backend_cls, messages)
    await backend._mark_history_synced(channel)

    # the webui's message_edit route: rewrite a past user message in place
    await channel.chat.messages.edit(0, {"role": "user", "content": "EDITED ask", "_metadata": {}})

    assert await backend._history_diverged(channel) is True
    assert await backend._rebuild_if_history_diverged(channel) is True

    assert ("new_session",) in client.calls
    assert module.session_map.get("chat-1", "").endswith(".jsonl")

    # the store's recap fed to pi reflects the edit, not the original text
    recap = backend._render_chat_history(channel.messages[:-1] if channel.messages[-1]["role"] == "user" else channel.messages)
    assert "EDITED ask" in recap
    assert "first ask" not in recap
    # tool activity is summarized, answers survive
    assert "bash(" in recap and "second answer" in recap

    # the watermark clears itself: after resync the store matches again
    await backend._mark_history_synced(channel)
    assert await backend._rebuild_if_history_diverged(channel) is False


async def test_delete_from_rebuilds(pi_backend_cls):
    backend, channel, client, _ = make_sync_backend(pi_backend_cls, conversation())
    await backend._mark_history_synced(channel)

    # message_delete route: delete_from(index) drops that message and all after
    await channel.chat.messages.delete_from(3)

    assert await backend._history_diverged(channel) is True
    assert await backend._rebuild_if_history_diverged(channel) is True
    assert ("new_session",) in client.calls

    # once resynced, the same store no longer diverges (no rebuild storms)
    client.calls.clear()
    assert await backend._rebuild_if_history_diverged(channel) is False
    assert client.calls == []

    # and the rebuilt recap only contains what survived the delete
    recap = backend._render_chat_history(channel.messages)
    assert "first ask" in recap and "second answer" not in recap


async def test_regenerate_shape_puts_user_message_last(pi_backend_cls):
    """webui regenerate = delete_from(user_index) + re-stream that message;
    the store then ends on the user message, which must become the prompt
    itself rather than being folded into the recap"""
    messages = [
        {"role": "user", "content": "old ask", "_metadata": {}},
        {"role": "assistant", "content": "old answer", "_metadata": {}},
        {"role": "user", "content": "ask to redo", "_metadata": {}},
        {"role": "assistant", "content": "the answer being regenerated", "_metadata": {}},
    ]
    backend, channel, client, _ = make_sync_backend(pi_backend_cls, messages)
    await backend._mark_history_synced(channel)

    # the regenerate route: delete_from(user index) drops that message and
    # everything after it, then the channel re-adds the same user message
    await channel.chat.messages.delete_from(2)
    await channel.chat.messages.add({"role": "user", "content": "ask to redo", "_metadata": {}})

    assert await backend._history_diverged(channel) is True

    captured = {}
    original_render = backend._render_chat_history

    def spy(msgs):
        captured["rendered_len"] = len(original_render(msgs))
        # the trailing user message must have been excluded from the recap
        assert all(m.get("role") != "user" or "ask to redo" not in str(m.get("content")) for m in msgs)
        return original_render(msgs)

    backend._render_chat_history = spy
    assert await backend._rebuild_if_history_diverged(channel) is True
    assert captured["rendered_len"] > 0  # prior turns were kept in the recap


async def test_ghost_and_cmd_messages_excluded_from_recap():
    module = load_pi_module("backend")
    recap = module._render_chat_history([
        {"role": "user", "content": "real", "_metadata": {}},
        {"role": "user", "content": "injected context", "_metadata": {"ghost": True}},
        {"role": "user", "content": "/somecmd", "_metadata": {"is_cmd": True}},
        {"role": "assistant", "content": "answer", "_metadata": {}},
    ])
    assert "real" in recap and "answer" in recap
    assert "injected context" not in recap and "/somecmd" not in recap


# ---------------------------------------------------------------------------
# compaction parity (the module's Context patches)
# ---------------------------------------------------------------------------

async def test_threshold_and_compress_route_to_pi(patched_context):
    apply_config(**BASE)
    manager = FakeManager()
    stats = {"contextUsage": {"tokens": 9000, "contextWindow": 10000, "percent": 90}}
    backend, channel, client, module = make_sync_backend(
        load_pi_module("backend").PiBackend, stats=stats
    )
    manager.API = backend
    channel.manager = manager

    ctx = channel.context  # real class with the module's patched methods

    # 90% fill >= 0.8 threshold -> same verdict the default would give
    assert await ctx.is_over_threshold() is True

    # and the compression it triggers runs pi's compaction, silently
    result = await ctx.compress()
    assert ("compact",) in client.calls
    assert result == {"role": "assistant", "content": "handoff summary"}
    assert channel.pushes == []  # automatic compression is silent, like default
    # the original invalidates cached token measurements after compressing
    assert channel.chat.data[0]["token_usage"] == 0
    assert channel.chat.data[0]["token_usage_mark"] == 0
    assert ctx.using_api_token_data is False


async def test_threshold_respects_settings(patched_context):
    stats = {"contextUsage": {"tokens": 9000, "contextWindow": 10000}}

    # user disabled automatic compression -> default returns False -> so must we
    apply_config(**{**BASE, "model": {**BASE["model"], "automatically_compress_context": False}})
    backend, channel, client, _ = make_sync_backend(
        load_pi_module("backend").PiBackend, stats=stats
    )
    channel.manager.API = backend
    assert await channel.context.is_over_threshold() is False

    # fill below the configured threshold
    apply_config(**{**BASE, "model": {**BASE["model"], "context_compression_threshold": 0.95}})
    assert await channel.context.is_over_threshold() is False

    # threshold at exactly the fill triggers (>=, like the original)
    apply_config(**{**BASE, "model": {**BASE["model"], "context_compression_threshold": 0.9}})
    assert await channel.context.is_over_threshold() is True


async def test_auto_trigger_never_interrupts_a_running_turn(patched_context):
    apply_config(**BASE)
    backend, channel, client, _ = make_sync_backend(
        load_pi_module("backend").PiBackend,
        stats={"contextUsage": {"tokens": 9500, "contextWindow": 10000}},
    )
    channel.manager.API = backend

    backend.is_streaming = True
    assert await channel.context.compress() is False
    assert ("compact",) not in client.calls

    backend.is_streaming = False
    async with backend._turn_lock:
        assert await channel.context.compress() is False
    assert ("compact",) not in client.calls


async def test_explicit_compaction_pushes_like_default(patched_context):
    """the /compress path (and /pi compact / panel button) reports the
    before/after fill - the default backend's explicit compress also answers
    through the channel, while its automatic path stays quiet"""
    backend, channel, client, _ = make_sync_backend(load_pi_module("backend").PiBackend)
    channel.manager.API = backend
    result = await backend.pi_compact()
    assert result["summary"] == "handoff summary"
    assert any("compacted" in str(p) for p in channel.pushes)


async def test_auto_compaction_mirrors_the_openlumara_switch(patched_context):
    apply_config(**BASE)
    module_cls = load_pi_module("module").Pi
    backend, channel, client, module = make_sync_backend(
        load_pi_module("backend").PiBackend
    )

    shell = module_cls.__new__(module_cls)
    shell.backend = backend
    shell._auto_compaction_applied = None
    shell.logs = []

    def log(category, message):
        shell.logs.append((category, message))

    shell.log = log

    await shell.sync_auto_compaction()
    assert ("set_auto_compaction", True) in client.calls

    # unchanged setting -> no repeated rpc
    client.calls.clear()
    await shell.sync_auto_compaction()
    assert client.calls == []

    apply_config(**{**BASE, "model": {**BASE["model"], "automatically_compress_context": False}})
    await shell.sync_auto_compaction()
    assert ("set_auto_compaction", False) in client.calls


# ---------------------------------------------------------------------------
# delivery: the recap must actually reach pi, inside the next prompt
# ---------------------------------------------------------------------------

TURN_EVENTS = [
    {"type": "message_start", "message": {"role": "user"}},
    {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
    {"type": "message_start", "message": {"role": "assistant", "model": "m"}},
    {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "ok"}},
    {"type": "message_end", "message": {
        "role": "assistant", "model": "m", "stopReason": "stop", "usage": {},
        "content": [{"type": "text", "text": "ok"}]}},
    {"type": "agent_settled"},
]


class TurnPiClient(SyncPiClient):
    """SyncPiClient plus the turn surface: records each prompt (text and
    attachments) and replays a canned settled turn, so a whole send_stream
    can run against it"""

    def __init__(self, stats=None):
        super().__init__(stats)
        self.prompts = []
        self.images = []
        self._events = list(TURN_EVENTS)
        self._pos = 0

    async def prompt(self, text, images=None, timeout=None):
        self.calls.append(("prompt", text))
        self.prompts.append(text)
        self.images.append(list(images or []))
        self._pos = 0 # every prompt gets its own canned turn to replay

    async def next_event(self, timeout=None):
        if self._pos >= len(self._events):
            await asyncio.sleep(3600)
        event = self._events[self._pos]
        self._pos += 1
        return event


async def _turn_backend(pi_backend_cls, messages):
    """backend wired for a full send_stream against a fake store"""
    backend, channel, _client, module = make_sync_backend(pi_backend_cls, messages)
    client = TurnPiClient()
    backend.pi = client

    async def _none(channel=None):
        return None

    backend._sync_session = _none  # session pinning is covered elsewhere
    return backend, channel, client, module


async def _send(backend, context):
    return [t async for t in backend.send_stream(list(context))]


async def test_rebuild_delivers_the_recap_inside_the_next_prompt(pi_backend_cls):
    """handing pi an empty session after an edit would be worse than not
    rebuilding at all: one prompt has to carry replay + ask, and it must be
    ONE prompt - prompting the replay on its own would make pi answer it"""
    backend, channel, client, _ = await _turn_backend(pi_backend_cls, conversation())
    await backend._mark_history_synced(channel)

    await channel.chat.messages.edit(0, {"role": "user", "content": "EDITED ask", "_metadata": {}})
    await channel.chat.messages.add({"role": "user", "content": "and now?", "_metadata": {}})

    await _send(backend, [{"role": "user", "content": "and now?"}])

    assert len(client.prompts) == 1, "the replay must ride along, not cost its own turn"
    assert "[CONTEXT REPLAY" in client.prompts[0]
    assert "EDITED ask" in client.prompts[0]
    assert "and now?" in client.prompts[0]
    assert backend._pending_recap is None

    # the next turn is a normal turn again - the replay is spent
    await channel.chat.messages.add({"role": "user", "content": "next thing", "_metadata": {}})
    await _send(backend, [{"role": "user", "content": "next thing"}])
    assert len(client.prompts) == 2
    assert "[CONTEXT REPLAY" not in client.prompts[1]


async def test_rebuild_still_carries_the_uploaded_image(pi_backend_cls):
    """a regenerated turn that had a photo attached must reach pi with the
    photo, replay and all - the default backend re-sends the store's blocks"""
    import base64
    pixel = base64.b64encode(b"\x89PNG\r\n\x1a\n fake-png").decode()
    uploaded = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{pixel}"}},
        ],
        "_metadata": {"filenames": ["", "shot.png"]},
    }
    backend, channel, client, _ = await _turn_backend(pi_backend_cls, conversation())
    await backend._mark_history_synced(channel)

    await channel.chat.messages.edit(0, {"role": "user", "content": "EDITED ask", "_metadata": {}})
    await channel.chat.messages.add(uploaded)

    await _send(backend, [{"role": "user", "content": "what is in this picture?"}])

    assert len(client.prompts) == 1
    assert "[CONTEXT REPLAY" in client.prompts[0] and "EDITED ask" in client.prompts[0]
    assert client.images[0], "the attachment has to go out with the rebuilt turn"
    assert client.images[0][0]["mimeType"] == "image/png"


async def test_ordinary_turns_never_rebuild(pi_backend_cls):
    """the channel appends the user message right before prompting; that is
    not an out-of-band edit. treating it as one would rebuild - and replay
    the whole store - in front of every single turn"""
    backend, channel, client, _ = await _turn_backend(pi_backend_cls, [])

    for i in range(3):
        message = {"role": "user", "content": f"ask {i}", "_metadata": {}}
        await channel.chat.messages.add(message)
        await _send(backend, [message])

    assert ("new_session",) not in client.calls
    assert all("[CONTEXT REPLAY" not in prompt for prompt in client.prompts)
    assert len(client.prompts) == 3


async def test_regenerate_after_a_finished_turn_rebuilds(pi_backend_cls):
    """the other side of that rule: once the answer is in the store, rewinding
    it (the webui's message_regenerate route) is exactly the change pi cannot
    know about - and the prompt-time watermark must not hide it"""
    backend, channel, client, _ = await _turn_backend(pi_backend_cls, [])

    message = {"role": "user", "content": "first ask", "_metadata": {}}
    await channel.chat.messages.add(message)
    await _send(backend, [message])
    assert ("new_session",) not in client.calls # clean first turn
    assert len(channel.messages) == 2 # pi's answer was persisted

    await channel.chat.messages.delete_from(1) # regenerate drops the answer...
    retry = {"role": "user", "content": "first ask", "_metadata": {}}
    await channel.chat.messages.add(retry) # ...and the route re-sends the ask
    await _send(backend, [retry])

    assert ("new_session",) in client.calls
    assert "[CONTEXT REPLAY" in client.prompts[-1]


async def test_regenerate_rewinding_to_a_previously_stamped_shape_rebuilds(pi_backend_cls):
    """the watermark is the single shape pi sits on, not a set of every shape
    it has ever passed through: rewinding to a conversation this session
    outgrew two turns ago is exactly the case that needs a rebuild"""
    backend, channel, client, _ = await _turn_backend(pi_backend_cls, [])

    for ask in ("first ask", "second ask"):
        message = {"role": "user", "content": ask, "_metadata": {}}
        await channel.chat.messages.add(message)
        await _send(backend, [message])
    assert ("new_session",) not in client.calls # two ordinary turns
    assert len(channel.messages) == 4 # u, a, u, a

    await channel.chat.messages.delete_from(2) # regenerate the second answer
    retry = {"role": "user", "content": "second ask", "_metadata": {}}
    await channel.chat.messages.add(retry)
    await _send(backend, [retry])

    assert ("new_session",) in client.calls
    assert "[CONTEXT REPLAY" in client.prompts[-1]
    assert "second answer" not in client.prompts[-1] # the discarded branch stays out
