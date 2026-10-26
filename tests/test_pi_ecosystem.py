# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
pi's own configuration and plugin ecosystem, as seen from OpenLumara.

pi reads its config folder (~/.pi/agent: settings.json, auth.json, the
extensions/, skills/ and prompts/ folders, packages) with no help from us -
that is what makes an existing pi setup carry over. What the module must
actually get right is the three things around it:

- the rpc client answers the extension UI dialog subprotocol: ctx.ui
  select/confirm/input/editor block inside pi until a client responds, and
  OpenLumara has no dialog widget - un-answered, a plain permission-gate
  plugin freezes the turn forever (the documented pattern awaits
  ctx.ui.confirm in a tool_call handler). dialogs must be cancelled the
  moment they arrive, fire-and-forget methods must not get a response, and
  every record must stay visible (callback + event queue, not swallowed);
- pi_agent_dir reaches the child as PI_CODING_AGENT_DIR (expanded,
  absolute) and project_trust maps to --approve / --no-approve, with
  'auto' adding nothing so pi decides by its own saved decisions;
- the new settings exist, explain themselves to the settings menu, and are
  part of the spawn fingerprint that triggers the restart-on-change.
"""

import asyncio
import json
import os
import pathlib
import types

import pytest

from test_pi_tool_stream import load_pi_module

PI_DIR = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# the rpc client's extension UI subprotocol
# ---------------------------------------------------------------------------

class FakeStdin:
    def __init__(self):
        self.lines = []

    def write(self, data: bytes):
        self.lines.append(bytes(data))

    def is_closing(self):
        return False

    async def drain(self):
        pass


class FakeProcess:
    def __init__(self):
        self.stdin = FakeStdin()
        self.returncode = None


def _client(rpc_cls, on_ui_request=None):
    """a PiRPCClient that is 'running' against a recorded stdin, so the
    dispatch layer can be exercised without a subprocess"""
    client = rpc_cls.PiRPCClient("/bin/true", on_ui_request=on_ui_request)
    client.alive = True
    client.process = FakeProcess()
    return client


def _written(client):
    return [json.loads(b.decode("utf-8")) for b in client.process.stdin.lines]


@pytest.mark.asyncio
async def test_extension_dialogs_are_cancelled_on_arrival(rpc_cls):
    """a plugin awaiting ctx.ui.confirm must never block the turn: the
    answer pi needs is sent immediately, exactly as if the user pressed
    escape - and the record still reaches the module and the event queue"""
    seen = []
    client = _client(rpc_cls, on_ui_request=lambda req, auto: seen.append((req["method"], auto)))

    client._dispatch({
        "type": "extension_ui_request", "id": "u1", "method": "confirm",
        "title": "Allow tool call?", "message": "bash",
    })
    await asyncio.sleep(0)  # let the fire-and-forget drain run

    assert _written(client) == [{"type": "extension_ui_response", "id": "u1", "cancelled": True}]
    assert seen == [("confirm", True)]
    event = client._events.get_nowait()
    assert event["type"] == "extension_ui_request" and event["id"] == "u1"


@pytest.mark.asyncio
async def test_fire_and_forget_ui_methods_get_no_response(rpc_cls):
    """notify/setStatus expect no answer; answering them would be a protocol
    violation, but they must still reach the callback (uncancelled) so a
    plugin's notifications can be surfaced"""
    seen = []
    client = _client(rpc_cls, on_ui_request=lambda req, auto: seen.append((req["method"], auto)))

    client._dispatch({"type": "extension_ui_request", "id": "u2", "method": "notify", "message": "saved"})
    client._dispatch({"type": "extension_ui_request", "id": "u3", "method": "setStatus", "status": "indexing"})
    await asyncio.sleep(0)

    assert _written(client) == []
    assert seen == [("notify", False), ("setStatus", False)]
    assert client._events.qsize() == 2


@pytest.mark.asyncio
async def test_responses_and_plain_events_still_route(rpc_cls):
    """the UI interception must not disturb the two routes that were already
    there: correlated responses resolve commands, everything else queues"""
    client = _client(rpc_cls)
    fut = asyncio.get_running_loop().create_future()
    client._pending["7"] = fut
    client._dispatch({"type": "response", "id": "7", "success": True, "data": {"ok": 1}})
    client._dispatch({"type": "agent_settled"})

    assert client._pending == {}
    assert fut.done() and fut.result()["data"]["ok"] == 1
    assert client._events.get_nowait()["type"] == "agent_settled"


@pytest.mark.asyncio
async def test_ui_callback_errors_cannot_kill_the_reader(rpc_cls):
    """the callback runs inside the stdout reader task: a module-side bug
    must not take the whole connection down"""
    def boom(request, auto):
        raise RuntimeError("handler blew up")

    client = _client(rpc_cls, on_ui_request=boom)
    client._dispatch({"type": "extension_ui_request", "id": "u4", "method": "confirm"})
    await asyncio.sleep(0)

    assert _written(client)  # the answer went out before the callback ran


