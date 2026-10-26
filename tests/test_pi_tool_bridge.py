# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
the tool bridge: OpenLumara tools exposed to pi and executed by OpenLumara.

intent under test:
- routing: while the pi backend is active, core's ToolcallManager.process()
  must stay out of the way - and the check must recognise PiBackend itself
  (PiBackend subclasses core.api.APIClient, so an isinstance-APIClient test
  silently let core execute pi's tool calls, pi's built-ins included, as
  OpenLumara tools; that regression is pinned here).
- execution: ToolBridge.execute_tool resolves tools through the same catalog
  entry / disabled list / loaded set the default chain uses, renders results
  the same way, and rejects unknown / meta / disabled / unloaded tools with
  the same wording.
- the loopback HTTP surface: bearer-token auth, GET /tools mirroring
  manager.tools, POST /execute round-tripping a real module tool.
- spawn wiring: the bridge extension and its env arrive with the provider
  injection; the tool manifest is written next to the provider config.
- robustness: the event translator never dereferences a vanished pi client
  (the 'NoneType' object has no attribute 'next_event' crash).
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

import aiohttp
import pytest

import core

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
PI_DIR = pathlib.Path(__file__).resolve().parent.parent


def load_pi_module(name):
    """load user_modules/pi-backend/<name>.py as part of a synthetic
    user_modules.pi package (user_modules has no __init__.py - the framework
    loads module files by path), so relative imports keep working"""
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


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeToolModule:
    """a loaded module instance with one callable tool"""

    def __init__(self, response=None, raises=False, sleeps=0.0):
        self.calls = []
        self.disabled_tools = set()
        self.response = response if response is not None else {"status": "success", "content": "ok"}
        self.raises = raises
        self.sleeps = sleeps

    async def sample_tool(self, **kwargs):
        self.calls.append(kwargs)
        if self.sleeps:
            await asyncio.sleep(self.sleeps)
        if self.raises:
            raise RuntimeError("kaboom")
        return self.response

    def result(self, content, success=True):
        return {"status": "success" if success else "error", "content": content}


class FakeToolLoader:
    def __init__(self, catalog):
        self.catalog = catalog
        self.meta_tool_names = {"tools_load", "tools_call"}


def make_manager(tool=None, response=None, raises=False):
    """a FakeManager wired like the live one: channel + tool_loader catalog +
    modules dict + the active tools array OpenLumara sends its own model"""
    from harness import FakeManager

    manager = FakeManager()
    module = tool if tool is not None else FakeToolModule(response=response, raises=raises)
    manager.modules = {"sample": module}
    manager.tools = [{
        "type": "function",
        "function": {
            "name": "sample_tool",
            "description": "a sample tool",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": []},
        },
    }]

    channel = types.SimpleNamespace()
    channel.manager = manager
    channel.tool_loader = FakeToolLoader({
        "sample_tool": {"module": "sample", "method": "sample_tool", "tool": manager.tools[0]},
    })
    channel.logs = []
    channel.log = lambda category, message: channel.logs.append((category, message))
    manager.channel = channel
    return manager, module


class FakePiModule:
    """the settings / logging surface ToolBridge and the injection path read"""

    binary = "/bin/true"

    class _Config:
        def __init__(self, settings):
            self.settings = settings

        def get(self, key, default=None):
            return self.settings.get(key, default)

    def __init__(self, manager=None, settings=None, bridge=None):
        self._settings = {
            "use_openlumara_settings": False,
            "api_url": "",
            "api_key": "",
            "model": "",
            "expose_tools": True,
            **(settings or {}),
        }
        self.config = FakePiModule._Config(self._settings)
        self.bridge = bridge
        self.logs = []
        self.binary = "/bin/true"

    def resolved_provider_settings(self):
        return (self._settings["api_url"], self._settings["api_key"], self._settings["model"])

    def provider_extension_path(self):
        return str(PI_DIR / "pi_provider_extension.js")

    def tool_bridge_extension_path(self):
        return str(PI_DIR / "pi_tool_bridge_extension.js")

    def resolved_cwd(self):
        return str(REPO_ROOT)

    def extra_args(self):
        return []

    def log(self, category, message):
        self.logs.append((category, message))


