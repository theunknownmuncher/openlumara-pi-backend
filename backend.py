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
The pi-backed stand-in for OpenLumara's API client.

Every channel reaches the AI through manager.API: send(), send_stream(),
cancel(), connect/disconnect, list_models/set_model, get_status. PiBackend
implements exactly that surface on top of a `pi --mode rpc` subprocess driven
through pi's RPC protocol (the same layer the interactive, print and web modes
of pi sit on), so the webui (streaming chain, stop button, model switcher,
context pill, stream state), the CLI, telegram, the scheduler and commands
like /status and /model keep working unchanged - they just drive pi now.

pi event stream -> OpenLumara stream token mapping:
  text_delta        -> {"type": "content"}          (answer bubble)
  thinking_delta    -> {"type": "reasoning"}        (chain thinking card)
  tool calls        -> {"type": "tool_call_delta"} per streamed argument chunk,
                       {"type": "tool_calls"} per finalized assistant message
                       and {"type": "tool"} per finished call: exactly the
                       OpenAI-shaped tokens the default backend streams, so
                       every channel renders pi's tools as native tool cards
  tool responses    -> folded into those cards by the core TurnCollector
  tool execution    -> the card's live result (no separate panel or feed)
  after settle      -> {"type": "token_usage"} from pi's real context usage

OpenLumara's own toolcall chain never *executes* anything for pi: the calls
are pi's own tools, already executed inside pi's session by the time they
stream here (core/toolcalls.py's guard keeps ToolcallManager.process() from
running them a second time). The tool_calls / tool tokens this backend emits
are display-only, and they carry the same message shapes the default backend
adds to the chat store, so live streaming and reloaded history match.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid

import core

from .pi_rpc import PiRPCClient, PiRPCError

# openlumara's context compression call synthesizes this prompt as the final
# message. auto-compression is patched out while pi is active (pi compacts its
# own session) and /compress is routed to pi, so this marker only guards
# against a compression request that races module (re)loading
COMPRESSION_MARKER = "Compress the conversation so far"

# how long a turn may go without a single pi event before the stream is
# declared stuck (tool runs keep streaming updates, so this is generous)
IDLE_EVENT_TIMEOUT = 600.0

# how long to wait for pi to accept a prompt before calling it a hang
PROMPT_ACK_TIMEOUT = 60.0

# how long cancel() and the abort-drain wait for pi to settle
ABORT_SETTLE_TIMEOUT = 15.0

# how fresh the "user just sent a message" note must be for the non-streaming
# send() path to treat the call as an interactive turn instead of a direct
# backend call (e.g. from the scheduler)
INTERACTIVE_NOTE_WINDOW = 15.0

# cap for one chain activity note, so a huge tool result cannot flood the chat
NOTE_CHAR_CAP = 2000

# pi refuses to call a provider whose key resolves to an empty string, even
# for auth-free local servers (verified: "No API key found"). pi's own docs
# prescribe a dummy key for endpoints that ignore authentication (same trick
# as their "apiKey": "ollama" example), so an unset key gets this value
DUMMY_API_KEY = "openlumara"


def _text_and_images(message: dict):
    """split an OpenAI-style user message into plain text plus pi ImageContent blocks"""
    text, images, _ = _extract_message(message)
    return text, images

# what OpenLumara's own multimodal pipeline names things ("File: x",
# "[Binary file: x]"); these markers follow that style so the agent reads
# attachments it cannot receive the same way it reads any other attachment
UNDELIVERED_IMAGE_NOTE = "[Attached image was not forwarded to you: {name}]"
UNDELIVERED_AUDIO_NOTE = "[Attached audio file cannot be delivered to you: {name}]"


def _extract_message(message: dict, forward_images: bool = True):
    """
    turn an OpenAI-style user message (as core/channel._process_multimodal
    builds it) into what pi can be given: (text, ImageContent blocks, notes).

    pi takes text and images - everything else _process_multimodal already
    flattened to text (plain files, PDFs), so it rides along in the text.
    What pi genuinely cannot carry is audio (pi's rpc only accepts
    ImageContent attachments), and images when the user turned forwarding
    off. The default backend forwards those to its endpoint; rather than
    letting them vanish without a trace here, each undeliverable block turns
    into a note appended to the prompt, so the agent knows the user attached
    something and can say so instead of answering as if nothing arrived.
    """
    content = message.get("content")

    if isinstance(content, str):
        return content, [], []

    names = (message.get("_metadata") or {}).get("filenames") or []

    texts = []
    images = []
    notes = []
    for index, part in enumerate(content or []):
        # _process_multimodal records one filename per block, "" for the
        # message text itself; fall back to a positional name
        name = names[index] if index < len(names) else ""
        if not isinstance(part, dict):
            continue

        if part.get("type") == "text":
            texts.append(part.get("text") or "")

        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") or ""
            match = re.match(r"data:([^;]+);base64,(.+)", url, re.DOTALL)
            if not match:
                continue
            if forward_images:
                images.append({
                    "type": "image",
                    "data": match.group(2),
                    "mimeType": match.group(1),
                })
            else:
                notes.append(UNDELIVERED_IMAGE_NOTE.format(name=name or f"image{len(notes) + 1}"))

        elif part.get("type") == "input_audio":
            fmt = str((part.get("input_audio") or {}).get("format") or "audio")
            notes.append(UNDELIVERED_AUDIO_NOTE.format(
                name=name or f"audio{len(notes) + 1}.{fmt}"))

    return "\n".join(t for t in texts if t), images, notes


def _last_user_message(messages):
    """the newest real user message in a message list (skips signals, ghosts and command echoes)"""
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        meta = message.get("_metadata") or {}
        if meta.get("signal") or meta.get("ghost") or meta.get("is_cmd"):
            continue
        return message
    return None


def _is_compression_message(message: dict) -> bool:
    text, _ = _text_and_images(message)
    return COMPRESSION_MARKER in text[:200]


def _compact_json(value, limit: int = 160) -> str:
    """one-line rendering of tool arguments for activity notes"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)

    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + ".."
    return text


def _excerpt(text, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit] + ".."
    return text


def _result_text(result) -> str:
    """join the text blocks of a pi tool result / partial result"""
    if not isinstance(result, dict):
        return str(result or "")
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def _tool_response_payload(result, is_error: bool) -> str:
    """
    render a pi tool result the way core/toolcalls.py renders a default-backend
    tool response ({"status": ..., "content": ...} json), so tool cards and
    their failure tinting behave identically for both backends.
    """
    payload = {"status": "error" if is_error else "success", "content": _result_text(result)}
    if isinstance(result, dict) and result.get("details"):
        payload["details"] = result["details"]
    return json.dumps(payload, ensure_ascii=False)


def _tool_card(call_id: str, name: str, args_json: str) -> dict:
    """one OpenAI-shaped tool call entry, the shape ToolcallManager and the
    webui's tool cards consume on the stream and in the chat store"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name or "tool", "arguments": args_json or "{}"},
    }

# per-line caps for the rebuilt history block (see _render_chat_history):
# pi only needs the gist of past turns, not full tool transcripts
HISTORY_ARG_CHARS = 120
HISTORY_RESULT_CHARS = 240
HISTORY_TOTAL_CHARS = 48000

# how long pi's cached session stats may be reused for the context metrics the
# UI shows; a turn refreshes them anyway, this only keeps chat switches and
# the /status command from firing an rpc per read
CONTEXT_METRICS_TTL = 5.0


def _history_fingerprint(messages) -> str:
    """cheap identity of a chat store's message list (count + content hash),
    used to detect edits / deletions / regenerates that bypassed pi"""
    payload = json.dumps(messages, ensure_ascii=False, default=str)
    return f"{len(messages or [])}:{hashlib.md5(payload.encode('utf-8')).hexdigest()}"


