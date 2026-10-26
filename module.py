# pi module for OpenLumara — connects the OpenLumara UI to a pi coding agent instance
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Turns OpenLumara into a UI for the pi coding agent (npm: @earendil-works/pi-coding-agent).

While this module is enabled, OpenLumara does not run its own AI agent backend:
manager.API is swapped for a pi-backed client that drives a `pi --mode rpc`
subprocess through pi's SDK RPC protocol. Every OpenLumara UI element keeps
working, but now controls / displays pi:

  - chat send + streaming chain  -> pi prompt + pi event stream
  - stop button                  -> pi abort
  - model switcher + /model      -> pi model registry (set_model RPC)
  - context pill / /status       -> pi's own context-window and cost stats
  - /compress                    -> pi's session compaction
  - scheduler jobs               -> prompts for pi
  - chat switching               -> pi session switch (each OpenLumara chat is
    mapped to its own pi session file, persisted in data/pi_sessions.json)

See README.md for the full mapping, settings reference and limitations.
"""

import asyncio
import os
import shutil

import core

from .backend import PiBackend
from .pi_rpc import PiRPCError
from .tool_bridge import ToolBridge


class Pi(core.module.Module):
    """Replaces OpenLumara's AI backend with a pi coding agent: chat, streaming, stop, model switching and context stats in every channel drive a `pi --mode rpc` session through pi's SDK RPC protocol instead of the OpenAI-API backend. Each OpenLumara chat maps to its own pi session. Requires pi installed (npm install -g @earendil-works/pi-coding-agent); until the binary is found the module stays passive and OpenLumara keeps using its own backend."""

    category = "system"

    # pi reads, edits and executes on the host: be honest about it in the UI
    unsafe = True

    settings = {
        "pi_path": {
            "default": "pi",
            "description": "Path to the pi binary (the pi coding agent CLI). Absolute paths, '~' paths and relative paths are used directly; a bare name like 'pi' is looked up on PATH, then common locations like ~/.pi/agent/bin/pi and ~/.local/bin/pi. A running pi is restarted automatically to apply this"
        },
        "working_dir": {
            "default": ".",
            "description": "The project folder pi runs in (where pi reads AGENTS.md / CLAUDE.md, edits files and runs commands). Relative paths resolve from OpenLumara's folder; '~' is expanded. A running pi is restarted automatically to apply this"
        },
        "pi_agent_dir": {
            "default": "",
            "description": "Override pi's own configuration folder (empty = pi's standard ~/.pi/agent). Everything in that folder loads untouched: settings.json, auth.json and models.json (so pi keeps its own provider logins), the extensions/, skills/ and prompts/ folders, themes, and pi package declarations - an existing pi setup carries over to OpenLumara as-is. Point it at another pi profile to run OpenLumara on that instead. A running pi is restarted automatically to apply this"
        },
        "project_trust": {
            "default": "auto",
            "description": "pi's project trust for the working directory, which gates that folder's .pi/ resources (project settings, project extensions, project packages): 'auto' lets pi decide (outside a terminal it only loads them when a decision was saved before, e.g. by running pi interactively with /trust in that folder), 'approve' trusts the folder on every start, 'ignore' always skips it. A running pi is restarted automatically to apply this"
        },
        "use_openlumara_settings": {
            "type": "boolean",
            "default": True,
            "description": "Configure pi with OpenLumara's own API settings (endpoint url and key) and model settings (model name): pi gets an 'openlumara' provider pointing at the same OpenAI-compatible endpoint and starts on the same model. Turn off to configure pi manually with the (then visible) api_url / api_key / model settings below, or leave those empty to keep pi on its own providers and auth. A running pi is restarted automatically to apply this (once its current turn finishes)"
        },
        "api_url": {
            "default": "",
            "depends": {"use_openlumara_settings": False},
            "description": "The API base URL pi is configured with (any OpenAI-compatible endpoint, usually ending in /v1). Only used when 'use openlumara settings' is off. A running pi is restarted automatically to apply this"
        },
        "api_key": {
            "default": "",
            "depends": {"use_openlumara_settings": False},
            "description": "The API key pi authenticates against that endpoint with. Leave empty for auth-free servers (pi then gets a harmless dummy key they ignore). Only used when 'use openlumara settings' is off. A running pi is restarted automatically to apply this"
        },
        "model": {
            "default": "",
            "depends": {"use_openlumara_settings": False},
            "description": "The model pi starts with: a bare model id is used on the endpoint above, a 'provider/id' pattern selects one of pi's own providers. Only used when 'use openlumara settings' is off. A running pi is restarted automatically to apply this"
        },
        "extra_args": {
            "type": "list",
            "default": [],
            "description": "Extra command-line arguments passed to `pi --mode rpc`, one per list entry (for example '--offline' or '--no-session'). A running pi is restarted automatically to apply this"
        },
        "auto_start": {
            "type": "boolean",
            "default": True,
            "description": "Start the pi process as soon as the module loads. When off, pi starts on the first message (slightly slower first message, nothing runs while idle)"
        },
        "session_per_chat": {
            "type": "boolean",
            "default": True,
            "description": "Give every OpenLumara chat its own pi session (switching chats switches pi sessions; pi's own compaction and history apply per chat). Turn off to share one pi session across all chats"
        },
        "show_tool_activity": {
            "type": "boolean",
            "default": False,
            "description": "Additionally show pi's tool executions as plain activity lines inside the processing chain. Tool calls always render as OpenLumara's native tool cards (streamed arguments, live result, history) exactly like the default AI backend, and the cards already show the tool's output; this setting only adds extra text lines on top"
        },
        "tool_output_chars": {
            "type": "number",
            "default": 400,
            "description": "Maximum characters of tool output shown in a chain activity line (0 hides output and only shows that the tool finished)"
        },
        "forward_images": {
            "type": "boolean",
            "default": True,
            "description": "Forward images you upload or paste in chat to pi as image attachments (only useful when the active pi model accepts images)"
        },
        "expose_tools": {
            "type": "boolean",
            "default": True,
            "description": "Expose OpenLumara's own tools to pi: the pi agent sees them in its tool list and every call to one is intercepted and executed by OpenLumara (the result streams back into pi's turn and renders as a native tool card). pi's built-in tools (read, bash, edit, write, ...) always run natively inside pi regardless of this setting. Turning this off leaves pi with only its own tools; a running pi is restarted automatically to apply this"
        }
    }

    # pi is an external Node CLI, nothing to pip-install
    dependencies = []

    # ------------------------- lifecycle -------------------------

    async def on_ready(self):
        self.binary = self._resolve_binary()
        self.backend = None
        self._original_api = None
        self._patches = []
        self._settings_task = None
        self._spawn_state = None
        self._auto_compaction_applied = None
        self.bridge = None

        self.session_map = core.storage.StorageDict("pi_sessions", type="json")

        self.backend = PiBackend(self.manager, self)

        if self.binary is None:
            self.log("pi", "pi binary not found: install it with 'npm install -g @earendil-works/pi-coding-agent' or set the pi_path setting. The module stays passive - OpenLumara keeps using its own AI backend.")
            return

        # the swap: channels reach the AI through manager.API, so replacing it
        # redirects every channel (webui, cli, telegram, scheduler, commands)
        # at pi without touching the core
        self._original_api = self.manager.API
        self.manager.API = self.backend
        self._patch_context()
        self._patch_toolcalls()

        # the loopback server that runs OpenLumara tools for pi; must be up
        # before the first pi spawn so the bridge extension can point at it
        self.bridge = None
        await self.ensure_bridge()

        self.log("pi", f"OpenLumara's AI backend now points at pi ({self.binary}, cwd {self.resolved_cwd()})"
                 + (f", OpenLumara tools bridged to pi via {self.bridge.url}" if self.bridge else ""))

        if self.config.get("auto_start", default=True):
            result = await self.backend.connect(silent=True)
            if result is not True:
                self.log("pi", f"could not start pi yet: {result}")

        # pi's own "compact when the context is nearly full" follows
        # OpenLumara's automatically_compress_context switch from the start
        await self.backend.apply_auto_compaction()

        # saved settings (OpenLumara's or this module's) are pushed to pi
        # automatically instead of waiting for a manual /pi restart
        self._settings_task = asyncio.create_task(self._settings_watchdog())

    async def on_shutdown(self):
        if getattr(self, "_settings_task", None) is not None:
            self._settings_task.cancel()
            self._settings_task = None

        # restore everything we patched, in reverse order
        for cls, name, original in reversed(getattr(self, "_patches", [])):
            try:
                setattr(cls, name, original)
            except Exception:
                pass
        self._patches = []

        backend = getattr(self, "backend", None)
        if backend is not None and getattr(self.manager, "API", None) is backend:
            self.manager.API = self._original_api
        self._original_api = None

        bridge = getattr(self, "bridge", None)
        if bridge is not None:
            try:
                await bridge.stop()
            except Exception as e:
                self.log("pi", f"error while stopping the tool bridge: {e}")
            self.bridge = None

        if backend is not None:
            try:
                await backend.shutdown()
            except Exception as e:
                self.log("pi", f"error while stopping pi: {e}")

    def _resolve_binary(self):
        """find the pi executable from the pi_path setting (explicit path wins, bare names get PATH + common locations)"""
        configured = str(self.config.get("pi_path") or "pi").strip() or "pi"
        expanded = os.path.expanduser(configured)

        # An explicit path wins, even when it is relative or does not contain a separator.
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

        # If the user configured a path-like value, do not silently fall back to
        # a different pi binary; report the configured path instead.
        if configured != "pi" and (os.sep in expanded or (os.altsep and os.altsep in expanded)):
            self.log("pi", f"configured pi_path '{configured}' is not an executable file")
            return None

        found = shutil.which(expanded)
        if found:
            return found

        if expanded == "pi":
            for candidate in ("~/.pi/agent/bin/pi", "~/.pi/bin/pi", "~/.local/bin/pi", "~/.npm-global/bin/pi", "/usr/local/bin/pi", "/usr/bin/pi"):
                path = os.path.expanduser(candidate)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path

        if configured != "pi":
            self.log("pi", f"configured pi_path '{configured}' was not found")

        return None

    def resolved_cwd(self) -> str:
        """the working directory pi runs in, as an absolute path"""
        raw = str(self.config.get("working_dir") or ".").strip() or "."
        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            raw = core.get_path(raw, sandbox=False)
        raw = os.path.normpath(raw)

        # If expanduser could not resolve a home shortcut, refuse the path rather
        # than creating a literal "~" directory under OpenLumara's folder.
        if "~" in raw.split(os.sep):
            self.log("pi", f"working_dir '{raw}' still contains a literal ~ segment, falling back to OpenLumara's folder")
            return core.get_path()

        if os.path.isdir(raw):
            return raw

        self.log("pi", f"working_dir '{raw}' is not a folder, falling back to OpenLumara's folder")
        return core.get_path()

    # ------------------------- provider injection -------------------------

    PROVIDER_ID = "openlumara" # provider id pi sees the injected endpoint as

    def resolved_provider_settings(self):
        """resolve which API settings pi should run with: while
        use_openlumara_settings is on (the default) OpenLumara's own API and
        model settings decide and the manual settings are not used (the UI
        hides them); with the switch off the manual api_url / api_key / model
        settings decide. Returns (base_url, api_key, model); an empty
        base_url disables the injection entirely"""
        if self.config.get("use_openlumara_settings", default=True):
            base_url = str(core.config.get("api", "url") or "").strip()
            api_key = str(core.config.get("api", "key") or "").strip()
            model = str(core.config.get("model", "name") or "").strip()
        else:
            base_url = str(self.config.get("api_url") or "").strip()
            api_key = str(self.config.get("api_key") or "").strip()
            model = str(self.config.get("model") or "").strip()

        # OpenLumara's stock placeholder endpoint is not a real server
        if "API_URL_HERE" in base_url:
            base_url = ""

        return base_url.rstrip("/"), api_key, model

    def provider_extension_path(self):
        """absolute path of the pi extension that registers the injected provider"""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_provider_extension.js")

    def tool_bridge_extension_path(self):
        """absolute path of the pi extension that exposes OpenLumara's tools to pi"""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_tool_bridge_extension.js")

    def extra_args(self):
        extra = self.config.get("extra_args", default=[]) or []
        return [str(a).strip() for a in extra if str(a).strip()]

    def tool_output_limit(self) -> int:
        try:
            limit = int(self.config.get("tool_output_chars", default=400))
        except (TypeError, ValueError):
            return 400
        return max(0, limit)

    # ------------------------- settings watchdog -------------------------

    SETTINGS_POLL_SECONDS = 5

    def spawn_fingerprint(self):
        """everything that only takes effect when the pi process is spawned:
        this module's settings plus the OpenLumara settings the injection
        reads. A change to the fingerprint means the running pi (if any) is
        running on stale configuration"""
        try:
            # pick up config.yml edits made outside this process (the config
            # manager reloads on its own gets too, this keeps the module's
            # own scoped settings view and this snapshot in step)
            if core.config.config is not None:
                core.config.config.load()
        except Exception:
            pass

        return (
            str(self.config.get("pi_path") or ""),
            str(self.config.get("working_dir") or ""),
            str(self.config.get("pi_agent_dir") or ""),
            str(self.config.get("project_trust") or "auto"),
            tuple(str(a).strip() for a in (self.config.get("extra_args") or [])),
            bool(self.config.get("use_openlumara_settings", default=True)),
            str(self.config.get("api_url") or ""),
            str(self.config.get("api_key") or ""),
            str(self.config.get("model") or ""),
            str(core.config.get("api", "url") or ""),
            str(core.config.get("api", "key") or ""),
            str(core.config.get("api", "max_context", 128000)),
            str(core.config.get("api", "max_output_tokens", 8192)),
            str(core.config.get("model", "name") or ""),
            str(core.config.get("model", "enable_thinking", default=True)),
            bool(self.config.get("expose_tools", default=True)),
        )

    async def _on_tool_registry(self, payload: dict):
        """log what pi took and what it refused: a tool pi shadowed with one of
        its own built-ins is otherwise simply missing, with nothing to say why
        (/pi status repeats this on request)"""
        registered = payload.get("registered") or []
        skipped = payload.get("skipped") or []
        line = f"pi registered {len(registered)} OpenLumara tool(s)"
        if skipped:
            line += " - NOT usable by pi (the name shadows one of pi's built-in tools): " + ", ".join(
                str(t.get("name") if isinstance(t, dict) else t) for t in skipped)
        self.log("pi", line)

    async def ensure_bridge(self):
        """start / stop the tool bridge to match the expose_tools setting
        (the bridge lives as long as the module, pi respawns around it)"""
        want = bool(self.config.get("expose_tools", default=True))

        if want and self.bridge is None and self.binary is not None:
            bridge = ToolBridge(self.manager, log=self.log)
            # pi reports back which tools it took; anything it refused (a name
            # that shadows one of pi's built-ins) must reach the user, not just
            # the log, or a missing tool is unexplainable
            bridge.on_registry = self._on_tool_registry
            result = await bridge.start()
            if result is True:
                self.bridge = bridge
                self.log("pi", f"tool bridge listening on {bridge.url}")
            else:
                self.log("pi", f"tool bridge is not running: {result}")
                return False
        elif not want and self.bridge is not None:
            try:
                await self.bridge.stop()
            except Exception as e:
                self.log("pi", f"error while stopping the tool bridge: {e}")
            self.bridge = None
            self.log("pi", "tool bridge stopped (expose_tools is off)")
        return True

    async def check_settings_change(self):
        """one watchdog pass: restart pi when saved settings changed since it
        was spawned. Returns True when a restart was attempted"""
        state = self.spawn_fingerprint()

        if state == self._spawn_state:
            return False

        # the bridge itself must follow the setting before pi respawns with it
        await self.ensure_bridge()

        backend = getattr(self, "backend", None)
        if backend is None:
            return False

        # a live pi must be respawned to pick the settings up; a stopped one
        # will read them on its next spawn anyway
        if backend.pi is None or not backend.pi.alive:
            self._spawn_state = state
            return False

        # never cut into a running turn (also not one that is between
        # connect and stream start, which holds the turn lock): retry once
        # pi is idle
        if backend.is_streaming or backend._turn_lock.locked():
            return False

        self._spawn_state = state
        self.log("pi", "settings changed: restarting pi with the updated configuration")

        # pi_path may have changed too
        self.binary = self._resolve_binary()
        if self.binary is None:
            self.log("pi", "the pi binary is no longer available: fix the pi_path setting")
            return True

        result = await backend.restart_process()
        if result is not True:
            self.log("pi", f"auto-restart could not start pi again: {result}")
        else:
            await backend.apply_auto_compaction()
        return True

    async def sync_auto_compaction(self):
        """one watchdog pass: OpenLumara's automatically_compress_context is
        not part of the pi spawn fingerprint (it is model config, not module
        config), so mirror it into the running pi whenever it changes"""
        backend = getattr(self, "backend", None)
        if backend is None or backend.pi is None or not backend.pi.alive:
            return
        wanted = await backend.pi_auto_compact_enabled()
        if wanted == self._auto_compaction_applied:
            return
        try:
            await backend.pi.set_auto_compaction(wanted)
        except PiRPCError as e:
            self.log("pi", f"could not sync pi's auto-compaction setting: {e}")
            return
        self._auto_compaction_applied = wanted

    async def _settings_watchdog(self):
        self._spawn_state = self.spawn_fingerprint()
        while True:
            try:
                await asyncio.sleep(self.SETTINGS_POLL_SECONDS)
                await self.check_settings_change()
                await self.sync_auto_compaction()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log("pi", f"settings watchdog hiccup: {e}")

    # ------------------------- core patches -------------------------

    def _patch_context(self):
        """while pi is the backend, OpenLumara's own context management has
        nothing to measure: the built context is never sent anywhere (pi keeps
        its own session), so the compression trigger measures pi's real
        session fill and /compress plus the automatic threshold both route to
        pi's own session compaction - the same trigger points and the same
        user-facing behavior as the default backend, whose trigger fires from
        the very same call sites. originals are restored on shutdown /
        module reload."""
        context_cls = core.context.Context

        async def is_over_threshold(self_ctx):
            # same switch the default backend's is_over_threshold respects
            if not core.config.get("model", "automatically_compress_context"):
                return False
            backend = self_ctx.channel.manager.API
            if not isinstance(backend, PiBackend):
                return False

            threshold = core.config.get("model", "context_compression_threshold")
            if not threshold:
                return False

            # estimated_context_tokens is the setting the default backend's
            # threshold divides by; with pi in charge, pi reports its own
            # fill directly, so only the threshold carries over
            try:
                fill = await backend.pi_context_fill()
            except Exception:
                return False
            return bool(fill) and fill >= float(threshold)

        async def compress(self_ctx):
            """replaces Context.compress: compacts the pi session instead of an
            OpenAI-style summarization pass. like the default backend's
            automatic compression (fired from messages.add mid-conversation)
            this runs silently; only the explicit /compress path pushes"""
            backend = self_ctx.channel.manager.API
            if not isinstance(backend, PiBackend):
                return False
            if getattr(self_ctx, "compressing", False):
                return False # same recursion guard the original uses
            # an automatic trigger can fire while pi is mid-turn (or between
            # connect and stream start); compacting then would cut into the
            # running prompt, and pi compacts on its own near full anyway
            if backend.is_streaming or backend._turn_lock.locked():
                return False

            self_ctx.compressing = True
            try:
                result = await backend.pi_compact(notify=False)
            except PiRPCError as e:
                await self_ctx.channel.push(f"pi compaction failed: {e}")
                return False
            finally:
                self_ctx.compressing = False

            if not result or not result.get("summary"):
                return False

            # the original invalidates the cached API token measurement after
            # compressing - pi's number is always current, but the chat's own
            # cache must not keep claiming the pre-compaction fill
            if self_ctx.chat.current is not None:
                self_ctx.chat.data[self_ctx.chat.current]["token_usage"] = 0
                self_ctx.chat.data[self_ctx.chat.current]["token_usage_mark"] = 0
                self_ctx.using_api_token_data = False

            return {"role": "assistant", "content": result.get("summary")}

        original_get_size = context_cls.get_size
        original_get_total_tokens = context_cls.get_total_tokens

        async def get_total_tokens(self_ctx):
            """replaces Context.get_total_tokens: the pill's numerator. the
            default backend caches the API's reported prompt size; with pi in
            charge the equivalent measurement is pi's own session fill, which
            also covers what only pi knows about (its system prompt, its own
            tool calls and results) - the estimators here would describe a
            context that is never sent"""
            backend = self_ctx.channel.manager.API
            if isinstance(backend, PiBackend):
                try:
                    metrics = await backend.pi_context_metrics_for(self_ctx.channel)
                except Exception:
                    metrics = None
                if metrics:
                    return metrics["total_tokens"]
            return await original_get_total_tokens(self_ctx)

        async def get_size(self_ctx):
            """replaces Context.get_size: the same breakdown dict the webui
            context popup and /status render, measured from pi's session
            instead of openlumara's (inert) prompt assembly"""
            backend = self_ctx.channel.manager.API
            if isinstance(backend, PiBackend):
                try:
                    metrics = await backend.pi_context_metrics_for(self_ctx.channel)
                except Exception:
                    metrics = None
                if metrics:
                    return metrics
            return await original_get_size(self_ctx)

        self._patches.append((context_cls, "is_over_threshold", context_cls.is_over_threshold))
        self._patches.append((context_cls, "compress", context_cls.compress))
        self._patches.append((context_cls, "get_total_tokens", context_cls.get_total_tokens))
        self._patches.append((context_cls, "get_size", context_cls.get_size))
        context_cls.is_over_threshold = is_over_threshold
        context_cls.compress = compress
        context_cls.get_total_tokens = get_total_tokens
        context_cls.get_size = get_size

    def _patch_toolcalls(self):
        """while pi is the backend, OpenLumara's toolcall chain must not
        execute anything: pi runs its built-in tools (read/bash/edit/write)
        natively inside its own session, and OpenLumara tools that pi calls
        are already executed by the tool bridge when the call streams past -
        the backend streams the calls as display-only cards with matching
        responses already on the stream. letting ToolcallManager.process()
        handle them would run every pi tool call a second time as an
        OpenLumara tool (and fail outright on pi's own tools, which are not
        in OpenLumara's catalog). the original is restored on shutdown /
        module reload."""
        tc_cls = core.toolcalls.ToolcallManager
        original = tc_cls.process

        async def process(self_tc, assistant_message, push=False):
            # NOTE: PiBackend is a subclass of core.api.APIClient, so the
            # check below must test for the pi backend itself - matching on
            # "is an APIClient" would treat pi as the default backend and
            # make core execute pi's tool calls (its built-ins included) as
            # OpenLumara tools a second time.
            backend = self_tc.channel.manager.API
            if isinstance(backend, PiBackend):
                # agent-runtime backend: every call already ran inside pi
                # (pi's own tools natively, OpenLumara tools through the tool
                # bridge) and the matching responses stream with the events -
                # nothing to execute here. the channel still counts tool
                # tokens as a toolcall round, so its own single-message
                # persist stays skipped and the pi backend persists the
                # turn's messages (cards included) itself
                return
                yield  # keeps this a (necessarily empty) async generator
            async for token in original(self_tc, assistant_message, push=push):
                yield token

        self._patches.append((tc_cls, "process", original))
        tc_cls.process = process

    # ------------------------- module hooks -------------------------

    async def on_user_message(self, content: str):
        # never intercept: this hook only lets the pi backend recognise an
        # interactive turn (vs. a direct scheduler call) in its send() path
        backend = getattr(self, "backend", None)
        if backend is not None:
            backend.note_user_message(content)
        return None

    # ------------------------- user command -------------------------

    @core.module.command("pi", help={
        "": "shows pi backend status",
        "status": "shows pi backend status",
        "models": "lists pi's available models",
        "model <name>": "switches pi's model (provider/id or bare id)",
        "thinking <level>": "sets pi's thinking level (off, minimal, low, medium, high, xhigh, max)",
        "new": "starts a fresh pi session for this chat",
        "abort": "aborts pi's current run",
        "compact": "compacts pi's session context",
        "restart": "restarts the pi process",
        "steer <text>": "steers pi's currently running turn",
        "follow_up <text>": "queues text for pi to act on after its current turn",
    })
    async def pi_command(self, args: list):
        backend = getattr(self, "backend", None)
        if backend is None:
            return "pi module is not initialized"

        sub = (args[0].lower() if args else "status")

        if sub in ("status", ""):
            if self.binary is None:
                return "pi binary not found - install pi or fix the pi_path setting (the module is passive; OpenLumara still uses its own backend)"

            connected = await backend.attempt_connect()
            if connected is not True:
                return f"pi is not running: {connected}"

            snap = await backend._refresh_state() or {}
            status = backend.status_snapshot()
            ctx = status["context"]
            tokens = status["tokens"]

            lines = ["== pi backend =="]
            lines.append(f"Process: running (cwd {status['cwd']})")
            agent_dir = str(self.config.get("pi_agent_dir") or "").strip()
            lines.append(
                f"Config: {os.path.expanduser(agent_dir) if agent_dir else '~/.pi/agent (pi settings, auth, extensions, skills, packages)'}"
                f" · project trust: {str(self.config.get('project_trust') or 'auto')}"
            )
            base_url, _, model = self.resolved_provider_settings()
            if base_url:
                lines.append(f"API: {base_url} (default model: {model or 'first the endpoint offers'})")
            else:
                lines.append("API: none injected (pi uses its own providers and auth)")
            lines.append(f"Model: {status['model'] or 'unknown'} | thinking: {status['thinking_level'] or 'off'}")
            bridge = getattr(self, "bridge", None)
            if bridge is not None and bridge.url:
                from .tool_bridge import bridge_tool_definitions
                offered = len(bridge_tool_definitions(self.manager))
                line = f"Tools: {offered} OpenLumara tools bridged via {bridge.url}"
                if bridge.registered:
                    line += f" ({len(bridge.registered)} registered in pi)"
                if bridge.skipped:
                    line += (" - NOT available to pi: "
                             + ", ".join(t["name"] for t in bridge.skipped))
                lines.append(line)
            else:
                lines.append("Tools: none bridged (pi uses only its own tools)")
            lines.append(f"Session: {snap.get('sessionName') or '(unnamed)'} · {snap.get('sessionId') or '?'}")
            lines.append(f"Bound chat: {status['session']['bound_chat'] or 'none'}")
            if ctx.get("tokens") and ctx.get("window"):
                lines.append(f"pi context: {ctx['tokens']} / {ctx['window']} tokens ({ctx.get('percent')}%)")
            if tokens.get("total"):
                lines.append(f"Session tokens: {tokens['total']} (input {tokens.get('input')}, output {tokens.get('output')})")
            if status.get("cost") is not None:
                lines.append(f"Session cost: ${status['cost']}")
            lines.append(f"Streaming: {'yes' if status['busy'] else 'no'}")
            return "\n".join(lines)

        elif sub == "models":
            models = await backend.list_models()
            if not isinstance(models, list):
                return f"could not list models: {models}"
            if not models:
                return "pi reported no models (check pi's own provider setup / auth)"
            current = backend.get_model()
            return "pi models (* = active):\n" + "\n".join(
                f"{'*' if m == current else ' '} {m}" for m in models
            )

        elif sub == "model":
            if len(args) < 2:
                return "usage: /pi model <provider/id or id>"
            ok = await backend._apply_model(args[1])
            return f"pi model set to {backend.get_model()}" if ok else f"could not switch pi to model '{args[1]}'"

        elif sub == "thinking":
            if len(args) < 2:
                levels = ", ".join(await backend.pi_thinking_levels())
                return f"usage: /pi thinking <level> (available: {levels})"
            ok = await backend.pi_set_thinking_level(args[1].lower())
            return f"pi thinking level set to {args[1]}" if ok else "could not set pi's thinking level"

        elif sub == "new":
            try:
                await backend.pi_new_session()
            except PiRPCError as e:
                return f"could not start a new pi session: {e}"
            return "started a fresh pi session for this chat"

        elif sub == "abort":
            ok = await backend.cancel()
            return "aborted pi's current run" if ok else "pi is not running anything right now"

        elif sub == "compact":
            try:
                result = await backend.pi_compact()
            except PiRPCError as e:
                return f"pi compaction failed: {e}"
            if not result:
                return "pi compaction did not run (is pi connected?)"
            return f"pi session compacted: {result.get('tokensBefore')} -> ~{result.get('estimatedTokensAfter')} tokens"

        elif sub == "restart":
            if self.binary is None:
                return "pi binary not found - fix the pi_path setting first"
            result = await backend.restart_process()
            return "pi process restarted" if result is True else f"restarting pi failed: {result}"

        elif sub in ("steer", "follow_up", "followup"):
            if len(args) < 2:
                return f"usage: /pi {sub} <text to give pi>"
            mode = "follow_up" if sub.startswith("follow") else "steer"
            try:
                await backend.pi_steer(" ".join(args[1:]), mode=mode)
            except PiRPCError as e:
                return f"could not reach pi: {e}"
            return ("queued a follow-up: pi acts on it after its current turn"
                    if mode == "follow_up" else "steered pi")

        return "unknown /pi subcommand - try /pi for help"
