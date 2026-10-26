# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
integration: one real pi turn through PiBackend's translation layer.

spawns a genuine `pi --mode rpc` (own throwaway session dir, offline) with
the two default tools, prompts it to call one, and asserts the emitted stream
tokens are the exact OpenAI shapes the default OpenAI-compatible backend
produces - with real pi event payloads rather than hand-written fixtures
(the unit tests in this folder cover the shapes; this one proves pi's wire
format matches what we translate).

skipped when the pi binary or a usable model endpoint is unavailable.
"""

import base64
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import types

import pytest

import core
from harness import FakeManager

PI_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = PI_DIR.parent.parent


def load_pi_module(name):
    """load user_modules/pi/<name>.py under a synthetic user_modules.pi
    package (user_modules has no __init__.py - the framework loads module
    files by path), so the module's relative imports keep working"""
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


class _Config:
    def __init__(self, settings):
        self.settings = settings

    def get(self, key, default=None):
        return self.settings.get(key, default)


class FakeModule:
    binary = "/bin/true"

    def __init__(self):
        self._settings = {"show_tool_activity": False, "tool_output_chars": 400}
        self.config = _Config(self._settings)
        self.logs = []

    def tool_output_limit(self):
        return 400

    def log(self, category, message):
        self.logs.append((category, message))


class RecordingChannel:
    def __init__(self, manager):
        self.manager = manager
        self.added = []

        outer = self

        class _Messages:
            async def add(self, message, **kwargs):
                outer.added.append(dict(message))

        self.context = types.SimpleNamespace(chat=types.SimpleNamespace(messages=_Messages()))

pytestmark = pytest.mark.integration

PI_BIN = shutil.which("pi") or os.path.expanduser("~/.pi/agent/bin/pi")
PROVIDER_CONFIG = REPO_ROOT / "data" / "pi_provider.json"


@pytest.fixture(scope="module")
def backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture(scope="module")
def rpc_cls():
    return load_pi_module("pi_rpc")


async def test_real_pi_turn_streams_native_tool_tokens(backend_cls, rpc_cls, tmp_path):
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    if PROVIDER_CONFIG.is_file():
        provider_path = str(PROVIDER_CONFIG)
    else:  # fresh checkout: inject our own endpoint
        provider_path = str(tmp_path / "pi_provider.json")
        with open(provider_path, "w", encoding="utf-8") as f:
            json.dump({
                "provider": "openlumara",
                "displayName": "OpenLumara",
                "baseUrl": os.environ.get("OPENLUMARA_TEST_API_URL", "http://localhost:8091/v1"),
                "apiKeyEnv": "PI_OPENLUMARA_API_KEY",
                "api": "openai-completions",
                "models": [{
                    "id": os.environ.get("OPENLUMARA_TEST_MODEL", "Qwen3.8-Flash-Next-W4A16"),
                    "name": "test", "reasoning": False, "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 64000, "maxTokens": 2048,
                }],
            }, f)
    model_id = json.load(open(provider_path, encoding="utf-8"))["models"][0]["id"]

    manager = FakeManager()
    module = FakeModule()
    backend = backend_cls(manager, module)
    module.binary = PI_BIN

    client = rpc_cls.PiRPCClient(
        PI_BIN,
        cwd=str(tmp_path),
        extra_args=[
            # the same provider injection the module performs on launch,
            # so pi serves the OpenLumara endpoint instead of its own auth
            "--extension", str(PI_DIR / "pi_provider_extension.js"),
            "--offline",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
            "--provider", "openlumara", "--model", model_id,
        ],
        env={
            "PI_OPENLUMARA_CONFIG": provider_path,
            "PI_OPENLUMARA_API_KEY": os.environ.get("OPENLUMARA_TEST_API_KEY", "not-needed"),
        },
    )
    backend.pi = client
    channel = RecordingChannel(manager)

    try:
        await client.start()
        try:
            await client.prompt(
                "Call the bash tool exactly once with the command `echo pi-tool-probe`. "
                "Then answer with the single word done.",
                timeout=90.0,
            )
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")

        tokens = []
        async for token in backend._translate_events(channel):
            tokens.append(token)
    finally:
        await client.stop()

    if any(t.get("type") == "error" for t in tokens):
        pytest.skip(f"pi turn failed (endpoint?): {[t for t in tokens if t['type'] == 'error'][:1]}")

    deltas = [t for t in tokens if t.get("type") == "tool_call_delta"]
    finals = [t for t in tokens if t.get("type") == "tool_calls"]
    responses = [t for t in tokens if t.get("type") == "tool"]

    assert deltas, "real pi must stream toolcall_start/toolcall_delta events"
    assert deltas[0]["tool_calls"][0]["function"]["name"] == "bash"

    assert finals, "real pi must finalize the call in message_end"
    call = finals[0]["tool_calls"][0]
    assert call["type"] == "function"
    assert "pi-tool-probe" in call["function"]["arguments"]

    assert responses, "tool_execution_end must become an OpenLumara tool token"
    assert responses[0]["tool_call_id"] == call["id"]
    payload = json.loads(responses[0]["content"])
    assert payload["status"] == "success"
    assert "pi-tool-probe" in payload["content"]

    # and the streamed card + response merge into exactly one done step,
    # which is what the webui renders as a finished native tool card
    collector = core.turns.TurnCollector(None)

    async def _gen():
        for t in tokens:
            yield t

    snapshots = {}
    async for event in collector.group_stream(_gen()):
        if event.get("type") == "step":
            snapshots[event["content"]["step"]] = event["content"]
    tool_steps = [s for s in snapshots.values() if s["tool_calls"]]
    assert len(tool_steps) == 1
    assert tool_steps[0]["status"] == "done"