# ---------------------------------------------------------------------------
# the module-side surfacing and spawn wiring
# ---------------------------------------------------------------------------

def _backend(backend_cls, settings=None):
    """a PiBackend that only has what _prepare_provider_injection and the UI
    notes touch (no endpoint configured: pi stays on its own auth/providers,
    which is the 'existing pi user' configuration)"""
    backend = backend_cls.__new__(backend_cls)
    settings = settings or {}
    backend.module = types.SimpleNamespace(
        PROVIDER_ID="openlumara",
        provider_extension_path=lambda: "/nowhere/provider.js",
        tool_bridge_extension_path=lambda: "/nowhere/bridge.js",
        resolved_provider_settings=lambda: ("", "", ""),
        config=types.SimpleNamespace(get=lambda key, default=None: settings.get(key, default)),
        bridge=None,
        log=lambda *a: None,
    )
    backend.logs = []
    backend.manager = types.SimpleNamespace(
        log=lambda category, message: backend.logs.append((category, message))
    )
    return backend


@pytest.mark.asyncio
async def test_pi_agent_dir_reaches_pi_as_its_config_folder(backend_cls):
    """an existing pi install elsewhere: one setting moves pi's whole config
    (settings, auth, extensions, skills, packages) - pi reads the folder
    from the environment and the module forwards nothing else"""
    backend = _backend(backend_cls, {"pi_agent_dir": "~/alt-pi"})
    env, args, notes = await backend._prepare_provider_injection()

    assert env["PI_CODING_AGENT_DIR"] == os.path.abspath(os.path.expanduser("~/alt-pi"))
    assert notes and "alt-pi" in notes
    # empty setting = pi's own default (~/.pi/agent), untouched
    env2, _, _ = await _backend(backend_cls)._prepare_provider_injection()
    assert "PI_CODING_AGENT_DIR" not in env2


@pytest.mark.asyncio
async def test_project_trust_maps_to_pis_cli_flags(backend_cls):
    """RPC mode cannot prompt for trust and silently skips .pi/ resources;
    auto must add no flag (saved decisions keep working), approve and
    ignore must force the decision"""
    env, args, _ = await _backend(backend_cls, {"project_trust": "approve"})._prepare_provider_injection()
    assert "--approve" in args and "--no-approve" not in args

    _, args, _ = await _backend(backend_cls, {"project_trust": "ignore"})._prepare_provider_injection()
    assert "--no-approve" in args

    _, args, _ = await _backend(backend_cls, {"project_trust": "auto"})._prepare_provider_injection()
    assert "--approve" not in args and "--no-approve" not in args

    _, args, _ = await _backend(backend_cls)._prepare_provider_injection()  # default
    assert "--approve" not in args and "--no-approve" not in args


def test_declined_dialog_is_explained_in_the_log(backend_cls):
    """a permission gate that we decline is otherwise invisible: the plugin
    just does nothing. the log line is the only trace, so it must name the
    dialog and say what happened"""
    backend = _backend(backend_cls)
    backend._on_extension_ui_request(
        {"type": "extension_ui_request", "id": "u5", "method": "confirm", "title": "Allow tool call?"},
        True,
    )
    backend._on_extension_ui_request(
        {"type": "extension_ui_request", "id": "u6", "method": "notify", "message": "done", "notifyType": "warning"},
        False,
    )
    backend._on_extension_ui_request(
        {"type": "extension_ui_request", "id": "u7", "method": "setStatus"}, False,
    )

    logged = " | ".join(m for _, m in backend.logs)
    assert "Allow tool call?" in logged and "cancelled" in logged
    assert "done" in logged and "warning" in logged
    assert "setStatus" not in logged  # noise: pi degrades these itself


# ---------------------------------------------------------------------------
# the settings themselves
# ---------------------------------------------------------------------------

def test_the_two_ecosystem_settings_are_declared(module_cls):
    for name in ("pi_agent_dir", "project_trust"):
        entry = module_cls.settings[name]
        assert entry["description"], f"{name} needs a settings-menu description"
        assert "restart" in entry["description"].lower(), f"{name} takes effect at spawn - say so"
    assert module_cls.settings["pi_agent_dir"]["default"] == ""
    assert module_cls.settings["project_trust"]["default"] == "auto"


def test_both_settings_trigger_a_restart_on_change():
    """the settings watchdog only restarts pi when the spawn fingerprint
    moved - both of these only take effect at spawn, so they must be in it"""
    source = (PI_DIR / "module.py").read_text(encoding="utf-8")
    fingerprint = source.split("def spawn_fingerprint", 1)[1].split("async def", 1)[0]
    assert 'config.get("pi_agent_dir")' in fingerprint
    assert 'config.get("project_trust")' in fingerprint


# ---------------------------------------------------------------------------
# fixtures / module loading
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rpc_cls():
    return load_pi_module("pi_rpc")


@pytest.fixture(scope="module")
def backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture(scope="module")
def module_cls():
    return load_pi_module("module").Pi