# ---------------------------------------------------------------------------
# routing: the toolcall guard must recognise PiBackend itself
# ---------------------------------------------------------------------------

def _load_backend_cls():
    return load_pi_module("backend").PiBackend


def _load_module_cls():
    return load_pi_module("module").Pi


def _apply_guard():
    module_cls = _load_module_cls()
    shell = module_cls.__new__(module_cls)
    shell._patches = []
    shell._patch_toolcalls()
    return shell


async def test_guard_skips_for_a_real_pi_backend_instance(fake_channel):
    """regression: PiBackend IS a core.api.APIClient subclass - the guard has
    to skip core's chain for it, not run pi's tool calls through OpenLumara"""
    # load backend BEFORE the guard so both share one class object (the
    # guard's isinstance check and this backend must be the same class)
    backend_cls = _load_backend_cls()
    shell = _apply_guard()
    try:
        from harness import FakeManager

        manager = FakeManager()
        pi_module = FakePiModule()
        backend = backend_cls(manager, pi_module)
        fake_channel.manager.API = backend

        # a catalog + module that WOULD execute the call if core ran
        runner = FakeToolModule(response={"status": "success", "content": "executed"})
        fake_channel.tool_loader = FakeToolLoader({
            "bash": {"module": "sample", "method": "sample_tool", "tool": {}},
        })
        fake_channel.manager.modules = {"sample": runner}

        recorder = []

        class _Messages:
            async def add(self, message, **kwargs):
                recorder.append(dict(message))

        fake_channel.context = types.SimpleNamespace(chat=types.SimpleNamespace(messages=_Messages()))

        message = {
            "role": "assistant",
            "tool_calls": [{"id": "x1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
        }
        tc = core.toolcalls.ToolcallManager(fake_channel)
        tokens = [t async for t in tc.process(message)]

        assert tokens == [], "the pi backend must not have its calls executed by core"
        assert runner.calls == [], "the OpenLumara tool behind the name must not have run"
        assert recorder == [], "core must not persist anything for pi's calls"
    finally:
        for cls, name, original in reversed(shell._patches):
            setattr(cls, name, original)


async def test_guard_still_delegates_for_the_default_backend(fake_channel):
    """the default OpenAI-compatible chain stays untouched while pi is off"""
    _load_backend_cls()  # keep one shared backend class for the guard
    shell = _apply_guard()
    try:
        class DefaultLikeAPI(core.api.APIClient):
            def __init__(self):
                self.connected = True
                self.cancel_request = False

        fake_channel.manager.API = DefaultLikeAPI()
        fake_channel.tool_loader = FakeToolLoader({})
        fake_channel.manager.modules = {}

        recorder = []

        class _Messages:
            async def add(self, message, **kwargs):
                recorder.append(dict(message))

        async def _false():
            return False

        fake_channel.context = types.SimpleNamespace(
            chat=types.SimpleNamespace(messages=_Messages()),
            is_over_threshold=_false,
            compress=_false,
        )

        message = {
            "role": "assistant",
            "tool_calls": [{"id": "x1", "function": {"name": "nope", "arguments": "{}"}}],
        }
        tc = core.toolcalls.ToolcallManager(fake_channel)
        tokens = [t async for t in tc.process(message)]
        assert any(t.get("type") == "tool" for t in tokens), "the original chain must still run"
    finally:
        for cls, name, original in reversed(shell._patches):
            setattr(cls, name, original)


# ---------------------------------------------------------------------------
# ToolBridge.execute_tool: OpenLumara executes the bridged call
# ---------------------------------------------------------------------------

async def test_execute_tool_runs_catalog_tool_and_renders_result():
    tb = load_pi_module("tool_bridge")
    manager, module = make_manager(response={"status": "success", "content": "file list"})
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("sample_tool", {"value": "x"})
    # a module.result() dict is flattened to its model-facing text, exactly
    # what the default chain puts in the tool message (cards match byte for
    # byte once the backend wraps it back into {"status", "content"})
    assert result == {"status": "success", "content": "file list"}
    assert module.calls == [{"value": "x"}]


async def test_execute_tool_string_result_passes_through():
    tb = load_pi_module("tool_bridge")
    manager, _ = make_manager(response="plain text")
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("sample_tool", {})
    assert result == {"status": "success", "content": "plain text"}


async def test_execute_tool_error_result_is_flagged():
    tb = load_pi_module("tool_bridge")
    manager, _ = make_manager(response={"status": "error", "content": "bad args"})
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("sample_tool", {})
    assert result["status"] == "error"
    assert "bad args" in result["content"]


async def test_execute_tool_rejects_unknown_meta_disabled_and_unloaded():
    tb = load_pi_module("tool_bridge")

    manager, module = make_manager()
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("bash", {})
    assert result["status"] == "error"
    assert "does not exist" in result["content"]

    result = await bridge.execute_tool("tools_load", {})
    assert result["status"] == "error"
    assert "Dynamic tool loading is disabled" in result["content"], \
        "a meta tool pi should not have must say what the default chain says"

    module.disabled_tools = {"sample_tool"}
    result = await bridge.execute_tool("sample_tool", {})
    assert "disabled" in result["content"]
    module.disabled_tools = set()

    manager.tools = []  # not in the loaded set anymore
    result = await bridge.execute_tool("sample_tool", {})
    assert result["status"] == "error"
    assert "not available" in result["content"]


async def test_execute_tool_timeout_and_crash(monkeypatch):
    tb = load_pi_module("tool_bridge")

    manager, _ = make_manager(raises=True)
    bridge = tb.ToolBridge(manager)
    result = await bridge.execute_tool("sample_tool", {})
    assert result["status"] == "error"
    assert "kaboom" in result["content"]

    manager, _ = make_manager(response={"status": "success", "content": "late"})
    bridge = tb.ToolBridge(manager)
    monkeypatch.setattr(core.config, "get", lambda *a, **k: 0.05)
    slow = FakeToolModule(sleeps=1.0)
    slow.disabled_tools = set()
    manager.modules = {"sample": slow}
    result = await bridge.execute_tool("sample_tool", {})
    assert result["status"] == "error"
    assert "timed out" in result["content"]


# ---------------------------------------------------------------------------
# the loopback HTTP surface
# ---------------------------------------------------------------------------

async def test_bridge_http_roundtrip():
    tb = load_pi_module("tool_bridge")
    manager, module = make_manager(response={"status": "success", "content": "hello from openlumara"})
    bridge = tb.ToolBridge(manager)
    started = await bridge.start()
    assert started is True and bridge.url

    try:
        async with aiohttp.ClientSession() as session:
            # no token -> refused
            async with session.get(bridge.url + "/tools") as resp:
                assert resp.status == 401

            headers = {"Authorization": f"Bearer {bridge.token}"}
            async with session.get(bridge.url + "/tools", headers=headers) as resp:
                assert resp.status == 200
                tools = (await resp.json())["tools"]
                assert [t["name"] for t in tools] == ["sample_tool"]
                assert tools[0]["parameters"]["type"] == "object"

            async with session.post(
                bridge.url + "/execute",
                headers=headers,
                json={"tool": "sample_tool", "arguments": {"value": "v"}},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "success"
                assert "hello from openlumara" in data["content"]
                assert module.calls == [{"value": "v"}]

            async with session.post(
                bridge.url + "/execute", headers=headers, json={"tool": "bash", "arguments": {}}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "error"
                assert "does not exist" in data["content"]
    finally:
        await bridge.stop()
    assert bridge.url is None


def test_bridge_tool_definitions_mirrors_active_tools():
    tb = load_pi_module("tool_bridge")
    manager, _ = make_manager()
    manager.tools.append({"type": "function", "function": {"name": "tools_load", "parameters": {}}})
    manager.tools.append({"type": "function", "function": {"name": "broken"}})

    defs = tb.bridge_tool_definitions(manager)
    names = [d["name"] for d in defs]
    # the dynamic-tool-loading meta tools are part of the array the default
    # backend's model is offered (tools_call is how it reaches the rest of the
    # catalog), so pi must be offered them too; only nameless entries drop out
    assert names == ["sample_tool", "tools_load", "broken"]
    by_name = {d["name"]: d for d in defs}
    assert by_name["sample_tool"]["description"] == "a sample tool"
    assert by_name["sample_tool"]["parameters"]["properties"]["value"]["type"] == "string"
    # a definition with no usable schema still gets a valid empty one
    assert by_name["broken"]["parameters"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# spawn wiring: bridge extension + env + manifest
# ---------------------------------------------------------------------------

async def test_spawn_injection_carries_the_bridge(tmp_path, monkeypatch):
    backend_cls = _load_backend_cls()
    from harness import FakeManager

    manager = FakeManager()
    manager.tools = [{
        "type": "function",
        "function": {"name": "sample_tool", "description": "d",
                     "parameters": {"type": "object", "properties": {}}},
    }]

    class _Bridge:
        url = "http://127.0.0.1:9"
        token = "test-token"

    pi_module = FakePiModule(bridge=_Bridge())
    backend = backend_cls(manager, pi_module)

    monkeypatch.setattr(core, "get_data_path", lambda name: str(tmp_path / name))

    env, args, note = await backend._prepare_provider_injection()
    assert env["PI_OPENLUMARA_BRIDGE_URL"] == "http://127.0.0.1:9"
    assert env["PI_OPENLUMARA_BRIDGE_TOKEN"] == "test-token"
    assert "--extension" in args
    assert str(PI_DIR / "pi_tool_bridge_extension.js") in args
    assert "bridged" in (note or "")

    manifest = json.load(open(tmp_path / "pi_tools.json", encoding="utf-8"))
    assert [t["name"] for t in manifest["tools"]] == ["sample_tool"]
    assert env["PI_OPENLUMARA_TOOLS_CONFIG"].endswith("pi_tools.json")


async def test_spawn_injection_without_bridge(tmp_path, monkeypatch):
    backend_cls = _load_backend_cls()
    from harness import FakeManager

    backend = backend_cls(FakeManager(), FakePiModule(bridge=None))
    monkeypatch.setattr(core, "get_data_path", lambda name: str(tmp_path / name))

    env, args, note = await backend._prepare_provider_injection()
    assert "PI_OPENLUMARA_BRIDGE_URL" not in (env or {})
    assert "pi_tool_bridge_extension.js" not in " ".join(args)


# ---------------------------------------------------------------------------
# the vanished-client crash ('NoneType' object has no attribute 'next_event')
# ---------------------------------------------------------------------------

async def test_translate_events_without_client_yields_error_token():
    backend_cls = _load_backend_cls()
    from harness import FakeManager

    backend = backend_cls(FakeManager(), FakePiModule())
    backend.pi = None

    tokens = [t async for t in backend._translate_events(None)]
    assert len(tokens) == 1
    assert tokens[0]["type"] == "error"
    assert "not running" in tokens[0]["content"]


async def test_translate_events_survives_client_replacement():
    """a settings restart swapping self.pi mid-turn must not crash the
    running turn: it keeps draining the client it started"""
    backend_cls = _load_backend_cls()
    from harness import FakeManager

    class ScriptedClient:
        alive = True

        def __init__(self, events):
            self._events = list(events)

        async def next_event(self, timeout=None):
            if not self._events:
                raise asyncio.TimeoutError
            return self._events.pop(0)

    backend = backend_cls(FakeManager(), FakePiModule())
    events = [{"type": "agent_settled", "aborted": False}]
    client = ScriptedClient(events)
    backend.pi = None  # self.pi already swapped away - the pinned client saves us

    tokens = [t async for t in backend._translate_events(None, client=client)]
    assert tokens == []


# ---------------------------------------------------------------------------
# dynamic tool loading: pi reaches the whole catalog the same way the default
# backend's model does - through openlumara's own tools_load / tools_call
# ---------------------------------------------------------------------------

class MetaToolLoader:
    """ToolLoader surface the meta tools need: the two meta callables plus the
    catalog, exactly as core/tool_loader.py wires them"""

    def __init__(self, catalog, *, hidden_result="ran a catalog-only tool"):
        self.catalog = catalog
        self.meta_tool_names = {"tools_load", "tools_call"}
        self.calls = []
        self.hidden_result = hidden_result

    def get_meta_callable(self, tool_name):
        if tool_name in ("tools_load", "tools_call"):
            return getattr(self, tool_name)
        return None

    async def tools_load(self, module_names):
        self.calls.append(("tools_load", tuple(module_names)))
        return {"status": "success", "content": f"loaded {list(module_names)}"}

    async def tools_call(self, name, params=None, arguments=None):
        """core's cache-stable invocation path: runs a cataloged tool that is
        not in the active array at all"""
        self.calls.append(("tools_call", name, params, arguments))
        if name == "hidden_tool":
            return {"status": "success", "content": self.hidden_result}
        return {"status": "error", "content": f"Tool '{name}' is not available."}


def _dynamic_manager():
    tb = load_pi_module("tool_bridge")
    manager, module = make_manager()
    manager.tools.append({
        "type": "function",
        "function": {"name": "tools_load", "description": "load tools",
                     "parameters": {"type": "object", "properties": {"module_names": {"type": "array"}}}},
    })
    manager.tools.append({
        "type": "function",
        "function": {"name": "tools_call", "description": "call a tool",
                     "parameters": {"type": "object", "properties": {"name": {"type": "string"},
                                                                     "params": {"type": "string"}}}},
    })
    loader = MetaToolLoader(dict(manager.channel.tool_loader.catalog))
    manager.channel.tool_loader = loader
    return tb, manager, module, loader


async def test_meta_tools_are_offered_to_pi_when_dynamic_loading_is_on():
    tb, manager, _module, _loader = _dynamic_manager()
    names = [d["name"] for d in tb.bridge_tool_definitions(manager)]
    assert "tools_load" in names and "tools_call" in names


async def test_tools_load_runs_openlumaras_loader():
    tb, manager, _module, loader = _dynamic_manager()
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("tools_load", {"module_names": ["memory"]})

    assert loader.calls == [("tools_load", ("memory",))]
    assert result == {"status": "success", "content": "loaded ['memory']"}


async def test_tools_call_reaches_a_tool_pi_was_never_offered():
    """the point of tools_call: the model can invoke a cataloged tool that is
    not in the active array - pi must be able to as well, or it would have
    fewer tools than the default backend"""
    tb, manager, _module, loader = _dynamic_manager()
    bridge = tb.ToolBridge(manager)

    assert "hidden_tool" not in manager.channel.tool_loader.catalog
    result = await bridge.execute_tool("tools_call", {"name": "hidden_tool", "params": '{"path": "x"}'})

    assert loader.calls == [("tools_call", "hidden_tool", '{"path": "x"}', None)]
    assert result == {"status": "success", "content": "ran a catalog-only tool"}


async def test_meta_tool_errors_are_not_masked():
    tb, manager, _module, loader = _dynamic_manager()
    bridge = tb.ToolBridge(manager)

    result = await bridge.execute_tool("tools_call", {"name": "nope", "params": "{}"})
    assert result["status"] == "error" and "nope" in result["content"]


# ---------------------------------------------------------------------------
# registration report: a tool pi refuses must be named, not silently absent
# ---------------------------------------------------------------------------

async def test_bridge_records_what_pi_registered_and_refused():
    import aiohttp

    tb = load_pi_module("tool_bridge")
    manager, _ = make_manager()
    seen = []

    async def on_registry(payload):
        seen.append(payload)

    bridge = tb.ToolBridge(manager)
    bridge.on_registry = on_registry
    assert await bridge.start() is True
    try:
        headers = {"Authorization": f"Bearer {bridge.token}"}
        payload = {"registered": ["sample_tool"],
                   "skipped": [{"name": "read", "reason": "shadows a built-in tool"}]}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{bridge.url}/registered", json=payload, headers=headers) as resp:
                assert resp.status == 200
    finally:
        await bridge.stop()

    assert bridge.registered == ["sample_tool"]
    assert bridge.skipped == [{"name": "read", "reason": "shadows a built-in tool"}]
    assert seen == [payload]


async def test_registration_report_needs_the_bridge_token():
    import aiohttp

    tb = load_pi_module("tool_bridge")
    manager, _ = make_manager()
    bridge = tb.ToolBridge(manager)
    assert await bridge.start() is True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{bridge.url}/registered", json={"registered": []}) as resp:
                assert resp.status == 401
        assert bridge.registered == []
    finally:
        await bridge.stop()