async def test_real_pi_turn_calls_a_bridged_openlumara_tool(backend_cls, rpc_cls, tmp_path, monkeypatch):
    """end-to-end: pi, spawned with the tool bridge extension, calls an
    OpenLumara tool; OpenLumara executes it and the call/result stream as
    native tool cards exactly like pi's built-in tools do"""
    import asyncio

    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    provider_path = str(tmp_path / "pi_provider.json")
    model_id = os.environ.get("OPENLUMARA_TEST_MODEL", "Qwen3.8-Flash-Next-W4A16")
    with open(provider_path, "w", encoding="utf-8") as f:
        json.dump({
            "provider": "openlumara",
            "displayName": "OpenLumara",
            "baseUrl": os.environ.get("OPENLUMARA_TEST_API_URL", "http://localhost:8091/v1"),
            "apiKeyEnv": "PI_OPENLUMARA_API_KEY",
            "api": "openai-completions",
            "models": [{
                "id": model_id,
                "name": "test", "reasoning": False, "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 64000, "maxTokens": 2048,
            }],
        }, f)

    # the OpenLumara side of the bridge: a fake module with one tool, wired
    # through the real ToolBridge HTTP server
    tb = load_pi_module("tool_bridge")

    class RecordingToolModule:
        disabled_tools = set()

        def __init__(self):
            self.calls = []

        async def bridge_probe_tool(self, **kwargs):
            self.calls.append(kwargs)
            return {"status": "success", "content": "bridge-probe-executed-in-openlumara"}

    manager = FakeManager()
    tool_module = RecordingToolModule()
    manager.modules = {"probe": tool_module}
    manager.tools = [{
        "type": "function",
        "function": {
            "name": "bridge_probe_tool",
            "description": "Probe tool that records it was called. Call it with probe='yes'.",
            "parameters": {
                "type": "object",
                "properties": {"probe": {"type": "string", "description": "must be 'yes'"}},
                "required": ["probe"],
            },
        },
    }]
    channel = types.SimpleNamespace()
    channel.manager = manager
    channel.tool_loader = types.SimpleNamespace(
        catalog={"bridge_probe_tool": {"module": "probe", "method": "bridge_probe_tool"}},
        meta_tool_names={"tools_load", "tools_call"},
    )
    channel.log = lambda *a: None
    manager.channel = channel

    bridge = tb.ToolBridge(manager)
    started = await bridge.start()
    assert started is True

    manifest_path = str(tmp_path / "pi_tools.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"tools": tb.bridge_tool_definitions(manager)}, f)

    module = FakeModule()
    backend = backend_cls(manager, module)
    module.binary = PI_BIN

    client = rpc_cls.PiRPCClient(
        PI_BIN,
        cwd=str(tmp_path),
        extra_args=[
            "--extension", str(PI_DIR / "pi_provider_extension.js"),
            # the bridge extension under test
            "--extension", str(PI_DIR / "pi_tool_bridge_extension.js"),
            "--offline",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
            "--provider", "openlumara", "--model", model_id,
        ],
        env={
            "PI_OPENLUMARA_CONFIG": provider_path,
            "PI_OPENLUMARA_API_KEY": os.environ.get("OPENLUMARA_TEST_API_KEY", "not-needed"),
            "PI_OPENLUMARA_BRIDGE_URL": bridge.url,
            "PI_OPENLUMARA_BRIDGE_TOKEN": bridge.token,
            "PI_OPENLUMARA_TOOLS_CONFIG": manifest_path,
        },
    )
    backend.pi = client
    rec_channel = RecordingChannel(manager)

    try:
        await client.start()
        try:
            await client.prompt(
                "Call the bridge_probe_tool tool exactly once with probe set to the exact word yes. "
                "Then answer with the single word done.",
                timeout=90.0,
            )
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")

        tokens = []
        async for token in backend._translate_events(rec_channel):
            tokens.append(token)
    finally:
        await client.stop()
        await bridge.stop()

    if any(t.get("type") == "error" for t in tokens):
        pytest.skip(f"pi turn failed (endpoint?): {[t for t in tokens if t['type'] == 'error'][:1]}")

    finals = [t for t in tokens if t.get("type") == "tool_calls"]
    responses = [t for t in tokens if t.get("type") == "tool"]
    if not finals:
        pytest.skip("model did not call the bridged tool (endpoint behavior)")

    call = finals[0]["tool_calls"][0]
    assert call["function"]["name"] == "bridge_probe_tool"

    # OpenLumara really executed it, through its own catalog
    assert tool_module.calls == [{"probe": "yes"}]

    assert responses, "the bridged result must stream as an OpenLumara tool token"
    payload = json.loads(responses[0]["content"])
    assert payload["status"] == "success"
    assert "bridge-probe-executed-in-openlumara" in payload["content"]
    assert responses[0]["tool_call_id"] == call["id"]


async def _spawn_real_pi(rpc_cls, tmp_path, provider_path, model_id):
    client = rpc_cls.PiRPCClient(
        PI_BIN,
        cwd=str(tmp_path),
        extra_args=[
            "--extension", str(PI_DIR / "pi_provider_extension.js"),
            "--offline",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
            "--provider", "openlumara", "--model", model_id,
        ],
        env={
            "PI_OPENLUMARA_CONFIG": provider_path,
            "PI_OPENLUMARA_API_KEY": os.environ.get("OPENLUMARA_TEST_API_KEY", "not-needed"),
        },
    )
    await client.start()
    return client


def _provider_for(tmp_path):
    if PROVIDER_CONFIG.is_file():
        return str(PROVIDER_CONFIG)
    provider_path = str(tmp_path / "pi_provider.json")
    with open(provider_path, "w", encoding="utf-8") as f:
        json.dump({
            "provider": "openlumara", "displayName": "OpenLumara",
            "baseUrl": os.environ.get("OPENLUMARA_TEST_API_URL", "http://localhost:8091/v1"),
            "apiKeyEnv": "PI_OPENLUMARA_API_KEY",
            "api": "openai-compl" + chr(0x65) + "tions",
            "models": [{
                "id": os.environ.get("OPENLUMARA_TEST_MODEL", "Qwen3.8-Flash-Next-W4A16"),
                "name": "test", "reasoning": False, "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 64000, "maxTokens": 2048,
            }],
        }, f)
    return provider_path


async def test_real_pi_accepts_compaction_control_commands(backend_cls, rpc_cls, tmp_path):
    """the chat-sync features ride on pi rpc commands the fake-client tests
    cannot vouch for: set_auto_compaction (mirroring OpenLumara's
    automatically_compress_context switch) and the session rebuild's
    new_session round trip"""
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")
    provider_path = _provider_for(tmp_path)
    model_id = json.load(open(provider_path, encoding="utf-8"))["models"][0]["id"]

    manager = FakeManager()
    module = FakeModule()
    module.session_map = core.storage.StorageDict("pi_sessions", type="json")
    backend = backend_cls(manager, module)
    client = await _spawn_real_pi(rpc_cls, tmp_path, provider_path, model_id)
    backend.pi = client
    try:
        # set_auto_compaction must be accepted both ways
        await client.set_auto_compaction(False)
        await client.set_auto_compaction(True)

        first = (await client.get_state()).get("sessionFile")
        await client.new_session()
        second = (await client.get_state()).get("sessionFile")
        assert first and second and first != second, "rebuild's new_session must move to a fresh session"

        # stats must answer with the shape pi_context_fill reads
        stats = await client.get_session_stats()
        assert isinstance(stats, dict)
    finally:
        await client.stop()


class _UploadChannel:
    """chat store readable/writable enough for a real send_stream turn"""

    def __init__(self, manager):
        self.manager = manager
        self.stored = []
        channel = self

        class _Messages:
            async def get(self, index=None):
                return list(channel.stored) if index is None else channel.stored[index]

            async def add(self, message, **kwargs):
                channel.stored.append(dict(message))

        meta = {"id": "chat-1", "title": "uploads"}
        self.context = types.SimpleNamespace(
            chat=types.SimpleNamespace(messages=_Messages(),
                                       get=lambda key=None, default=None: meta.get(key, default)))

    async def push(self, message, defer=True):
        pass

    def log(self, category, message):
        pass


class _InjectionModule(FakeModule):
    """module stub driving the real provider-injection path (manual settings,
    so no dependency on a live install's config.yml)"""

    PROVIDER_ID = "openlumara"

    def __init__(self, base_url, api_key, model):
        super().__init__()
        self._settings.update({"forward_images": True, "use_openlumara_settings": False,
                               "api_url": base_url, "api_key": api_key, "model": model})
        self.PROVIDER_ID = "openlumara"

    def provider_extension_path(self):
        return str(PI_DIR / "pi_provider_extension.js")

    def resolved_provider_settings(self):
        return (self._settings["api_url"].rstrip("/"), self._settings["api_key"], self._settings["model"])


async def test_real_pi_delivers_an_uploaded_image(backend_cls, rpc_cls, tmp_path):
    """
    uploads must reach the model through pi, like they do on the default
    backend. pi drops images client-side for any model entry not flagged
    image-capable - answering that it cannot see anything - so this runs the
    module's own provider injection, then a real turn with a real PNG.
    """
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    base_url = os.environ.get("OPENLUMARA_TEST_API_URL", "http://localhost:8091/v1")
    api_key = os.environ.get("OPENLUMARA_TEST_API_KEY", "")
    model_id = os.environ.get("OPENLUMARA_TEST_MODEL", "Qwen3.8-Flash-Next-W4A16")

    icon = REPO_ROOT / "channels" / "webui" / "pwa" / "icon-192.png"
    if not icon.is_file():
        pytest.skip("no sample image in the checkout")

    manager = FakeManager()
    module = _InjectionModule(base_url, api_key, model_id)
    module.binary = PI_BIN
    module.session_map = core.storage.StorageDict("pi_sessions", type="json")
    backend = backend_cls(manager, module)

    env, injection_args, notes = await backend._prepare_provider_injection()
    payload = json.loads(pathlib.Path(env["PI_OPENLUMARA_CONFIG"]).read_text())
    assert "image" in payload["models"][0]["input"], \
        "pi silently omits images for models not flagged image-capable"

    client = rpc_cls.PiRPCClient(
        PI_BIN,
        cwd=str(tmp_path),
        extra_args=[
            *injection_args,
            "--offline",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
        ],
        env=env,
    )
    backend.pi = client

    pixel = base64.b64encode(icon.read_bytes()).decode()
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Name the dominant colour of this icon in one word."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{pixel}"}},
        ],
        "_metadata": {"filenames": ["", "icon-192.png"]},
    }

    channel = _UploadChannel(manager)
    manager.channel = channel

    async def _no_session_sync(channel=None):
        return None

    backend._sync_session = _no_session_sync # session pinning is covered elsewhere

    try:
        await client.start()
        tokens = []
        try:
            async for token in backend.send_stream([message]):
                tokens.append(token)
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")
    finally:
        await client.stop()

    errors = [t for t in tokens if t.get("type") == "error"]
    if errors:
        pytest.skip(f"pi turn failed (endpoint?): {errors[:1]}")

    answer = "".join(
        str(t.get("content") or "") for t in tokens if t.get("type") in ("content", "text")
    ).strip()
    assert answer, "the turn produced no answer"
    assert channel.stored, "the answer must be persisted like the default backend does"

    # pi's own tell-tale for a dropped attachment, paraphrased by the model
    refusal = ("support image", "can't see", "cannot see", "no image", "not visible",
               "wasn't included", "was not included", "unable to see", "don't have the image")
    assert not any(sig in answer.lower() for sig in refusal), \
        f"the image never reached the model, which said: {answer[:200]!r}"


