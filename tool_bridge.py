# pi module for OpenLumara — lets the pi agent execute OpenLumara's own tools
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
The tool bridge: OpenLumara tools, callable from inside pi.

pi owns the model loop, so a tool call pi makes for an OpenLumara tool has to
come back here to run. pi_tool_bridge_extension.js registers one pi tool per
OpenLumara tool (name, description and parameter schema served by this
server); the extension's execute() posts the call to this loopback HTTP
server, the server runs the tool through OpenLumara's own tool catalog (the
same catalog entry, disabled list, loaded set, timeout and result rendering
core/toolcalls.py applies to its own backend's calls) and hands the result
back to pi, which feeds it to its model.

Because the call is a normal pi tool from pi's point of view, its arguments,
finalized card and result stream to OpenLumara through pi's regular tool
events - the UI shows bridged tools as the same native tool cards as pi's
built-in ones, and pi's built-in tools (read/bash/edit/write) keep running
natively inside pi, untouched.

Security: the server binds to 127.0.0.1 on an ephemeral port; every request
must carry the per-spawn bearer token (given to the pi child as
PI_OPENLUMARA_BRIDGE_TOKEN). The token never enters any file.
"""

import asyncio
import hmac
import json
import secrets

from aiohttp import web

import core

# the dynamic-tool-loading meta tools are part of the model's tool array while
# dynamic loading is on; they are bridged like any other tool (see below), so
# pi reaches the whole catalog through the very same mechanism the default
# backend's model uses rather than through a module-specific shortcut
META_TOOL_NAMES = ("tools_load", "tools_call")


def bridge_tool_definitions(manager):
    """the active OpenAI tool array OpenLumara sends its own model
    (manager.tools), reduced to what pi's registerTool needs: name,
    description and parameter schema.

    verbatim, on purpose: with dynamic tool loading off that array is every
    loadable tool; with it on it is the preloaded set plus the tools_load /
    tools_call meta tools, and tools_call is what reaches the rest of the
    catalog. filtering either one out would hand pi strictly fewer tools than
    the default backend's model is offered."""
    definitions = []
    for tool in getattr(manager, "tools", []) or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        definitions.append({
            "name": name,
            "description": function.get("description") or f"OpenLumara tool {name}",
            "parameters": parameters,
        })
    return definitions


