# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
pi backend: tool calls must reach the UI exactly like the default backend's.

The module lives outside core, so everything it changes in the framework is
a documented, restorable patch of its own (module._patch_toolcalls) - these
tests exercise the module through that patch, never through a modified core.

intent under test:
- the module's ToolcallManager.process() wrapper (the framework's tool
  executor) is skipped while the pi backend is active, so pi's tool calls
  are never executed a second time; a default OpenAI-compatible backend
  (an APIClient) passes straight through to the original chain.
- PiBackend translates pi's rpc events into the exact OpenAI-shaped stream
  tokens the default backend streams: tool_call_delta per streamed argument
  chunk, one tool_calls token per finalized assistant message, tool per
  finished call - so core.turns / every channel renders native tool cards.
- the turn's messages are persisted the way the default chain persists them
  (assistant message with content/reasoning, tool calls on the assistant
  message, tool result messages), so reloaded history rebuilds the cards.
- group_stream (the webui's live path over the very same tokens) folds the
  response into the card with the matching id, exactly like with the default
  backend.
- robustness: responses for calls that never streamed a card (nested /
  aborted / joined-late) still synthesize one instead of dangling.
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

import pytest

import core
from harness import FakeManager

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
PI_DIR = pathlib.Path(__file__).resolve().parent.parent


def load_pi_module(name):
    """load user_modules/pi/<name>.py as part of a synthetic user_modules.pi
    package, so the module's own relative imports keep working (user_modules
    has no __init__.py - the framework loads modules by file path)"""
    pkg = "user_modules.pi"
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_loader(pkg, loader=None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(PI_DIR)]
        sys.modules[pkg] = package
    full = f"{pkg}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PI_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pi_backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture
def patched_toolcalls():
    """apply the module's ToolcallManager patch the same way Pi.on_ready
    does, and restore the original afterwards (module reload safety)"""
    module_cls = load_pi_module("module").Pi
    shell = module_cls.__new__(module_cls)  # patching needs no manager
    shell._patches = []
    shell._patch_toolcalls()
    yield shell._patches
    for cls, name, original in reversed(shell._patches):
        setattr(cls, name, original)


class DefaultLikeAPI(core.api.APIClient):
    """an active backend that IS the default OpenAI-compatible client type
    (the harness's plain FakeAPI is deliberately not one); only the handful
    of attributes the original chain reads are set"""

    def __init__(self, manager=None):
        self.connected = True
        self.cancel_request = False


class FakePiClient:
    """PiRPCClient surface the translation path touches"""

    def __init__(self, events):
        self._events = list(events)
        self.alive = True

    async def next_event(self, timeout=None):
        if not self._events:
            raise asyncio.TimeoutError("no more events")
        return self._events.pop(0)

    async def get_state(self):
        return {"model": {}}

    async def get_session_stats(self):
        return {}

    def clear_events(self):
        pass


class FakeModule:
    """PiBackend reads only these bits of its module"""

    binary = "/bin/true"

    class _Config:
        def __init__(self, settings):
            self.settings = settings

        def get(self, key, default=None):
            return self.settings.get(key, default)

    def __init__(self, settings=None):
        self._settings = {
            "show_tool_activity": False,
            "forward_images": True,
            "tool_output_chars": 400,
            **(settings or {}),
        }
        self.config = FakeModule._Config(self._settings)
        self.logs = []

    def tool_output_limit(self):
        return self._settings.get("tool_output_chars", 400)

    def log(self, category, message):
        self.logs.append((category, message))


def _false_coro():
    async def _c():
        return False
    return _c()


class RecorderMessages:
    def __init__(self, sink):
        self.sink = sink

    async def add(self, message, **kwargs):
        self.sink.append(dict(message))


class RecordingChannel:
    """only channel.context.chat.messages.add() is touched (message persist)"""

    def __init__(self, manager):
        self.manager = manager
        self.added = []
        self.context = types.SimpleNamespace(
            chat=types.SimpleNamespace(messages=RecorderMessages(self.added))
        )


async def make_backend(pi_backend_cls, pi_events, settings=None):
    manager = FakeManager()
    module = FakeModule(settings)
    backend = pi_backend_cls(manager, module)
    backend.pi = FakePiClient(pi_events)
    return backend, RecordingChannel(manager), module


async def run_turn(backend, channel):
    """translate a full pi turn (events end with agent_settled) and persist
    the trailing answer the way send_stream does; returns the stream tokens"""
    tokens = [t async for t in backend._translate_events(channel)]
    if not any(t.get("type") == "error" for t in tokens):
        final_message = {"role": "assistant", "content": "".join(backend._turn_text)}
        if backend._turn_reasoning:
            final_message["reasoning_content"] = "".join(backend._turn_reasoning)
        if final_message["content"] or backend._turn_reasoning:
            await backend._persist_assistant(channel, final_message)
    return tokens


# ---------------------------------------------------------------------------
# the module's toolcall guard: no double execution
# ---------------------------------------------------------------------------

async def test_process_wrapper_skips_while_pi_is_the_backend(patched_toolcalls, fake_channel, pi_backend_cls):
    """while the pi backend is swapped in, OpenLumara's toolcall chain must
    not run pi's tool calls a second time. the backend here is a real
    PiBackend instance on purpose: PiBackend subclasses core.api.APIClient,
    so the guard must recognise the pi backend itself, not just 'not an
    APIClient'"""

    from harness import FakeManager

    fake_channel.manager.API = pi_backend_cls(FakeManager(), FakeModule())
    fake_channel.tool_loader = object()  # touching it would blow up

    tc = core.toolcalls.ToolcallManager(fake_channel)
    message = {
        "role": "assistant",
        "tool_calls": [
            {"id": "x1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
        ],
    }
    assert [t async for t in tc.process(message)] == []


async def test_process_wrapper_delegates_to_the_default_backend(patched_toolcalls, fake_channel):
    """a default OpenAI-compatible backend (an APIClient) must get the
    untouched original chain (unknown tool here, but the chain clearly runs)"""
    fake_channel.manager.API = DefaultLikeAPI(fake_channel.manager)
    fake_channel.tool_loader = types.SimpleNamespace(catalog={}, meta_tool_names=set())
    recorder = RecordingChannel(fake_channel.manager)
    fake_channel.context = types.SimpleNamespace(
        chat=recorder.context.chat,
        is_over_threshold=lambda: _false_coro(),
        compress=lambda: _false_coro(),
    )
    fake_channel.manager.modules = {}

    tc = core.toolcalls.ToolcallManager(fake_channel)
    message = {
        "role": "assistant",
        "tool_calls": [{"id": "x1", "function": {"name": "nope", "arguments": "{}"}}],
    }
    tokens = [t async for t in tc.process(message)]
    assert any(t.get("type") == "tool" for t in tokens)


async def test_process_wrapper_patch_is_restored(patched_toolcalls, fake_channel):
    """unloading the module must put the framework back together"""
    original = patched_toolcalls[0][2]
    shell_cls = load_pi_module("module").Pi
    shell = shell_cls.__new__(shell_cls)
    shell._patches = list(patched_toolcalls)
    for cls, name, fn in reversed(shell._patches):
        setattr(cls, name, fn)
    assert core.toolcalls.ToolcallManager.process is original


# ---------------------------------------------------------------------------
# pi event -> openlumara token translation
# ---------------------------------------------------------------------------

def _pi_turn_events():
    """one pi turn: streamed reasoning, a streamed tool call, its finalized
    message, its execution, and a final answer message"""
    return [
        {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "listing dir"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_start", "contentIndex": 1, "id": "call_1", "toolName": "bash"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1, "delta": '{"comma'}},
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1, "delta": 'nd": "ls -la"}'}},
        {"type": "message_end", "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "listing dir"},
                {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls -la"}},
            ],
        }},
        {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash", "args": {"command": "ls -la"}},
        {"type": "tool_execution_end", "toolCallId": "call_1", "toolName": "bash",
         "result": {"content": [{"type": "text", "text": "file.txt"}], "details": {"exit": 0}}, "isError": False},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "done: one file"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "done: one file"}]}},
        {"type": "agent_settled", "aborted": False},
    ]