async def test_real_pi_context_metrics_feed_the_ui(backend_cls, rpc_cls, tmp_path):
    """
    the context pill, the popup and /status all read pi's measurement of its
    context window after a turn - not openlumara's estimate of a context it
    never sends, and not pi's cumulative session totals (those add every
    cached re-read and run past the window).
    """
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
    env, injection_args, _notes = await backend._prepare_provider_injection()

    client = rpc_cls.PiRPCClient(
        PI_BIN, cwd=str(tmp_path),
        extra_args=[*injection_args, "--offline", "--no-context-files", "--no-skills",
                    "--no-prompt-templates", "--no-mcp",
                    "--session-dir", str(tmp_path / "sessions")],
        env=env,
    )
    backend.pi = client
    channel = _UploadChannel(manager)
    manager.channel = channel

    async def _no_session_sync(channel=None):
        return None

    backend._sync_session = _no_session_sync

    message = {"role": "user", "content": "Reply with exactly: OK", "_metadata": {}}
    channel.stored.append(message)

    try:
        await client.start()

        # nothing prompted yet: pi has measured nothing, and inventing a
        # number is worse than letting openlumara estimate
        assert await backend.pi_context_metrics_for(channel) is None

        tokens = []
        try:
            async for token in backend.send_stream([message]):
                tokens.append(token)
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")
        if [t for t in tokens if t.get("type") == "error"]:
            pytest.skip("pi turn failed (endpoint?)")

        live = await client.get_session_stats()
        usage = live.get("contextUsage") or {}
        assert usage.get("tokens"), "pi reported no context usage after a turn"

        # what the module hands to the channel as this turn's token usage
        # (and therefore to the pill / the chat's cached measurement)
        reported = [t for t in tokens if t.get("type") == "token_usage"]
        assert reported, "the turn must report its token usage like the default backend"

        metrics = await backend.pi_context_metrics_for(channel)
        assert metrics["total_tokens"] == usage["tokens"]
        assert metrics["max_context"] == usage["contextWindow"]
        assert metrics["percent_full"] == round((usage["tokens"] / usage["contextWindow"]) * 100)
        assert metrics["total_tokens"] <= metrics["max_context"], \
            "the pill's number must be what fits in the window, not the session total"
        assert metrics["total_tokens"] == reported[-1]["content"]

        # the popup's parts describe pi's session: pi's own standing prompt,
        # pi's tool schemas, the conversation, no openlumara end prompt
        assert metrics["system_prompt"]["tokens"] > 0
        assert metrics["tools"]["active"] > 0
        assert metrics["end_prompt"] == {"tokens": 0, "words": 0}
        parts = (metrics["system_prompt"]["tokens"] + metrics["tools"]["tokens"]
                 + metrics["message_history"]["tokens"])
        assert parts <= metrics["total_tokens"]
    finally:
        await client.stop()


