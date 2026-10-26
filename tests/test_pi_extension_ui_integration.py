# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
integration: a plugin's ctx.ui dialog against a real pi process.

loads an extension that uses the documented permission-gate pattern - a
tool_call handler that awaits ctx.ui.confirm with no timeout before a write
runs - and prompts pi to call write. Without a client that answers the
extension UI subprotocol, pi waits for that confirm forever: the turn never
settles (this is what any approval-gating plugin would do to OpenLumara).
With the module's rpc client the dialog is cancelled the moment it appears,
the gate blocks the call, and the turn ends normally.

skipped when the pi binary or a usable model endpoint is unavailable.
"""

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import sys

import pytest

PI_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = PI_DIR.parent.parent

pytestmark = pytest.mark.integration

PI_BIN = shutil.which("pi") or os.path.expanduser("~/.pi/agent/bin/pi")
PROVIDER_CONFIG = REPO_ROOT / "data" / "pi_provider.json"

# the gate pattern from pi's own extensions docs (docs/extensions.md):
# confirm every write through ctx.ui, with no timeout - only a client that
# answers extension_ui_request keeps this from blocking the turn forever
GATE_EXTENSION = """
export default function (pi) {
	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName === "write") {
			const ok = await ctx.ui.confirm("Allow write?", String(event.toolName));
			if (!ok) return { block: true, reason: "write was not approved" };
		}
	});
}
"""


def load_pi_module(name):
    """load user_modules/pi-backend/<name>.py under a synthetic
    user_modules.pi package (see the other test files)"""
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
def rpc_cls():
    return load_pi_module("pi_rpc")


async def test_plugin_confirm_does_not_hang_a_real_pi_turn(rpc_cls, tmp_path):
    if not os.access(PI_BIN, os.X_OK):
        pytest.skip("pi binary not available")

    provider_path = str(tmp_path / "pi_provider.json")
    if PROVIDER_CONFIG.is_file():
        provider_path = str(PROVIDER_CONFIG)
    else:  # fresh checkout: inject the test endpoint ourselves
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

    extension_path = tmp_path / "gate.js"
    extension_path.write_text(GATE_EXTENSION, encoding="utf-8")

    seen = []
    client = rpc_cls.PiRPCClient(
        PI_BIN,
        cwd=str(tmp_path / "work"),
        extra_args=[
            "--extension", str(PI_DIR / "pi_provider_extension.js"),
            "--extension", str(extension_path),
            "--offline",
            "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-mcp",
            "--session-dir", str(tmp_path / "sessions"),
            "--provider", "openlumara", "--model", model_id,
        ],
        env={
            "PI_OPENLUMARA_CONFIG": provider_path,
            "PI_OPENLUMARA_API_KEY": os.environ.get("OPENLUMARA_TEST_API_KEY", "not-needed"),
        },
        on_ui_request=lambda request, auto: seen.append((request.get("method"), auto)),
    )

    (tmp_path / "work").mkdir()

    try:
        await client.start()
        # prompt() only returns when the turn has settled - if the cancelled
        # dialog never reached pi, this is where the hang would surface
        try:
            await client.prompt(
                "Use the write tool to create the file note.txt in the current "
                "directory containing the single word hi. If the write is not "
                "allowed, just say so. Then answer with the single word done.",
                timeout=120.0,
            )
        except rpc_cls.PiRPCError as e:
            pytest.skip(f"pi rejected the prompt (endpoint unavailable?): {e}")

        settled = False
        try:
            while True:
                event = await client.next_event(timeout=15.0)
                if event.get("type") == "agent_settled":
                    settled = True
                    break
                if event.get("type") == "__dead__":
                    break
        except asyncio.TimeoutError:
            pass
    finally:
        await client.stop()

    assert [m for m, _ in seen] and seen[0][0] == "confirm", \
        "pi never routed the plugin's ctx.ui.confirm to the client"
    assert seen[0][1] is True, "the confirm must have been auto-cancelled"
    assert settled, "the turn never settled: the dialog answer did not unblock pi"