async def test_pi_tools_stream_as_native_tool_tokens(pi_backend_cls):
    backend, channel, _ = await make_backend(pi_backend_cls, _pi_turn_events())
    tokens = await run_turn(backend, channel)

    # deltas carry the growing argument buffer, like core/api.py does
    deltas = [t for t in tokens if t.get("type") == "tool_call_delta"]
    assert len(deltas) == 3  # start + 2 arg chunks
    assert deltas[0]["tool_calls"][0]["id"] == "call_1"
    assert deltas[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert json.loads(deltas[-1]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls -la"}

    # one finalized, OpenAI-shaped tool_calls token per assistant message
    final_calls = [t for t in tokens if t.get("type") == "tool_calls"]
    assert len(final_calls) == 1
    call = final_calls[0]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls -la"}

    # the response token mirrors core/toolcalls.py's {"status", "content"} json
    tool_tokens = [t for t in tokens if t.get("type") == "tool"]
    assert len(tool_tokens) == 1
    assert tool_tokens[0]["tool_call_id"] == "call_1"
    parsed = json.loads(tool_tokens[0]["content"])
    assert parsed["status"] == "success"
    assert parsed["content"] == "file.txt"

    # ordering: reasoning -> argument deltas -> finalized calls -> response
    order = [t["type"] for t in tokens if t["type"] in ("reasoning", "tool_call_delta", "tool_calls", "tool", "content")]
    assert order == ["reasoning", "tool_call_delta", "tool_call_delta", "tool_call_delta",
                     "tool_calls", "tool", "content"]


async def test_pi_turn_persists_default_shaped_messages(pi_backend_cls):
    backend, channel, _ = await make_backend(pi_backend_cls, _pi_turn_events())
    await run_turn(backend, channel)

    stored = channel.added
    # intermediate message (reasoning + tool calls) then the final answer:
    # the same message shapes the default chain writes
    assert [m.get("role") for m in stored] == ["assistant", "assistant"]
    assert stored[0]["reasoning_content"] == "listing dir"
    assert stored[1]["content"] == "done: one file"

    calls_msgs = [m for m in stored if m.get("tool_calls")]
    assert len(calls_msgs) == 1
    assert calls_msgs[0]["tool_calls"][0]["id"] == "call_1"
    assert calls_msgs[0]["tool_calls"][0]["function"]["name"] == "bash"


async def test_stream_and_history_grouping_agree(pi_backend_cls):
    """the equality claim: grouping pi's streamed tokens through
    TurnCollector.group_stream must produce the same tool card (name, args,
    response, status) as grouping stored messages through group_history -
    which is exactly what the default backend produces for both"""
    backend, channel, _ = await make_backend(pi_backend_cls, _pi_turn_events())
    tokens = await run_turn(backend, channel)

    collector = core.turns.TurnCollector(None)

    async def _gen():
        for t in tokens:
            yield t

    live_snapshots = {}
    async for event in collector.group_stream(_gen()):
        if event.get("type") == "step":
            # the webui renders by full replacement, keyed by step number
            live_snapshots[event["content"]["step"]] = event["content"]
    tool_steps = [s for s in live_snapshots.values() if s["tool_calls"]]
    assert len(tool_steps) == 1
    card = tool_steps[0]["tool_calls"][0]
    expected_response = json.dumps(
        {"status": "success", "content": "file.txt", "details": {"exit": 0}}, ensure_ascii=False
    )
    assert card["_metadata"]["response"] == expected_response
    assert card["_metadata"]["args_parsed"] == {"command": "ls -la"}
    assert tool_steps[0]["status"] == "done"

    # the same turn rebuilt from persisted history (assistant tool_calls +
    # tool result) yields an identical card
    history = [
        {"role": "user", "content": "list it"},
        {"role": "assistant", "content": "", "reasoning_content": "listing dir",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "bash", "arguments": '{"command": "ls -la"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": expected_response},
        {"role": "assistant", "content": "done: one file"},
    ]
    turns = await collector.group_history(history)
    hist_steps = [s for s in turns[1]["steps"] if s["tool_calls"]]
    hist_card = hist_steps[0]["tool_calls"][0]
    assert hist_card["_metadata"]["response"] == card["_metadata"]["response"]
    assert hist_card["_metadata"]["args_parsed"] == card["_metadata"]["args_parsed"]
    assert hist_steps[0]["status"] == "done"


async def test_response_without_card_synthesizes_one(pi_backend_cls):
    """nested / joined-late calls have no streamed card: the response must
    still render, so a tool_calls token is synthesized ahead of it"""
    events = [
        {"type": "tool_execution_end", "toolCallId": "nested_1", "toolName": "bash",
         "result": {"content": [{"type": "text", "text": "out"}]}, "isError": False},
        {"type": "agent_settled", "aborted": False},
    ]
    backend, channel, _ = await make_backend(pi_backend_cls, events)
    tokens = [t async for t in backend._translate_events(channel)]

    assert tokens[0]["type"] == "tool_calls"
    assert tokens[0]["tool_calls"][0]["id"] == "nested_1"
    assert tokens[1]["type"] == "tool"
    assert tokens[1]["tool_call_id"] == "nested_1"


async def test_failed_tool_marks_status_error(pi_backend_cls):
    events = [
        {"type": "message_update", "assistantMessageEvent": {"type": "toolcall_start", "contentIndex": 0, "id": "c9", "toolName": "host_shell"}},
        {"type": "tool_execution_end", "toolCallId": "c9", "toolName": "host_shell",
         "result": {"content": [{"type": "text", "text": "boom"}]}, "isError": True},
        {"type": "agent_settled", "aborted": False},
    ]
    backend, channel, _ = await make_backend(pi_backend_cls, events)
    tokens = [t async for t in backend._translate_events(channel)]

    tool_token = next(t for t in tokens if t.get("type") == "tool")
    assert json.loads(tool_token["content"])["status"] == "error"


async def test_activity_notes_off_by_default_on_by_setting(pi_backend_cls):
    events = [
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {}},
        {"type": "agent_settled", "aborted": False},
    ]
    backend, channel, _ = await make_backend(pi_backend_cls, events)
    tokens = [t async for t in backend._translate_events(channel)]
    assert [t for t in tokens if t.get("type") == "reasoning"] == []

    backend, channel, _ = await make_backend(pi_backend_cls, events, settings={"show_tool_activity": True})
    tokens = [t async for t in backend._translate_events(channel)]
    notes = [t for t in tokens if t.get("type") == "reasoning"]
    assert len(notes) == 1
    assert "bash" in notes[0]["content"]
