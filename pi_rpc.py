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
JSONL RPC client for `pi --mode rpc`.

This speaks pi's documented RPC protocol (docs/rpc.md in the pi package):
one JSON object per line into the child's stdin, response lines
({type: "response"} with a matching id) resolve pending commands, and every
other line is an agent event that goes to the event queue for consumers to
iterate.

the extension UI subprotocol (docs/rpc-extension-ui.md) is answered here:
pi hands plugins ctx.ui.select/confirm/input/editor as an
extension_ui_request that blocks inside pi until a matching
extension_ui_response arrives. OpenLumara has no dialog widget, so a
permission-gate plugin would otherwise freeze the turn forever; the client
therefore auto-cancels every dialog the moment it arrives (what a plugin
sees is exactly what a user pressing escape produces: the guarded action
is declined, nothing hangs). Fire-and-forget methods (notify, setStatus,
...) expect no response. Every UI record also goes to the event queue and
to the optional on_ui_request callback so the module can log or surface it.

Framing note from the protocol docs: LF is the only record delimiter, and
asyncio's StreamReader splits on LF only, so readline() is protocol-compliant
(unlike Node's readline, which also breaks on U+2028/U+2029).
"""

import asyncio
import json
import os

# pi can print very large tool results; give the stream reader plenty of headroom
READER_LIMIT = 64 * 1024 * 1024

# how many stderr lines to keep around for error reports
STDERR_TAIL_LINES = 40

# ctx.ui methods that wait forever for a client response
# (docs/rpc-extension-ui.md); the fire-and-forget ones are not in here
UI_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

class PiRPCError(Exception):
    """a command came back with success: false, or the process died mid-command"""
    pass


class PiRPCClient:
    """
    controls one `pi --mode rpc` subprocess over stdin/stdout JSONL
    """

    def __init__(self, binary, cwd=None, extra_args=None, env=None, log=None, on_ui_request=None):
        self.binary = binary
        self.cwd = cwd
        self.extra_args = list(extra_args or [])
        # extra environment variables for the child (None = plain inherit);
        # used to hand pi the injected provider config and API key
        self.env = env
        self._log = log or (lambda category, message: None)
        # called for every extension_ui_request with (request, auto_resolved),
        # after any automatic dialog response has been sent
        self._on_ui_request = on_ui_request

        self.process = None
        self.alive = False
        self.stderr_tail = []

        self._events = asyncio.Queue()
        self._pending = {} # response id -> Future
        self._next_id = 0
        self._readers = []

    # ------------------------- lifecycle -------------------------

    async def start(self):
        """spawn `pi --mode rpc` and start draining its stdout/stderr"""
        args = [self.binary, "--mode", "rpc"] + self.extra_args
        env = {**os.environ, **self.env} if self.env else None

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd or None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=READER_LIMIT
            )
        except (OSError, ValueError) as e:
            raise PiRPCError(f"could not start pi ({e})")

        self.alive = True
        self.stderr_tail = []
        self._readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    async def stop(self):
        """close stdin, then terminate (and finally kill) the process"""
        self.alive = False
        proc = self.process
        self.process = None

        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass

        for task in self._readers:
            task.cancel()
        self._readers = []

        self._fail_pending("the pi process was stopped")

    # ------------------------- io plumbing -------------------------

    async def _read_stdout(self):
        """read JSONL records from pi's stdout and dispatch them"""
        reader = self.process.stdout
        reason = "the pi process exited"

        while True:
            try:
                line = await reader.readline()
            except Exception as e:
                reason = f"reading pi's stdout failed ({e})"
                break

            if not line:
                break # EOF: pi closed the pipe, i.e. it exited

            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue # not JSON: nothing sane to do with it

            if not isinstance(obj, dict):
                continue

            self._dispatch(obj)

        code = self.process.returncode
        if code is not None:
            reason += f" (exit code {code})"

        self.alive = False
        self._fail_pending(reason)
        self._events.put_nowait({"type": "__dead__", "reason": reason})

    def _dispatch(self, obj):
        """route one parsed stdout record: resolve a pending command, answer
        extension UI dialogs, queue everything else as an agent event"""
        if obj.get("type") == "response":
            fut = self._pending.pop(str(obj.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(obj)
            return

        if obj.get("type") == "extension_ui_request":
            self._answer_ui_request(obj)

        self._events.put_nowait(obj)

    def _answer_ui_request(self, request):
        """see the module docstring: dialogs are answered 'cancelled' on
        arrival so no plugin can block the turn, and the record is handed to
        the on_ui_request callback (never raises out of the stdout reader)"""
        auto_resolved = request.get("method") in UI_DIALOG_METHODS
        if auto_resolved:
            self._write_line({
                "type": "extension_ui_response",
                "id": request.get("id"),
                "cancelled": True,
            })

        if self._on_ui_request is not None:
            try:
                self._on_ui_request(request, auto_resolved)
            except Exception as e:
                self._log("pi", f"extension UI handler failed: {e}")

    def _write_line(self, payload):
        """fire-and-forget one JSONL record to pi's stdin (nothing awaits it)"""
        proc = self.process
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return False
        try:
            proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        except Exception:
            return False

        async def _drain():
            try:
                await proc.stdin.drain()
            except Exception:
                pass

        asyncio.ensure_future(_drain())
        return True

    async def _read_stderr(self):
        """keep a tail of pi's stderr around for diagnostics"""
        reader = self.process.stderr

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                self.stderr_tail.append(text)
                if len(self.stderr_tail) > STDERR_TAIL_LINES:
                    self.stderr_tail.pop(0)
        except (asyncio.CancelledError, Exception):
            pass

    def _fail_pending(self, reason):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PiRPCError(reason))
        self._pending = {}

    # ------------------------- commands -------------------------

    async def send(self, cmd: dict, timeout: float = 60.0):
        """
        send one command and wait for its correlated response.
        returns the response's data field, or raises PiRPCError
        """
        if not self.alive or self.process is None:
            raise PiRPCError("the pi process is not running")

        self._next_id += 1
        req_id = str(self._next_id)

        payload = dict(cmd)
        payload["id"] = req_id

        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise PiRPCError(f"could not write to pi ({e})")

        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise PiRPCError(f"pi did not answer '{cmd.get('type')}' within {timeout}s")

        if not resp.get("success", False):
            raise PiRPCError(str(resp.get("error") or f"pi rejected '{cmd.get('type')}'"))

        return resp.get("data")

    async def next_event(self, timeout: float = None):
        """await the next agent event (raises asyncio.TimeoutError on timeout)"""
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout)

    def clear_events(self):
        """drop stale events so the next turn only sees fresh ones"""
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ------------------------- command wrappers -------------------------

    async def prompt(self, message, images=None, timeout: float = 60.0):
        cmd = {"type": "prompt", "message": message}
        if images:
            cmd["images"] = images
        return await self.send(cmd, timeout=timeout)

    async def steer(self, message, images=None):
        cmd = {"type": "steer", "message": message}
        if images:
            cmd["images"] = images
        return await self.send(cmd)

    async def follow_up(self, message, images=None):
        cmd = {"type": "follow_up", "message": message}
        if images:
            cmd["images"] = images
        return await self.send(cmd)

    async def abort(self):
        return await self.send({"type": "abort"}, timeout=15.0)

    async def get_state(self):
        return await self.send({"type": "get_state"}, timeout=15.0)

    async def new_session(self):
        return await self.send({"type": "new_session"})

    async def switch_session(self, session_path):
        return await self.send({"type": "switch_session", "sessionPath": session_path})

    async def set_session_name(self, name):
        return await self.send({"type": "set_session_name", "name": name})

    async def get_available_models(self):
        return await self.send({"type": "get_available_models"})

    async def set_model(self, provider, model_id):
        cmd = {"type": "set_model", "modelId": model_id}
        if provider:
            cmd["provider"] = provider
        return await self.send(cmd)

    async def get_available_thinking_levels(self):
        return await self.send({"type": "get_available_thinking_levels"})

    async def set_thinking_level(self, level):
        return await self.send({"type": "set_thinking_level", "level": level})

    async def get_session_stats(self):
        return await self.send({"type": "get_session_stats"})

    async def compact(self, custom_instructions=None):
        cmd = {"type": "compact"}
        if custom_instructions:
            cmd["customInstructions"] = custom_instructions
        return await self.send(cmd, timeout=300.0)

    async def set_auto_compaction(self, enabled: bool):
        return await self.send({"type": "set_auto_compaction", "enabled": bool(enabled)}, timeout=15.0)

    async def get_commands(self):
        return await self.send({"type": "get_commands"})