class ToolBridge:
    """loopback HTTP server that executes one OpenLumara tool per request"""

    def __init__(self, manager, log=None):
        self.manager = manager
        self._log = log or (lambda category, message: None)

        self.token = secrets.token_urlsafe(32)
        self.url = None
        self._runner = None
        self._site = None
        # OpenLumara's own chain runs one tool at a time; keep that property
        # for calls coming from pi so tools never overlap
        self._exec_lock = asyncio.Lock()

        # what pi actually managed to register, reported back by the extension.
        # a tool pi refuses (a name that shadows one of pi's built-ins) is
        # otherwise invisible: the user would just find the tool missing from
        # the agent with no trace why
        self.registered = []
        self.skipped = []
        self.on_registry = None # async callback(payload), for the log / /pi status

    # ------------------------- lifecycle -------------------------

    async def start(self):
        """bind to 127.0.0.1 on an ephemeral port. returns True or an error string"""
        if self._site is not None:
            return True

        app = web.Application()
        app.router.add_get("/tools", self._handle_tools)
        app.router.add_post("/execute", self._handle_execute)
        app.router.add_post("/registered", self._handle_registered)

        try:
            self._runner = web.AppRunner(app, handle_signals=False)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await self._site.start()
        except OSError as e:
            self._site = None
            self._runner = None
            return f"could not bind the tool bridge: {e}"

        addresses = getattr(self._runner, "addresses", None) or []
        port = addresses[0][1] if addresses else None
        if not port:
            await self.stop()
            return "could not determine the tool bridge port"

        self.url = f"http://127.0.0.1:{port}"
        return True

    async def stop(self):
        if self._site is not None or self._runner is not None:
            try:
                if self._runner is not None:
                    await self._runner.cleanup()
            except Exception:
                pass
        self._site = None
        self._runner = None
        self.url = None

    # ------------------------- request handling -------------------------

    def _authorized(self, request) -> bool:
        header = request.headers.get("Authorization") or ""
        scheme, _, provided = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(provided.strip(), self.token)

    def _unauthorized(self):
        return web.json_response({"status": "error", "content": "invalid bridge token"}, status=401)

    async def _handle_tools(self, request):
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response({"tools": bridge_tool_definitions(self.manager)})

    async def _handle_registered(self, request):
        """the extension reports which tools pi took and which it refused, so a
        tool that never reached the agent is at least named in the log and in
        /pi status instead of being silently absent"""
        if not self._authorized(request):
            return self._unauthorized()
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False}, status=400)

        registered = [str(n) for n in (body.get("registered") or []) if isinstance(n, str)]
        skipped = []
        for entry in body.get("skipped") or []:
            if isinstance(entry, dict) and entry.get("name"):
                skipped.append({"name": str(entry["name"]), "reason": str(entry.get("reason") or "")})

        self.registered = registered
        self.skipped = skipped

        for tool in skipped:
            self._log("pi", f"pi could not register the OpenLumara tool {tool['name']} "
                            f"({tool['reason'] or 'rejected by pi'}) - it is unavailable to the agent")
        if self.on_registry is not None:
            try:
                await self.on_registry({"registered": registered, "skipped": skipped})
            except Exception:
                pass
        return web.json_response({"ok": True})

    async def _handle_execute(self, request):
        if not self._authorized(request):
            return self._unauthorized()

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response(
                {"status": "error", "content": "the tool bridge expects a json body"}, status=400)

        if not isinstance(body, dict):
            return web.json_response(
                {"status": "error", "content": "the tool bridge expects a json object"}, status=400)

        name = body.get("tool")
        arguments = body.get("arguments")
        if not isinstance(name, str) or not name:
            return web.json_response(
                {"status": "error", "content": "the tool bridge needs a 'tool' name"}, status=400)
        if not isinstance(arguments, dict):
            arguments = {}

        result = await self.execute_tool(name, arguments)
        return web.json_response(result)

    # ------------------------- execution -------------------------

    async def execute_tool(self, name: str, arguments: dict) -> dict:
        """run one OpenLumara tool the way the default backend's chain would.
        always returns {"status": "success"|"error", "content": str}"""
        channel = getattr(self.manager, "channel", None)
        tool_loader = getattr(channel, "tool_loader", None) if channel is not None else None
        catalog = getattr(tool_loader, "catalog", {}) if tool_loader is not None else {}

        # the dynamic-tool-loading meta tools run through openlumara's own
        # loader, exactly as core/toolcalls.py runs them for its own model, so
        # pi can load schemas (tools_load) and call any cataloged tool
        # (tools_call) instead of being limited to the preloaded set
        if name in META_TOOL_NAMES:
            callable_meta = getattr(tool_loader, "get_meta_callable", lambda _n: None)(name)
            if callable_meta is None:
                # openlumara only offers these while dynamic loading is on; if
                # pi asks anyway, say what the default chain would say
                return {"status": "error", "content": "Dynamic tool loading is disabled. "
                        "The meta tool (tools_load) is not available. You have access to all "
                        "available tools without needing to load them."}
            return await self._run(callable_meta, arguments)

        entry = catalog.get(name)
        module_instance = None
        method_name = None
        if entry is not None:
            module_instance = self.manager.modules.get(entry["module"])
            if module_instance is not None:
                method_name = entry["method"]

        if module_instance is None or method_name is None:
            self._log("toolcall", f"pi asked for unknown tool {name}")
            return {"status": "error", "content": f"Tool named {name} does not exist."}

        if method_name in getattr(module_instance, "disabled_tools", ()):
            return {"status": "error", "content": "That tool has been disabled by the user."}

        if name not in getattr(self.manager, "tool_names", []):
            return {"status": "error",
                    "content": f"Tool {name} is not available. This tool may be disabled or unavailable."}

        return await self._run(getattr(module_instance, method_name), arguments)

    async def _run(self, func_callable, arguments: dict) -> dict:
        """call a tool (or meta tool) with openlumara's own timeout and render
        its result the way the default chain renders it for the model"""
        name = getattr(func_callable, "__name__", "tool")
        timeout_val = float(core.config.get("core", "tool_timeout", default=10.0))

        channel = getattr(self.manager, "channel", None)
        if channel is not None:
            channel.log("toolcall", self._display_call(name, arguments))
        try:
            async with self._exec_lock:
                tool_task = asyncio.create_task(func_callable(**(arguments or {})))
                func_response = await asyncio.wait_for(tool_task, timeout=timeout_val)
        except asyncio.TimeoutError:
            tool_task.cancel()
            try:
                await tool_task
            except asyncio.CancelledError:
                pass
            return {"status": "error", "content": f"Tool timed out after {timeout_val}s"}
        except Exception as e:
            message = core.detail_error(e) if core.debug else str(e)
            return {"status": "error", "content": f"Error while executing tool: {message}"}

        # hand pi the same text the default chain puts into its tool message:
        # module.result() dicts carry their model-facing text in "content"
        # (flattening it keeps the tool card byte-identical to a
        # default-backend card instead of double-wrapping the json), and the
        # status decides whether pi marks the call as failed
        if isinstance(func_response, str):
            content = func_response
            failed = False
        elif isinstance(func_response, dict) and isinstance(func_response.get("content"), str):
            content = func_response["content"]
            failed = func_response.get("status") == "error"
        else:
            content = json.dumps(func_response)
            failed = isinstance(func_response, dict) and func_response.get("status") == "error"

        return {"status": "error" if failed else "success", "content": content}

    def _display_call(self, name: str, arguments: dict) -> str:
        arg_strs = []
        for key, value in (arguments or {}).items():
            text = str(value)
            if len(text) > 30:
                text = text[:30] + ".."
            arg_strs.append(f'{key}="{text}"')
        return f"🔧 {name}({', '.join(arg_strs)})"