def _skipped_in_history(message: dict) -> bool:
    """ghost / signal / command messages are OpenLumara-side plumbing; the
    default backend sends them no more than it sends its system prompt"""
    meta = message.get("_metadata") or {}
    return bool(meta.get("ghost") or meta.get("signal") or meta.get("is_cmd"))


def _render_chat_history(messages) -> str:
    """
    render the OpenLumara chat store into the recap block pi gets when its
    session has to be rebuilt after the history was edited, deleted from or
    regenerated. the default backend rebuilds its model context from the
    chat store on every single request, so those operations simply apply;
    pi's context lives in its session instead, so the module replays what
    the chat store still shows - pi then sees exactly what the user sees.
    Tool activity is kept one line per call: it preserves the gist without
    pretending tool outputs from long ago still describe the present.
    """
    lines = []
    for message in messages or []:
        if not isinstance(message, dict) or _skipped_in_history(message):
            continue
        role = message.get("role")

        if role == "user":
            # attachments: image bytes are not replayed into a recap (stale
            # pixels help nobody), but their presence is - like the audio that
            # pi cannot carry at all, so the replay still says what arrived
            text, images, notes = _extract_message(message)
            text = text.strip()
            if images:
                text = (text + " " if text else "") + f"({len(images)} image(s) attached)"
            for note in notes:
                text = (text + " " if text else "") + note
            if text:
                lines.append(f"User: {text}")

        elif role == "assistant":
            text, _ = _text_and_images(message)
            calls = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = _compact_json(fn.get("arguments"), HISTORY_ARG_CHARS)
                calls.append(f"{fn.get('name', 'tool')}({args})")
            if message.get("reasoning_content"):
                calls.append("(thought it through)")
            body = text.strip()
            if calls:
                body = (body + " " if body else "") + f"[ran tools: {'; '.join(calls)}]"
            if body:
                lines.append(f"Assistant: {body}")

        elif role == "tool":
            excerpt = _excerpt(_result_text({"content": [{"type": "text", "text": message.get("content") or ""}]}),
                               HISTORY_RESULT_CHARS)
            if excerpt:
                lines.append(f"  tool result: {excerpt}")

    if not lines:
        return ""
    history = "\n".join(lines)
    if len(history) > HISTORY_TOTAL_CHARS:
        history = "[..earlier turns trimmed..]\n" + history[-HISTORY_TOTAL_CHARS:]
    return history


class _CallSlot:
    """one in-flight pi tool call inside a turn: streams its argument deltas,
    emits its card once, then its response once (ids correlate the events,
    but pi-assigned nested ids never have a card of their own)"""

    def __init__(self, index):
        self.index = index
        self.id = None
        self.name = None
        self.args = ""
        self.card_emitted = False
        self.responded = False


