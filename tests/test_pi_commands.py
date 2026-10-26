# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
the /pi command is the module's only control surface - it ships no webui panel.

The module used to ship a sidebar panel with its own websocket broadcasts and
/api/ext/pi/... routes. Everything it offered is reachable without it, and
configuration is meant to come from OpenLumara's settings menu alone, so these
tests pin the removal down:

- the module ships no webui/ folder, declares no enable_webui_extension
  setting (core adds that toggle itself for modules that DO ship webui assets,
  so a leftover declaration would be a dead switch in the settings menu) and
  registers no webui extension route,
- every action the panel had a button for is a /pi subcommand, including
  pi's follow-up queue, which only the panel could reach before,
- what pi refused to register (a name shadowing one of pi's built-in tools)
  still reaches the user - through the log now instead of the panel feed,
- the backend no longer fans pi events out to a panel: the native tool cards
  are the feed (see test_pi_tool_stream.py for that path).
"""

import pathlib

import pytest

from test_pi_tool_stream import PI_DIR, load_pi_module


class ControlBackend:
    """only the PiBackend surface the /pi steer / follow-up path touches"""

    def __init__(self):
        self.steer_calls = [] # (text, mode)

    async def pi_steer(self, text, mode="steer"):
        self.steer_calls.append((text, mode))


def command_shell():
    """a Pi instance with just what pi_command needs (same shell trick as
    test_pi_chat_sync.py uses for the context patches)"""
    shell = load_pi_module("module").Pi.__new__(load_pi_module("module").Pi)
    shell.binary = "/usr/bin/pi"
    shell.backend = ControlBackend()
    shell.logs = []
    shell.log = lambda category, message: shell.logs.append((category, message))
    return shell


# ------------------------- the panel is gone -------------------------

def test_module_ships_no_webui_extension():
    module_cls = load_pi_module("module").Pi

    assert not (PI_DIR / "webui").exists(), \
        "the module must not ship webui assets: no sidebar panel, no panel assets"
    assert not (PI_DIR / "webui").is_dir()

    # core auto-inserts this setting for modules that ship a webui/ folder;
    # declaring it ourselves would leave a toggle that does nothing
    assert "enable_webui_extension" not in module_cls.settings

    # no method may still be marked as an /api/ext/pi/... route
    routes = [name for name in vars(module_cls)
              if getattr(getattr(module_cls, name, None), "_ui_route", None)]
    assert routes == [], f"leftover webui extension routes: {routes}"

    # and nothing left to push panel updates to
    assert not hasattr(module_cls, "broadcast_event")
    assert not hasattr(module_cls, "broadcast_state")
    assert not hasattr(load_pi_module("backend").PiBackend, "_push_event")


def test_no_module_file_mentions_the_removed_panel():
    for path in sorted(PI_DIR.glob("*.py")):
        text = path.read_text()
        assert "pi_event" not in text, f"{path.name} still speaks the panel protocol"
        assert "broadcast_state" not in text and "_push_event" not in text, path.name


# ------------------------- the settings menu is the only config -------------------------

def test_every_setting_is_plain_configuration():
    """the settings menu configures pi; it must not hold leftover UI switches"""
    settings = load_pi_module("module").Pi.settings

    assert settings, "the module is configured through the settings menu"
    for name, spec in settings.items():
        lowered = name.lower()
        assert "webui" not in lowered and "panel" not in lowered and "sidebar" not in lowered, \
            f"{name} configured the removed panel"
        # descriptions are what the settings menu shows: none may point at a panel
        assert "panel" not in str(spec.get("description", "")).lower(), name


# ------------------------- controls the panel had, now /pi -------------------------

async def test_steering_the_running_turn_still_works():
    shell = command_shell()
    reply = await shell.pi_command(["steer", "check", "the", "tests", "first"])

    assert shell.backend.steer_calls == [("check the tests first", "steer")]
    assert "steered" in reply


async def test_follow_up_subcommand_replaces_the_panel_input():
    """pi queues follow-ups separately from steering; the panel was the only way
    to reach that queue, so /pi owes a subcommand for it"""
    shell = command_shell()
    reply = await shell.pi_command(["follow_up", "then", "update", "the", "README"])

    assert shell.backend.steer_calls == [("then update the README", "follow_up")]
    assert "follow-up" in reply

    # alias spelling, same mode
    shell.backend.steer_calls.clear()
    await shell.pi_command(["followup", "next"])
    assert shell.backend.steer_calls == [("next", "follow_up")]


async def test_follow_up_without_text_asks_instead_of_calling_pi():
    shell = command_shell()
    reply = await shell.pi_command(["follow_up"])

    assert shell.backend.steer_calls == []
    assert "usage: /pi follow_up" in reply


def test_panel_buttons_all_have_a_subcommand():
    """abort / compact / new session / restart / steer all had buttons; the help
    table is what a user discovers, so it has to list them"""
    help_table = load_pi_module("module").Pi.pi_command._command_description

    for sub in ("abort", "compact", "new", "restart", "status", "models",
                "model <name>", "thinking <level>", "steer <text>", "follow_up <text>"):
        assert sub in help_table, f"/pi {sub} is not offered in the help"


# ------------------------- refused tools are still explained -------------------------

async def test_tools_pi_refused_are_named_in_the_log():
    """pi drops a bridged tool whose name shadows one of its built-ins; with no
    panel feed the log is what keeps that explainable"""
    shell = command_shell()

    await shell._on_tool_registry({
        "registered": [{"name": "memory_create"}, {"name": "calendar_add_event"}],
        "skipped": [{"name": "read"}],
    })

    assert len(shell.logs) == 1
    category, message = shell.logs[0]
    assert category == "pi"
    assert "2 OpenLumara tool" in message
    assert "read" in message and "NOT usable by pi" in message


async def test_clean_registration_logs_quietly_and_honestly():
    shell = command_shell()
    await shell._on_tool_registry({"registered": [{"name": "memory_create"}], "skipped": []})

    message = shell.logs[0][1]
    assert "1 OpenLumara tool" in message
    assert "NOT usable" not in message