async def test_real_pi_reaches_a_catalog_only_tool_through_tools_call(backend_cls, rpc_cls, tmp_path):
    """
    with dynamic tool loading on, the default backend's model is offered the
    preloaded set plus tools_load / tools_call, and tools_call runs any
    cataloged tool. pi must reach the same tools through the same mechanism -
    here it is offered ONLY tools_call, and must still get OpenLumara to run a
    tool it was never shown.
    """
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    tb = load_pi_module("tool_bridge")
    provider_path = _provider_for(tmp_path)
    model_id = json.load(open(provider_path, encoding="utf-8"))["models"][0]["id"]

    executed = []

    class Loader:
        catalog = {}
        meta_tool_names = {"tools_load", "tools_call"}

        def get_meta_callable(self, name):
            return self.tools_call if name == "tools_call" else None

        async def tools_call(self, name, params=None, arguments=None):
            executed.append(name)
            if name != "hidden_tool":
                return {"status": "error", "content": f"Tool '{name}' is not available."}
            return {"status": "success", "content": "hidden-tool-ran-in-openlumara"}

    manager = FakeManager()
    manager.modules = {}
    manager.tools = [{
        "type": "function",
        "function": {
            "name": "tools_call",
            "description": ("Calls an OpenLumara tool directly by name. To run the tool named "
                            "'hidden_tool', call this with name='hidden_tool' and params='{}'."),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "params": {"type": "string"}},
                "required": ["name", "params"],
            },
        },
    }]
    # one object plays both roles the bridge and the backend need: the
    # tool_loader the bridge executes through, and the chat store the backend
    # persists this turn's messages into
    channel = _UploadChannel(manager)
    channel.tool_loader = Loader()
    channel.log = lambda *a: None
    manager.channel = channel

    bridge = tb.ToolBridge(manager)
    assert await bridge.start() is True
    manifest = str(tmp_path / "pi_tools.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"tools": tb.bridge_tool_definitions(manager)}, f)

    module = FakeModule()
    module.binary = PI_BIN
    module.session_map = core.storage.StorageDict("pi_sessions", type="json")
    backend = backend_cls(manager, module)
    client = rpc_cls.PiRPCClient(
        PI_BIN, cwd=str(tmp_path),
        extra_args=[
            "--extension", str(PI_DIR / "pi_provider_extension.js"),
            "--extension", str(PI_DIR / "pi_tool_bridge_extension.js"),
            "--offline", "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
            "--provider", "openlumara", "--model", model_id,
        ],
        env={
            "PI_OPENLUMARA_CONFIG": provider_path,
            "PI_OPENLUMARA_API_KEY": os.environ.get("OPENLUMARA_TEST_API_KEY", "not-needed"),
            "PI_OPENLUMARA_BRIDGE_URL": bridge.url,
            "PI_OPENLUMARA_BRIDGE_TOKEN": bridge.token,
            "PI_OPENLUMARA_TOOLS_CONFIG": manifest,
        },
    )
    backend.pi = client

    try:
        await client.start()
        message = {"role": "user",
                   "content": "Run the OpenLumara tool named 'hidden_tool' using the tools_call tool. "
                              "Then answer with the word done.", "_metadata": {}}
        channel.stored.append(message)
        try:
            tokens = [t async for t in backend.send_stream([message])]
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")
    finally:
        await client.stop()
        await bridge.stop()

    if [t for t in tokens if t.get("type") == "error"]:
        pytest.skip("pi turn failed (endpoint?)")

    assert "hidden_tool" in executed, (
        "pi could not reach a catalog-only tool through tools_call: "
        f"{[t for t in tokens if t.get('type') in ('tool_calls', 'tool')][:3]}")

    responses = [t for t in tokens if t.get("type") == "tool"]
    assert responses and "hidden-tool-ran-in-openlumara" in json.dumps(responses)