class PiBackend(core.api.APIClient):
    """
    drop-in replacement for core.api.APIClient, backed by `pi --mode rpc`

    the base class is only reused for its attribute layout; everything that
    would talk to an OpenAI-compatible HTTP API is overridden here.
    """

    def __init__(self, manager, module):
        super().__init__(manager)

        self.module = module # settings, session map, logging

        self.pi = None # PiRPCClient, (re)created on connect
        self.supports_developer_role = False

        self._turn_lock = asyncio.Lock()
        self._stream_active = False
        self._bound_chat = None # openlumara chat id the pi session is bound to
        self._model_id = None # last known pi model ("provider/id")
        self._thinking_level = None
        self._pending_model = None # model to (re)apply on the next spawn
        self._interactive_note = None # (text, monotonic ts) from module.on_user_message
        self._turn_calls = {} # contentIndex -> _CallSlot for the running turn
        self._turn_calls_by_id = {} # toolCallId -> _CallSlot for the running turn
        self._turn_text = [] # assistant text blocks since the last message_end
        self._turn_reasoning = [] # thinking blocks since the last message_end
        self.stats = {} # last get_session_stats result, for /pi status and the context metrics
        self._stats_taken = 0.0 # monotonic time of the last get_session_stats
        self._history_marks = {} # chat key -> message-count + fingerprint watermark
        self._pending_recap = None # recap a rebuilt session still owes pi, consumed by the turn that rebuilt it
        self._projection = {"key": None} # cached scan of pi's session file (context metrics)

    # ------------------------- lifecycle -------------------------

    async def _ensure_process(self):
        """start the pi child if it is not running. returns True or an APIError"""
        if self.pi is not None and self.pi.alive:
            self.connected = True
            return True

        binary = self.module.binary
        if not binary:
            self.connected = False
            return core.api.APIError(
                "pi was not found. Install it with 'npm install -g @earendil-works/pi-coding-agent' "
                "or point the pi_path setting at your pi binary."
            )

        env, provider_args, provider_note = await self._prepare_provider_injection()

        client = PiRPCClient(
            binary,
            cwd=self.module.resolved_cwd(),
            extra_args=self.module.extra_args() + provider_args,
            env=env,
            log=self.manager.log,
            on_ui_request=self._on_extension_ui_request,
        )

        try:
            await client.start()
        except PiRPCError as e:
            tail = " | ".join(client.stderr_tail[-3:]) if client.stderr_tail else ""
            self.connected = False
            return core.api.APIError(f"could not start pi: {e}" + (f" (stderr: {tail})" if tail else ""))

        self.pi = client
        self.connected = True
        self._bound_chat = None
        self.manager.log("pi", f"started pi rpc process (cwd {self.module.resolved_cwd()})")
        if provider_note:
            self.manager.log("pi", provider_note)

        # re-apply a model that was chosen while the process was down
        if self._pending_model:
            name = self._pending_model
            self._pending_model = None
            await self._apply_model(name)

        # pick up pi's startup state (model, thinking level)
        await self._refresh_state()

        return True

    async def connect(self, silent=False):
        result = await self._ensure_process()
        if result is True and not silent:
            self.manager.log("pi", "connected to the pi coding agent")
        return result

    async def attempt_connect(self):
        if self.pi is not None and self.pi.alive:
            self.connected = True
            return True
        return await self.connect(silent=True)

    async def disconnect(self):
        if self.pi is not None:
            await self.pi.stop()
            self.pi = None
        self.connected = False
        self._bound_chat = None
        return True

    async def reconnect(self):
        await self.disconnect()
        return await self.connect()

    async def shutdown(self):
        """module teardown: make sure no pi process is left behind"""
        await self.disconnect()

    async def restart_process(self):
        """restart the pi child (settings changed, or the user asked via /pi restart)"""
        await self.disconnect()
        # the new process never saw the sessions the old one had fed, so no
        # chat may be assumed in sync with it
        self.clear_history_marks()
        self._bound_chat = None
        return await self.connect()

    # ------------------------- provider injection -------------------------

    async def _fetch_endpoint_models(self, base_url: str, api_key: str):
        """best-effort GET {base_url}/models: mirrors the endpoint's own model
        list into pi, so the model switcher shows what the server actually
        offers. Any failure just yields an empty list"""
        import aiohttp

        headers = {"Authorization": f"Bearer {api_key or DUMMY_API_KEY}"}
        try:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{base_url}/models", headers=headers) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
        except Exception:
            return []

        ids = []
        for entry in (data or {}).get("data") or []:
            model_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(model_id, str) and model_id and model_id not in ids:
                ids.append(model_id)
        return ids

    async def _prepare_provider_injection(self):
        """
        build the env + CLI args that configure pi with the resolved API and
        model settings (OpenLumara's settings by default, the module's manual
        api_url / api_key / model settings win when filled in), plus the
        settings that steer which of pi's own configuration is in play
        (pi_agent_dir, project_trust).

        The endpoint and its model list are written to data/pi_provider.json
        and registered inside pi by pi_provider_extension.js (--extension).
        The API key never enters that file: pi resolves it from
        PI_OPENLUMARA_API_KEY in the child's environment. Returns
        (env_overrides, extra_args, log_note).
        """
        base_url, api_key, model = self.module.resolved_provider_settings()

        env = {}
        args = []
        notes = []
        endpoint_models = []

        # pi's own configuration folder: by default pi reads the user's real
        # pi install (~/.pi/agent: settings.json, auth.json, models.json,
        # trust.json, the extensions/, skills/ and prompts/ folders, and the
        # package declarations), so an existing pi setup carries over to
        # OpenLumara untouched. The setting only relocates that folder
        agent_dir = str(self.module.config.get("pi_agent_dir") or "").strip()
        if agent_dir:
            resolved_dir = os.path.abspath(os.path.expanduser(agent_dir))
            env["PI_CODING_AGENT_DIR"] = resolved_dir
            notes.append(f"pi reads its own configuration from {resolved_dir}")

        # project trust gates the working directory's .pi/ folder (project
        # settings, project extensions, project packages). RPC mode cannot
        # prompt and silently skips those resources unless a decision was
        # saved (e.g. from pi's /trust) or forced here
        trust = str(self.module.config.get("project_trust") or "auto").strip().lower()
        if trust == "approve":
            args.append("--approve")
            notes.append("project trust granted: the working dir's .pi/ settings and extensions load")
        elif trust in ("ignore", "deny"):
            args.append("--no-approve")
            notes.append("project trust refused: the working dir's .pi/ settings and extensions are ignored")

        if base_url:
            endpoint_models = await self._fetch_endpoint_models(base_url, api_key)
            served = list(endpoint_models)
            if model and model not in served:
                # keep the configured default model selectable even when the
                # endpoint's list could not be fetched (or omits it)
                endpoint_models.insert(0, model)

            try:
                window = int(core.config.get("api", "max_context", 128000))
            except (TypeError, ValueError):
                window = 128000
            try:
                max_out = int(core.config.get("api", "max_output_tokens", 8192))
            except (TypeError, ValueError):
                max_out = 8192
            # pi only offers thinking for models flagged as reasoning-capable:
            # mirror OpenLumara's own thinking setting onto the injected models
            reasoning = bool(core.config.get("model", "enable_thinking", default=True))
            # pi only attaches images for models flagged as image-capable: an
            # entry that says ["text"] makes pi swap every image for a
            # "model doesn't support image input" note, so uploads would never
            # reach the endpoint. the default backend has no such gate - it
            # forwards images and lets the endpoint decide - so mirror that
            # whenever the module is allowed to forward images at all
            input_types = ["text"]
            if bool(self.module.config.get("forward_images", default=True)):
                input_types.append("image")

            entries = [
                {
                    "id": mid,
                    "name": mid,
                    "reasoning": reasoning,
                    "input": list(input_types),
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": window,
                    "maxTokens": max_out,
                }
                for mid in endpoint_models
            ]

            payload = {
                "provider": self.module.PROVIDER_ID,
                "displayName": "OpenLumara",
                "baseUrl": base_url,
                "apiKeyEnv": "PI_OPENLUMARA_API_KEY",
                "api": "openai-completions",
                "models": entries,
            }
            config_path = core.get_data_path("pi_provider.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)

            env["PI_OPENLUMARA_CONFIG"] = config_path
            env["PI_OPENLUMARA_API_KEY"] = api_key or DUMMY_API_KEY
            args += ["--extension", self.module.provider_extension_path()]

        # initial model selection: a bare id (or one the injected endpoint
        # actually serves) goes to that endpoint; anything else like
        # "anthropic/claude-..." is handed to pi as a native model pattern
        if model and base_url and (model in served or "/" not in model):
            args += ["--provider", self.module.PROVIDER_ID, "--model", model]
        elif model:
            args += ["--model", model]
        elif endpoint_models:
            # no model configured: start on the first one the endpoint offers
            args += ["--provider", self.module.PROVIDER_ID, "--model", endpoint_models[0]]

        if base_url:
            initial = model or (endpoint_models[0] if endpoint_models else "pi's own default")
            notes.append(f"pi runs on the OpenLumara API endpoint {base_url} ({len(endpoint_models)} models, initial model {initial})")
            if not api_key:
                notes.append(f"no API key configured: pi gets the dummy key '{DUMMY_API_KEY}' it requires even for auth-free endpoints; local servers ignore it")

        # the tool bridge: OpenLumara's own tools, registered inside pi by
        # pi_tool_bridge_extension.js and executed by the loopback server
        # (tool_bridge.py) when pi calls one. pi's built-in tools are not
        # touched by this - they keep running natively inside pi.
        bridge = getattr(self.module, "bridge", None)
        if bridge is not None and bridge.url:
            env["PI_OPENLUMARA_BRIDGE_URL"] = bridge.url
            env["PI_OPENLUMARA_BRIDGE_TOKEN"] = bridge.token
            args += ["--extension", self.module.tool_bridge_extension_path()]

            # spawn-time tool manifest, so the extension can register the
            # tools synchronously at load (a session_start refresh picks up
            # anything that changed since)
            from .tool_bridge import bridge_tool_definitions
            manifest_path = core.get_data_path("pi_tools.json")
            try:
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump({"tools": bridge_tool_definitions(self.manager)}, f)
                env["PI_OPENLUMARA_TOOLS_CONFIG"] = manifest_path
            except OSError as e:
                self.manager.log("pi", f"could not write the tool manifest: {e}")

            notes.append(f"OpenLumara tools are bridged to pi via {bridge.url}")

        return env, args, " | ".join(notes) if notes else None

    # ------------------------- extension UI -------------------------

    def _extension_ui_note(self, request, auto_resolved):
        """one line about an extension_ui_request, or None for the
        fire-and-forget records nobody needs to hear about (setStatus,
        setTitle, set_editor_text, ... - pi degrades them itself)"""
        method = str(request.get("method") or "?")
        if method in ("select", "confirm", "input", "editor"):
            label = str(request.get("title") or request.get("message") or "a question").strip()
            line = f"pi extension asked for a {method} dialog ({label!r})"
            if auto_resolved:
                line += " - answered 'cancelled': OpenLumara has no dialog widget, so whatever the plugin was guarding stays declined"
            return line
        if method == "notify":
            return f"pi extension notified [{request.get('notifyType') or 'info'}]: {request.get('message') or ''}"
        return None

    def _on_extension_ui_request(self, request, auto_resolved):
        """called by the rpc client for every ctx.ui call a plugin makes
        (after any automatic dialog cancellation was sent): keep it visible
        in the log, since a silently declined permission gate is otherwise
        indistinguishable from a plugin that just does nothing"""
        line = self._extension_ui_note(request, auto_resolved)
        if line:
            self.manager.log("pi", line)

    # ------------------------- state helpers -------------------------

    async def _refresh_state(self):
        """cache pi's current model / thinking level / session state"""
        if self.pi is None or not self.pi.alive:
            return None
        try:
            state = await self.pi.get_state()
        except PiRPCError as e:
            self.manager.log("pi", f"get_state failed: {e}")
            return None

        model = state.get("model") or {}
        if model.get("id"):
            self._model_id = f"{model.get('provider')}/{model.get('id')}"
        self._thinking_level = state.get("thinkingLevel")
        return state

    async def refresh_stats(self):
        """cache pi's session statistics (token usage, cost, context window fill)"""
        if self.pi is None or not self.pi.alive:
            self.stats = {}
            self._stats_taken = 0.0
            return self.stats
        try:
            self.stats = await self.pi.get_session_stats() or {}
            self._stats_taken = time.monotonic()
        except PiRPCError:
            self.stats = {}
        return self.stats

    def _measured_context(self):
        """
        pi's own measurement of what currently sits in the context window, as
        (tokens, window), or None when pi has no measurement to give.

        pi derives it from the last real request's usage, so it is the direct
        equivalent of the prompt size the default backend shows from the API's
        usage data. It is absent for a session nobody has prompted yet, and
        for one compacted since its last assistant message. The cumulative
        session totals are NOT a stand-in: they add up every cached re-read of
        the context across the whole session, so they grow past the window and
        would push both the pill and the compression trigger off the rails.
        """
        usage = (self.stats or {}).get("contextUsage") or {}
        try:
            tokens = int(usage.get("tokens") or 0)
            window = int(usage.get("contextWindow") or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= 0 or window <= 0:
            return None
        return tokens, window

    async def pi_context_fill(self, max_age: float = 30.0):
        """
        pi's own share of its model context, as a ratio of the context
        window (0.0 when unknown). OpenLumara's estimators describe the
        context the default backend would build - which is never sent while
        pi is the backend - so threshold checks measure pi's real session.
        Stats are cached for `max_age` seconds: is_over_threshold() runs on
        every stored message, and each call here is an rpc round trip.
        """
        if self.pi is None or not self.pi.alive:
            return 0.0
        if time.monotonic() - self._stats_taken > max_age:
            await self.refresh_stats()

        measured = self._measured_context()
        if measured is None:
            return 0.0
        tokens, window = measured
        return tokens / window

    def _session_projection_metrics(self, session_file):
        """
        what pi's session currently carries, read from pi's own session file:
        the standing prompt and tool schemas it sends (the system entry records
        both), the conversation still in context, and how often it was
        compacted. pi's rpc reports only the total, so this is how the context
        popup gets a breakdown that describes pi instead of a context that is
        never sent.

        cached by the file's identity; a compaction changes the projection, and
        it always rewrites the file, so the cache cannot go stale silently.
        """
        blank = {"compactions": 0, "system_chars": 0, "system_words": 0,
                 "tools": 0, "tools_chars": 0, "tools_words": 0,
                 "history_chars": 0, "history_words": 0}
        if not session_file:
            return dict(blank)
        try:
            stat = os.stat(session_file)
        except OSError:
            return dict(blank)

        key = (session_file, stat.st_mtime_ns, stat.st_size)
        if self._projection.get("key") == key:
            return {k: v for k, v in self._projection.items() if k != "key"}

        result = dict(blank)
        system_parts, tool_parts, history_parts = [], [], []

        def texts(block):
            """every piece of text pi carries inside one message"""
            if isinstance(block, str):
                yield block
            elif isinstance(block, dict):
                for field in ("text", "thinking", "content", "arguments", "details"):
                    value = block.get(field)
                    if isinstance(value, str):
                        yield value
                    elif value is not None:
                        yield from texts(value)
            elif isinstance(block, list):
                for item in block:
                    yield from texts(item)

        def measure(chunks):
            text = "\n".join(chunks)
            return len(text), len(text.split())

        try:
            with open(session_file, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    kind = entry.get("type")

                    if kind == "compaction":
                        # pi's live context restarts here: everything before is
                        # replaced by the summary, which is what the model sees
                        result["compactions"] += 1
                        history_parts = [str(entry.get("summary") or "")]
                        continue

                    if kind != "message":
                        continue
                    message = entry.get("message") or {}
                    role = message.get("role")

                    if role == "system":
                        sections = message.get("sections") or {}
                        system_parts.extend(str(v) for v in sections.values() if v)
                        tools = message.get("toolsAdded") or []
                        result["tools"] = len(tools)
                        for tool in tools:
                            tool_parts.append(str(tool.get("name") or ""))
                            tool_parts.append(str(tool.get("description") or ""))
                            tool_parts.append(json.dumps(tool.get("parameters") or {}, sort_keys=True))
                    elif role in ("user", "assistant", "toolResult"):
                        history_parts.extend(texts(message.get("content")))
        except OSError:
            return dict(blank)

        result["system_chars"], result["system_words"] = measure(system_parts)
        result["tools_chars"], result["tools_words"] = measure(tool_parts)
        result["history_chars"], result["history_words"] = measure(history_parts)

        self._projection = {"key": key, **result}
        return dict(result)

    async def pi_context_metrics(self, max_age: float = CONTEXT_METRICS_TTL):
        """
        the numbers behind the UI's context metrics, measured on pi's session:
        the same dict shape Context.get_size() returns for the default backend
        (percentages, max_context and the part breakdown the popup stacks), or
        None when pi has nothing to report - not connected, or a session nobody
        has prompted yet, where pi has measured nothing and OpenLumara's own
        estimation is the honest fallback.
        """
        if self.pi is None or not self.pi.alive:
            return None
        if time.monotonic() - self._stats_taken > max_age:
            await self.refresh_stats()

        stats = self.stats or {}
        measured = self._measured_context()
        if measured is None:
            return None
        total, window = measured

        projection = self._session_projection_metrics(stats.get("sessionFile"))
        # the parts are char-based estimates, the total is pi's own measurement;
        # keep the parts inside it so the popup's bar can never claim more than
        # the hero percentage shows
        system_tokens = projection["system_chars"] // 4
        tools_tokens = projection["tools_chars"] // 4
        history_tokens = min(projection["history_chars"] // 4,
                             max(total - system_tokens - tools_tokens, 0))

        return {
            "max_context": window,
            "total_tokens": total,
            "percent_full": round((total / window) * 100),
            "compaction_count": projection["compactions"],
            "total_words": projection["system_words"] + projection["tools_words"] + projection["history_words"],
            "system_prompt": {"tokens": system_tokens, "words": projection["system_words"]},
            "tools": {"active": projection["tools"], "tokens": tools_tokens,
                      "words": projection["tools_words"]},
            "message_history": {"tokens": history_tokens, "words": projection["history_words"]},
            # openlumara's end prompt is inert while pi is the backend
            "end_prompt": {"tokens": 0, "words": 0},
        }

    async def pi_context_tokens(self, max_age: float = CONTEXT_METRICS_TTL):
        """pi's current context occupancy in tokens, or None when unknown"""
        metrics = await self.pi_context_metrics(max_age=max_age)
        return None if metrics is None else metrics["total_tokens"]

    async def pi_context_metrics_for(self, channel):
        """
        metrics for the chat the caller is asking about. pi holds one session
        at a time and binds it per turn, so while it is still bound to another
        chat its numbers describe the wrong conversation - the caller falls
        back to OpenLumara's own estimation in that case, which is exactly what
        the default backend shows before its first API measurement.
        """
        chat_key = self._current_chat_key(channel)
        if self._bound_chat not in (None, chat_key):
            return None
        return await self.pi_context_metrics()

    async def pi_auto_compact_enabled(self):
        """OpenLumara's setting decides whether the agent backend may
        compress context on its own - the default backend reads
        model.automatically_compress_context, pi must honour the same switch"""
        return bool(core.config.get("model", "automatically_compress_context", default=True))

    async def apply_auto_compaction(self):
        """push model.automatically_compress_context into the running pi"""
        if self.pi is None or not self.pi.alive:
            return
        try:
            await self.pi.set_auto_compaction(await self.pi_auto_compact_enabled())
        except PiRPCError as e:
            self.module.log("pi", f"could not sync pi's auto-compaction setting: {e}")

    async def _resolve_model(self, name: str):
        """map a user-supplied model name to (provider, model_id) via pi's model list"""
        name = (name or "").strip()
        if not name:
            return None

        if "/" in name:
            provider, model_id = name.split("/", 1)
            if provider and model_id:
                return provider, model_id

        # bare id: try to find it among pi's configured models
        try:
            data = await self.pi.get_available_models()
        except PiRPCError:
            return None

        for model in (data or {}).get("models", []):
            if model.get("id") == name:
                return model.get("provider"), model.get("id")
        return None

    async def _apply_model(self, name: str):
        """switch pi's active model; returns True on success"""
        if self.pi is None or not self.pi.alive:
            self._pending_model = name
            return False

        resolved = await self._resolve_model(name)
        if resolved is None:
            self.manager.log("pi", f"model '{name}' is not available in pi")
            return False

        provider, model_id = resolved
        try:
            await self.pi.set_model(provider, model_id)
        except PiRPCError as e:
            self.manager.log("pi", f"model switch to '{name}' failed: {e}")
            return False

        self._model_id = f"{provider}/{model_id}" if provider else model_id
        await self._refresh_state()
        return True

    # ------------------------- API client surface -------------------------

    def get_model(self):
        return self._model_id or core.config.get("model", "name")

    def set_model(self, name: str):
        """sync on purpose: /api/model/set calls this without awaiting"""
        name = (name or "").strip()
        if not name:
            return False

        if self.pi is not None and self.pi.alive:
            asyncio.create_task(self._apply_model(name))
        else:
            self._pending_model = name
        return True

    async def list_models(self):
        connected = await self.attempt_connect()
        if connected is not True:
            return connected # APIError; channels and the models module str() it

        try:
            data = await self.pi.get_available_models()
        except PiRPCError as e:
            self.manager.log("pi", f"could not list pi models: {e}")
            return []

        models = []
        for model in (data or {}).get("models", []):
            if not isinstance(model, dict) or not model.get("id"):
                continue
            provider = model.get("provider")
            models.append(f"{provider}/{model['id']}" if provider else model["id"])

        models.sort()
        return models

    def get_status(self):
        return {
            "connected": bool(self.pi and self.pi.alive),
            "url": f"pi --mode rpc, cwd {self.module.resolved_cwd()}",
            "model": self._model_id or core.config.get("model", "name"),
        }

    def note_user_message(self, content: str):
        """module.on_user_message forwards interactive user messages here so
        the non-streaming send() path can tell an interactive turn apart from
        a direct backend call (the scheduler talks to the API directly and
        must get its instruction from the passed context instead)"""
        if isinstance(content, str):
            self._interactive_note = (content, time.monotonic())

    def _turn_is_interactive(self) -> bool:
        note = self._interactive_note
        return bool(note and (time.monotonic() - note[1]) < INTERACTIVE_NOTE_WINDOW)

    # ------------------------- the turn: send_stream -------------------------

    async def send_stream(self, context: list, use_tools=True, tools=None, use_thinking=True, **kwargs):
        """
        the streaming path: hand the user's message to pi and translate pi's
        event stream into OpenLumara stream tokens
        """
        connected = await self.attempt_connect()
        if connected is not True:
            yield {"type": "error", "content": str(connected)}
            return

        # _prompt_source: "chat" (default) reads the message from the active
        # chat store; "context" reads it from the passed context (used by the
        # non-streaming send() for direct backend calls like scheduler jobs)
        prefer = kwargs.get("_prompt_source", "chat")

        async with self._turn_lock:
            channel = self.manager.channel

            # another task may have stopped pi while we waited for the lock
            if self.pi is None or not self.pi.alive:
                connected = await self.attempt_connect()
                if connected is not True:
                    yield {"type": "error", "content": str(connected)}
                    return

            prompt, images, notes = await self._extract_prompt(context, channel, prefer=prefer)
            # notes name the attachments pi cannot carry (audio, or images with
            # forwarding off): the prompt says so instead of them vanishing,
            # so pi can tell the user rather than answer about nothing
            if notes:
                prompt = "\n".join([prompt] + notes).strip()
            if not prompt and not images:
                yield {"type": "error", "content": "pi backend: no user message found to send to pi"}
                return

            try:
                await self._sync_session(channel)
            except PiRPCError as e:
                yield {"type": "error", "content": f"pi backend: session switch failed ({e})"}
                return

            # the chat store may have been edited, trimmed or regenerated
            # since pi's last look at it - the default backend rebuilds from
            # the store every request so those just apply; rebuild pi here
            try:
                await self._rebuild_if_history_diverged(channel)
            except PiRPCError as e:
                yield {"type": "error", "content": f"pi backend: could not resync the session after the history change ({e})"}
                return

            # a session this very turn rebuilt (the history was edited,
            # deleted from or regenerated) still owes pi its recap: one prompt
            # carries replay plus the ask, so pi sees the whole store and then
            # what the user actually wants - and the replay never costs a turn
            # of its own, which the model would end up answering
            if self._pending_recap:
                recap, self._pending_recap = self._pending_recap, None
                prompt = f"{recap}\n\n{prompt}" if prompt else recap

            # pin the client for the whole turn: a settings restart swaps
            # self.pi, and this turn must keep talking to (and draining) the
            # process it started - never crash on a client that went away
            client = self.pi

            client.clear_events()

            # claim the turn before the first command goes out, so the
            # settings watchdog cannot respawn the process underneath us
            self.is_streaming = True
            self._stream_active = True

            try:
                await client.prompt(prompt or "", images=images, timeout=PROMPT_ACK_TIMEOUT)
            except (asyncio.CancelledError, GeneratorExit):
                self.is_streaming = False
                self._stream_active = False
                raise
            except PiRPCError as e:
                message = f"pi rejected the prompt: {e}"
                if "no api key" in str(e).lower():
                    message += (" - the model pi has selected has no credentials. Check the module's "
                                "API settings (use_openlumara_settings / api_url / api_key) or give pi "
                                "its own login; setting changes apply after /pi restart.")
                self.is_streaming = False
                self._stream_active = False
                yield {"type": "error", "content": message}
                return

            # note: the watermark is NOT stamped here. it is stamped once pi
            # actually holds the store (inside a rebuild) and again once this
            # turn's messages are persisted - stamping the prompt-only shape
            # too early would make a later regenerate look like a store pi
            # already has and hide the one change that needs a rebuild
            saw_error = False
            self._reset_turn_state()

            try:
                async for token in self._translate_events(channel, client=client):
                    if token.get("type") == "error":
                        saw_error = True
                    yield token

            except (asyncio.CancelledError, GeneratorExit):
                # the caller dropped the stream (chat switch, regenerate, task
                # cancel): pi must not keep running behind it
                self._abandon_turn()
                raise

            finally:
                self.is_streaming = False
                self._stream_active = False
                self.cancel_request = False

            if saw_error:
                return

            # the default backend's ToolcallManager adds the final assistant
            # message to the chat store once its toolcall chain ends; pi's
            # chain lives inside pi's own session, so the module stores the
            # turn's messages itself (intermediate ones land at message_end,
            # the answer lands here) and the channel skips its own single
            # message persist because our stream carries tool tokens
            final_message = {"role": "assistant", "content": "".join(self._turn_text)}
            if self._turn_reasoning:
                final_message["reasoning_content"] = "".join(self._turn_reasoning)
            if final_message["content"] or self._turn_reasoning:
                await self._persist_assistant(channel, final_message)

            # the turn's persisted messages are in the store now: re-stamp
            # the watermark with whatever it holds (also absorbs messages a
            # second channel added while this turn was running)
            await self._mark_history_synced(channel)

            # report pi's real context usage so the context pill and /status
            # show the pi session instead of an openlumara estimation
            usage_tokens = await self._context_token_count()
            if usage_tokens:
                yield {"type": "token_usage", "content": int(usage_tokens), "source": "API"}

    def _reset_turn_state(self):
        """clear the per-turn tool card / message buffers (a turn can span
        several assistant messages: reasoning, calls, results, answer)"""
        self._turn_calls = {}
        self._turn_calls_by_id = {}
        self._turn_text = []
        self._turn_reasoning = []

    async def _persist_assistant(self, channel, message: dict):
        """store one assistant message in the chat exactly the way the default
        backend's toolcall chain does, so reloaded history renders the same"""
        if channel is None:
            return
        try:
            await channel.context.chat.messages.add(dict(message))
        except Exception as e:
            self.module.log("pi", f"could not store pi's assistant message: {e}")

    def _tool_call_delta(self, event: dict):
        """pi's toolcall_start / toolcall_delta -> an OpenLumara tool_call_delta
        token with the call's accumulated-so-far arguments (the default
        backend streams the growing buffer the same way)"""
        delta = event.get("assistantMessageEvent") or {}
        index = delta.get("contentIndex")
        if index is None:
            return None

        slot = self._turn_calls.get(index)
        if slot is None:
            slot = _CallSlot(index)
            self._turn_calls[index] = slot

        if delta.get("type") == "toolcall_start":
            slot.id = delta.get("id") or f"pi_{uuid.uuid4().hex[:24]}"
            slot.name = delta.get("toolName") or ""
        elif slot.id is None:
            # toolcall_delta without a start (stream joined late): invent an
            # id so the card, its response and the merged history still line up
            slot.id = f"pi_{uuid.uuid4().hex[:24]}"

        if delta.get("type") == "toolcall_delta":
            slot.args += delta.get("delta") or ""

        if slot.id:
            self._turn_calls_by_id.setdefault(slot.id, slot)

        return {
            "type": "tool_call_delta",
            "tool_calls": [_tool_card(slot.id, slot.name, slot.args)],
        }

    def _assistant_message_blocks(self, message: dict):
        """
        pi's authoritative message_end for one assistant response: store the
        message (content + reasoning, and only those - the default backend's
        chain stores the same split) and emit one tool_calls token with its
        finalized calls, mirroring core/api.py's end-of-stream behavior.
        """
        tokens = []

        content = ""
        reasoning = ""
        calls = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                content += block.get("text") or ""
            elif block.get("type") == "thinking":
                reasoning += block.get("thinking") or ""
            elif block.get("type") == "toolCall":
                call_id = block.get("id") or f"pi_{uuid.uuid4().hex[:24]}"
                args_json = json.dumps(block.get("arguments") or {}, ensure_ascii=False)
                calls.append((call_id, block.get("name") or "tool", args_json))
                slot = self._turn_calls.get(block.get("contentIndex")) or self._turn_calls_by_id.get(call_id)
                if slot is None:
                    slot = _CallSlot(len(self._turn_calls))
                    self._turn_calls[slot.index] = slot
                slot.id, slot.name, slot.args = call_id, block.get("name"), args_json
                self._turn_calls_by_id[call_id] = slot

        finalized = []
        for call_id, name, args_json in calls:
            slot = self._turn_calls_by_id.get(call_id)
            if slot is not None:
                slot.args = args_json
            finalized.append(_tool_card(call_id, name, args_json))

        if content or reasoning or finalized:
            # one assistant message, exactly the shape the default chain
            # stores: text + reasoning, plus the tool calls it finalized
            tokens.insert(0, ("persist", content, reasoning, finalized))
            self._turn_text = []
            self._turn_reasoning = []

        if finalized:
            tokens.append(("calls", finalized))

        return tokens

    def _tool_execution_end_tokens(self, event: dict):
        """pi finished one tool call: emit the matching OpenLumara tool token
        (the core TurnCollector folds it into the card with the same id);
        synthesize a card first if the model's message never streamed here
        (aborted message, nested call id, reconnect mid-turn), so the
        response can never dangle ownerless"""
        call_id = event.get("toolCallId")
        if not call_id:
            return []

        response = _tool_response_payload(event.get("result") or {}, bool(event.get("isError")))

        slot = self._turn_calls_by_id.get(call_id)
        if slot is None:
            slot = _CallSlot(len(self._turn_calls))
            slot.id = call_id
            slot.name = event.get("toolName")
            slot.args = json.dumps(event.get("args") or {}, ensure_ascii=False)
            self._turn_calls[slot.index] = slot
            self._turn_calls_by_id[call_id] = slot

        if slot.responded:
            return []
        slot.responded = True

        tokens = []
        if not slot.card_emitted:
            slot.card_emitted = True
            tokens.append({"type": "tool_calls", "tool_calls": [_tool_card(slot.id, slot.name, slot.args)]})
        tokens.append({"type": "tool", "tool_call_id": call_id, "content": response})
        return tokens

    async def _translate_events(self, channel=None, client=None):
        """consume pi events until the agent settles, yielding stream tokens"""
        stuck_since = time.monotonic()
        self._reset_turn_state()

        # pin the client (send_stream passes the one it prompted; direct
        # callers fall back to the current one) and refuse to run at all
        # without one, instead of dying on None.next_event later
        client = client if client is not None else self.pi
        if client is None:
            yield {"type": "error", "content": "the pi process is not running"}
            return

        while True:
            try:
                event = await client.next_event(timeout=IDLE_EVENT_TIMEOUT)
            except asyncio.TimeoutError:
                if not client.alive:
                    yield {"type": "error", "content": "the pi process went away mid-turn"}
                    return

                # absolutely silent for another full window (and not just a
                # long-running tool streaming updates under the hood): give up
                if time.monotonic() - stuck_since > IDLE_EVENT_TIMEOUT:
                    yield {"type": "error", "content": "pi went silent; the turn was abandoned. Check /pi status."}
                    return
                continue

            stuck_since = time.monotonic()
            etype = event.get("type")

            if etype == "__dead__":
                yield {"type": "error", "content": event.get("reason") or "the pi process exited"}
                return

            elif etype == "message_update":
                delta = event.get("assistantMessageEvent") or {}
                dtype = delta.get("type")

                if dtype == "text_delta":
                    text = delta.get("delta")
                    if text:
                        self._turn_text.append(text)
                        yield {"type": "content", "content": text}
                elif dtype == "thinking_delta":
                    text = delta.get("delta")
                    if text:
                        self._turn_reasoning.append(text)
                        yield {"type": "reasoning", "content": text}
                elif dtype in ("toolcall_start", "toolcall_delta"):
                    token = self._tool_call_delta(event)
                    if token is not None:
                        yield token

            elif etype == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "assistant" and message.get("stopReason") == "error":
                    note = message.get("errorMessage") or "pi reported an error"
                    yield self._note(f"pi error: {note}")
                elif message.get("role") == "assistant":
                    # authoritative message: persist it (text, reasoning and
                    # its tool calls in one message, like the default chain)
                    # and emit one tool_calls token for the stream
                    for item in self._assistant_message_blocks(message):
                        if item[0] == "persist":
                            message_to_store = {"role": "assistant", "content": item[1]}
                            if item[2]:
                                message_to_store["reasoning_content"] = item[2]
                            if item[3]:
                                message_to_store["tool_calls"] = item[3]
                            if message_to_store["content"] or item[2] or item[3]:
                                await self._persist_assistant(channel, message_to_store)
                        else:
                            for call in item[1]:
                                slot = self._turn_calls_by_id.get(call.get("id"))
                                if slot is not None:
                                    slot.card_emitted = True
                            yield {"type": "tool_calls", "tool_calls": item[1]}

            elif etype == "tool_execution_start":
                note = self._activity_note(event)
                if note is not None:
                    yield note

            elif etype == "tool_execution_end":
                result = event.get("result") or {}
                excerpt = _excerpt(_result_text(result), self.module.tool_output_limit())
                failed = bool(event.get("isError"))

                note = self._activity_note(event, result_excerpt=excerpt, failed=failed)
                if note is not None:
                    yield note

                for token in self._tool_execution_end_tokens(event):
                    yield token

            elif etype == "compaction_start":
                yield self._note(f"Compacting pi context ({event.get('reason', '?')})..")

            elif etype == "compaction_end":
                result = event.get("result") or {}
                before = result.get("tokensBefore")
                after = result.get("estimatedTokensAfter")
                if result.get("summary"):
                    yield self._note(f"pi context compacted: {before} -> ~{after} tokens")
                await self.refresh_stats()

            elif etype == "auto_retry_start":
                yield self._note(f"pi hit a transient error, retrying: {event.get('error') or 'unknown error'}")

            elif etype == "extension_error":
                yield self._note(f"pi extension error: {event.get('error') or 'unknown error'}")

            elif etype == "extension_ui_request":
                line = self._extension_ui_note(event, event.get("method") in ("select", "confirm", "input", "editor"))
                if line:
                    yield self._note(line)

            elif etype == "agent_settled":
                await self._turn_settled()
                return

    async def _turn_settled(self):
        """post-turn bookkeeping: cache pi's session stats"""
        await self.refresh_stats()

    async def _context_token_count(self):
        """the number this turn reports as its token usage: pi's own reading of
        how full its context window is (None when pi has not measured yet,
        which leaves the channel on its estimation path, as with an API that
        reports no usage)"""
        return (self._measured_context() or (None, None))[0] or None

    # ------------------------- activity notes -------------------------

    def _note(self, text: str):
        """a chain note: reasoning text that renders inside the processing chain
        (pi's own thinking streams the same way) and lands in the message's
        reasoning_content, so chat history shows it after a reload too"""
        return {"type": "reasoning", "content": f"\n\npi: {text}\n"}

    def _activity_note(self, event: dict, result_excerpt: str = None, failed: bool = False):
        """render one pi tool execution as a chain note (toggleable). the
        native tool cards (streamed and persisted exactly like the default
        backend's) always show the full call and result; this only adds an
        extra textual activity line inside the processing chain"""
        if not self.module.config.get("show_tool_activity", default=False):
            return None

        tool = event.get("toolName") or "tool"
        if result_excerpt is None:
            return self._note(f"running {tool}: {_compact_json(event.get('args'))}")

        status = "failed" if failed else "done"
        if result_excerpt:
            return self._note(f"{tool} {status}: {result_excerpt[:NOTE_CHAR_CAP]}")
        return self._note(f"{tool} {status}")

    # ------------------------- non-streaming send -------------------------

    async def send(self, context: list, system_prompt=True, use_tools=True, tools=None, use_thinking=True, **kwargs):
        """
        non-streaming path (cli_lite, telegram in non-edit mode, the scheduler,
        api_bridge): run the same turn and return the final assistant message.

        prompt source: an interactive turn (a user message just passed through
        module.on_user_message) is taken from the chat store like the
        streaming path; a direct backend call like a scheduler job takes its
        instruction from the passed context instead.
        """
        interactive = self._turn_is_interactive()

        content = []
        reasoning = []
        error = None

        source = "chat" if interactive else "context"
        async for token in self.send_stream(context, _prompt_source=source):
            ttype = token.get("type")
            if ttype == "content":
                content.append(token.get("content") or "")
            elif ttype == "reasoning":
                reasoning.append(token.get("content") or "")
            elif ttype == "error":
                error = token.get("content")
            # tool_call_delta / tool_calls / tool tokens are display-only
            # cards (the default backend's chain would have executed them;
            # pi already did) - text channels that format streams handle
            # them the same way they handle the default backend's

        if error and not "".join(content).strip():
            return core.api.APIError(error)

        message = {"role": "assistant", "content": "".join(content)}
        if "".join(reasoning).strip():
            message["reasoning_content"] = "".join(reasoning)
        return message

    # ------------------------- cancellation -------------------------

    async def cancel(self):
        """the stop button: abort the running pi turn"""
        if self.pi is None or not self.pi.alive or not self._stream_active:
            self.cancel_request = False
            return False

        self.cancel_request = True
        try:
            await self.pi.abort()
        except PiRPCError as e:
            self.manager.log("pi", f"abort failed: {e}")

        waited = 0.0
        while self._stream_active and waited < ABORT_SETTLE_TIMEOUT:
            await asyncio.sleep(0.05)
            waited += 0.05

        self.cancel_request = False
        return True

    def _abandon_turn(self):
        """fire-and-forget: abort pi and swallow its events until it settles,
        so the next prompt starts from a clean state even though this
        generator is being torn down"""

        async def _drain():
            try:
                if self.pi is not None and self.pi.alive:
                    try:
                        await self.pi.abort()
                    except PiRPCError:
                        pass

                    deadline = time.monotonic() + ABORT_SETTLE_TIMEOUT
                    while time.monotonic() < deadline:
                        try:
                            event = await self.pi.next_event(timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if event.get("type") in ("agent_settled", "__dead__"):
                            break
            except Exception:
                pass
            finally:
                self.is_streaming = False
                self._stream_active = False
                self.cancel_request = False

        try:
            asyncio.create_task(_drain())
        except RuntimeError:
            pass # no running loop: nothing else we can do

    # ------------------------- prompt extraction -------------------------

    async def _extract_prompt(self, context: list, channel, prefer: str = "chat"):
        """
        find the user message this turn is about, as (text, images, notes).

        the chat store holds the exact message the channel just added, before
        end-prompt / message-inject decoration touched the built context, so
        it is the source of truth for interactive turns (and it preserves
        multimodal blocks). the passed context is used instead for direct
        backend calls that never touch a chat (scheduler jobs), and as a
        general fallback.

        attachments are resolved here, against the same forward_images switch
        the whole module honors, so what pi is shown and what the notes
        describe can never disagree.
        """
        forward_images = bool(self.module.config.get("forward_images", default=True))

        def extract(message):
            return _extract_message(message, forward_images=forward_images)

        chat_message = None
        if channel is not None and prefer == "chat":
            try:
                chat_message = _last_user_message(await channel.context.chat.messages.get())
            except Exception:
                pass

        ctx_message = _last_user_message(context)

        # safety net: a compression request racing the module (re)load still
        # arrives here as a synthesized summarization prompt; give pi the
        # whole flattened conversation instead of just the prompt
        if ctx_message is not None and _is_compression_message(ctx_message):
            return self._flatten_context(context), [], []

        if chat_message is not None:
            return extract(chat_message)

        if ctx_message is not None:
            return extract(ctx_message)

        # last resort: e.g. a scheduler instruction with role system
        for message in reversed(context or []):
            if isinstance(message, dict) and message.get("role") in ("user", "system", "developer"):
                return extract(message)

        return "", [], []

    def _flatten_context(self, context: list) -> str:
        lines = []
        for message in context or []:
            if not isinstance(message, dict):
                continue
            text, _ = _text_and_images(message)
            if text.strip():
                lines.append(f"[{message.get('role', '?')}]\n{text}")
        return "\n\n".join(lines)

    # ------------------------- pi session sync -------------------------

    def _current_chat_key(self, channel=None) -> str:
        channel = channel or getattr(self.manager, "channel", None)
        chat_id = None
        if channel is not None:
            try:
                chat_id = channel.context.chat.get("id")
            except Exception:
                chat_id = None
        if not self.module.config.get("session_per_chat", default=True):
            return "__shared__"
        return chat_id or "default"

    async def _sync_session(self, channel):
        """bind pi's session to the openlumara chat that is about to speak"""
        chat_key = self._current_chat_key(channel)
        if self._bound_chat == chat_key:
            return

        mapping = self.module.session_map
        known = mapping.get(chat_key)

        if known:
            try:
                data = await self.pi.switch_session(known)
                if data is None or not data.get("cancelled"):
                    state = await self.pi.get_state()
                    if (state or {}).get("sessionFile") == known:
                        self._bound_chat = chat_key
                        return
            except PiRPCError:
                pass # session file gone (deleted / moved / other cwd): start fresh

        await self.pi.new_session()
        state = await self.pi.get_state()
        session_file = (state or {}).get("sessionFile")

        if session_file:
            mapping[chat_key] = session_file
            mapping.save()

        self._bound_chat = chat_key

        # name the pi session after the openlumara chat title, best effort
        title = channel.context.chat.get("title")
        if title:
            try:
                await self.pi.set_session_name(str(title)[:80])
            except PiRPCError:
                pass

    # ------------------------- chat history sync (edit / delete / regenerate) -------------------------

    def clear_history_marks(self):
        """forget the chat-store watermarks: every chat is considered
        unsynced and gets resynced in front of pi on its next turn (used
        when the pi process is replaced - the new process never saw the
        sessions the old one had fed)"""
        self._history_marks = {}
        self._pending_recap = None

    async def _mark_history_synced(self, channel):
        """stamp the chat store's shape as what pi is sitting on. exactly one
        shape per chat, not a history of them: pi holds one session at one
        point of the conversation, so anything that no longer matches that
        shape is out of sync - including a regenerate rewinding the store to
        a shape this same session passed through earlier, because pi has long
        since moved past it"""
        messages = await channel.context.chat.messages.get()
        mark = self._history_marks.setdefault(self._current_chat_key(channel), {})
        mark["count"] = len(messages or [])
        mark["last"] = _history_fingerprint(messages)

    async def _history_diverged(self, channel) -> bool:
        """
        has the chat store changed in a way pi cannot know about? the
        watermark is stamped whenever pi is brought in line with the store
        (a rebuild) and once this turn's messages are persisted, so anything
        that does not match it arrived out of band: a message edit, a
        delete-from-index, or a regenerate - webui routes that only touch
        OpenLumara's store, never pi.
        """
        mark = self._history_marks.get(self._current_chat_key(channel))
        if not mark or not mark.get("last"):
            return False # first turn of this chat (or no watermark): nothing to resync
        messages = list(await channel.context.chat.messages.get() or [])
        if _history_fingerprint(messages) == mark["last"]:
            return False

        # this turn's own prompt is not an out-of-band change: the channel
        # appends the user message right before prompting, so judge the
        # history the store holds WITHOUT it - otherwise every ordinary turn
        # would look like an edit and rebuild the session. a regenerate still
        # shows up: its rewind leaves a shorter conversation than the one pi
        # is sitting on, not the shape pi was last brought in line with
        if messages and (messages[-1] or {}).get("role") == "user":
            if _history_fingerprint(messages[:-1]) == mark["last"]:
                return False

        return True

    def _render_chat_history(self, messages) -> str:
        return _render_chat_history(messages)

    async def _rebuild_if_history_diverged(self, channel):
        """
        rebuild pi's session from the chat store after the history was
        edited / deleted from / regenerated.

        the default backend assembles model context from the chat store on
        every request, so those operations need no special handling there.
        pi's context is its session, so the module does the equivalent:
        a fresh pi session plus one prompt carrying the recap of what the
        store still shows (and, when the store still ends on a user
        message - the regenerate / edit shape - that message as the prompt
        itself). pi then works from exactly the conversation the user
        sees, and compaction afterwards applies to the rebuilt context
        just like it would on the default backend.
        """
        if not (self.pi and self.pi.alive):
            return False
        if not await self._history_diverged(channel):
            return False

        messages = list(await channel.context.chat.messages.get() or [])

        # a trailing user message is the prompt of the turn being retried,
        # not history - send_stream prompts it normally
        trailing_prompt = None
        if messages and messages[-1].get("role") == "user":
            trailing_prompt = messages.pop()

        history = self._render_chat_history(messages)
        # the rebuilt session starts empty, so the recap has to ride along in
        # front of the prompt this turn sends (send_stream picks it up below).
        # it cannot be prompted on its own: that would make pi ANSWER the
        # recap, spend a turn on it and leak its text into the stream. the
        # trailing user message stays out of the recap - it is this turn's
        # prompt, and its images go out with the prompt like any upload
        self._pending_recap = None
        if history:
            self._pending_recap = (
                "[CONTEXT REPLAY - OpenLumara's chat history was edited, "
                "rewound or regenerated and this session was rebuilt from "
                "it. Continue from here; do not reply to this replay.]\n\n"
                f"{history}\n\n[END REPLAY]"
            )

        await self.pi.new_session()
        state = await self.pi.get_state()
        session_file = (state or {}).get("sessionFile")
        if session_file:
            self.module.session_map[self._current_chat_key(channel)] = session_file
            self.module.session_map.save()

        title = None
        try:
            title = channel.context.chat.get("title")
        except Exception:
            pass
        if title:
            try:
                await self.pi.set_session_name(str(title)[:80])
            except PiRPCError:
                pass

        await self.refresh_stats()
        # the store as it stands (minus the trailing prompt) is now in front
        # of pi; send_stream re-stamps after the prompt actually goes out
        await self._mark_history_synced(channel)
        self.module.log("pi", "resynced pi session with the edited chat history")
        return True

    # ------------------------- pi control actions (commands) -------------------------

    async def pi_compact(self, notify=True):
        """run pi's own compaction on the current session.
        notify=False is the automatic path: the default backend also
        compacts silently when the threshold trigger fires it"""
        if self.pi is None or not self.pi.alive:
            return None
        result = await self.pi.compact()
        await self.refresh_stats()

        if notify and result:
            before = result.get("tokensBefore")
            after = result.get("estimatedTokensAfter")
            channel = getattr(self.manager, "channel", None)
            if channel is not None:
                try:
                    await channel.push(f"pi session compacted: {before} -> ~{after} tokens")
                except Exception:
                    pass
        return result

    async def pi_new_session(self):
        """start a fresh pi session for the current chat (and forget the old mapping)"""
        chat_key = self._current_chat_key()

        data = await self.pi.new_session()
        state = await self.pi.get_state()
        session_file = (state or {}).get("sessionFile")

        mapping = self.module.session_map
        if session_file:
            mapping[chat_key] = session_file
        else:
            mapping.pop(chat_key, None)
        mapping.save()

        # pi now sits at an empty session while the chat store still shows
        # the whole conversation; stamping an unsynced mark would be a lie
        # either way, so drop it and let the next turn decide normally
        self._history_marks.pop(chat_key, None)

        self._bound_chat = chat_key
        await self.refresh_stats()
        return data

    async def pi_steer(self, message: str, mode: str = "steer"):
        """queue a steering / follow-up message on the running pi turn"""
        if self.pi is None or not self.pi.alive:
            raise PiRPCError("pi is not running")
        if mode == "follow_up":
            return await self.pi.follow_up(message)
        return await self.pi.steer(message)

    async def pi_thinking_levels(self):
        if self.pi is None or not self.pi.alive:
            return ["off"]
        try:
            data = await self.pi.get_available_thinking_levels()
        except PiRPCError:
            return ["off"]
        return (data or {}).get("levels") or ["off"]

    async def pi_set_thinking_level(self, level: str):
        if self.pi is None or not self.pi.alive:
            return False
        try:
            await self.pi.set_thinking_level(level)
        except PiRPCError:
            return False
        self._thinking_level = level
        return True

    # ------------------------- status snapshot -------------------------

    def status_snapshot(self) -> dict:
        """everything /pi status shows"""
        state = {}
        stats = self.stats or {}

        usage = stats.get("contextUsage") or {}
        state["context"] = {
            "tokens": usage.get("tokens"),
            "window": usage.get("contextWindow"),
            "percent": usage.get("percent"),
        }
        state["session"] = {
            "file": stats.get("sessionFile"),
            "id": stats.get("sessionId"),
            "bound_chat": self._bound_chat,
        }
        state["tokens"] = stats.get("tokens") or {}
        state["cost"] = stats.get("cost")
        state["messages"] = {
            "user": stats.get("userMessages"),
            "assistant": stats.get("assistantMessages"),
            "tool_calls": stats.get("toolCalls"),
        }
        state["model"] = self._model_id
        state["thinking_level"] = self._thinking_level
        state["alive"] = bool(self.pi and self.pi.alive)
        state["busy"] = self._stream_active
        state["cwd"] = self.module.resolved_cwd()
        base_url, _, model = self.module.resolved_provider_settings()
        state["provider"] = {
            "injected": bool(base_url),
            "id": self.module.PROVIDER_ID if base_url else None,
            "url": base_url,
            "model": model,
        }
        return state
