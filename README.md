# pi — drive the pi coding agent from OpenLumara

A user module for [OpenLumara](https://github.com/Rose22/openlumara) that replaces OpenLumara's AI
agent backend with the [pi coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(`pi`, the coding agent CLI with read/bash/edit/write tools and session management).

When the module is enabled, OpenLumara does **not** run its own AI agent loop. Instead it connects
OpenLumara's UI to a pi instance programmatically through pi's SDK RPC protocol
(`pi --mode rpc`, JSONL over stdin/stdout) and translates every UI interaction into pi commands and
every pi event back into OpenLumara stream tokens. All OpenLumara UI features keep working — they
just control or display the pi backend now.

## What you get

| OpenLumara feature | What it does with this module enabled |
|---|---|
| Chat send / streaming (webui, cli, telegram, discord, matrix, ...) | Sends a `prompt` command to pi and renders pi's live event stream: thinking → the processing chain's thinking cards, answer text → the answer bubble |
| Processing chain / tool cards | Every tool call pi makes — its own built-ins (read, bash, edit, write) *and* OpenLumara tools called through the tool bridge — streams as OpenLumara's **native tool cards**, exactly like the default AI backend's: streamed arguments (`tool_call_delta`), finalized calls (`tool_calls`), and results (`tool`) folded into the card, plus the same `calling_tools` / `processing_tools` stream states — `show_tool_activity` optionally adds plain activity lines on top |
| Stop button / `/stop` | Sends pi's `abort` command; the partial answer is kept in history |
| Model switcher (webui header, `/model`, settings) | Lists pi's configured models and switches pi's active model (`set_model` RPC) |
| Context pill / `token_usage` | pi's real context-window fill from `get_session_stats` — the pill, the popup's breakdown (pi's own standing prompt, its tool schemas, the conversation, compaction count) and `/status` all read pi's session; `Context.get_total_tokens()`/`get_size()` are patched for that and restored on unload |
| Cost / session totals | pi's cumulative tokens and cost from `get_session_stats`, reported by `/pi status` |
| `/status` | Reports pi's process, model, session and context stats |
| `/compress` | Runs pi's own session compaction instead of an OpenAI-style summarization pass; automatic compression fires on the same triggers and honors the same `model.automatically_compress_context` / `context_compression_threshold` settings, measuring pi's real session fill |
| Chat switching / new chats | Each OpenLumara chat is mapped to its own pi session file (persisted in `data/pi_sessions.json`); switching chats switches pi sessions, new chats get fresh pi sessions |
| Message edit / delete / regenerate | Work exactly as with the default backend: the module notices the chat store no longer matches what pi last saw, rebuilds pi's session and replays the edited history, so pi always works from the conversation you see |
| Scheduler jobs | Prompts pi directly (the job's instruction text is used) |
| Message file uploads | Same as the default backend: images are delivered to the model (forwarded to pi as image attachments, and the injected model is flagged image-capable so pi doesn't drop them), plain files and PDFs arrive as text through OpenLumara's normal pipeline, and anything pi cannot carry (audio) is named in the prompt instead of vanishing |
| `/pi` command | Backend status, model list/switch, thinking level, new session, abort, compact, restart, steer, follow-up — the module's only control surface (see "Controls") |

## Requirements

- OpenLumara with the webui (or any other channel) — the module itself needs no Python dependencies
- **pi** installed and authenticated:

  ```bash
  npm install -g @earendil-works/pi-coding-agent
  ```

  Make sure `pi` works standalone in the project folder you want to point OpenLumara at
  (provider/API keys are configured by pi itself, e.g. via `pi` interactive login or environment
  variables — this module reuses whatever pi already has configured).

If the pi binary cannot be found, the module stays passive: it logs a hint and OpenLumara keeps
using its own backend. Nothing breaks.

## Setup

1. Put this folder into your OpenLumara `user_modules/` folder.
2. Restart OpenLumara and enable the module: `/module pi` (or the settings panel, or config).
3. Set `working_dir` to the project folder pi should operate in (defaults to OpenLumara's folder).
4. Chat normally — your messages now drive pi.

## Settings

| Setting | Default | Description |
|---|---|---|
| `pi_path` | `pi` | Path to the pi binary. Absolute, `~` and relative paths are used directly; bare names are looked up on `PATH`, then `~/.pi/agent/bin`, `~/.pi/bin`, `~/.local/bin` and friends |
| `working_dir` | `.` | The project folder pi runs in (AGENTS.md discovery, file edits, shell commands). `~` and relative paths are expanded |
| `pi_agent_dir` | `""` | Override pi's own configuration folder (`PI_CODING_AGENT_DIR`); empty = pi's standard `~/.pi/agent`. See "Your existing pi setup" |
| `project_trust` | `auto` | How pi treats the working dir's `.pi/` resources (project settings, project extensions/packages): `auto` = only previously saved decisions load, `approve` = `--approve` at every start, `ignore` = `--no-approve` |
| `extra_args` | `[]` | Extra CLI args for `pi --mode rpc`, one per entry (e.g. `--offline`, `--no-session`, `--thinking high`) |
| `auto_start` | `true` | Spawn pi at module load instead of on first message |
| `session_per_chat` | `true` | One pi session per OpenLumara chat; `false` shares a single pi session across chats |
| `show_tool_activity` | `false` | Additionally render pi tool executions as plain text lines inside the processing chain (native tool cards are always shown) |
| `tool_output_chars` | `400` | Max tool output characters per chain activity line |
| `forward_images` | `true` | Forward uploaded/pasted images to pi as image attachments (also flags the injected models as image-capable — with it off, pi would drop images before they reach the endpoint, so the prompt then says an image was attached but not forwarded) |
| `expose_tools` | `true` | Expose OpenLumara's own tools to pi (see "Tool bridge"); pi's built-in tools always work regardless |
| `use_openlumara_settings` | `true` | Configure pi with OpenLumara's API settings (url + key) and model settings (model name); the manual fields below are hidden and unused while on |
| `api_url` | `""` | (manual mode) API base URL pi is configured with |
| `api_key` | `""` | (manual mode) API key; empty = dummy key for auth-free local servers |
| `model` | `""` | (manual mode) model pi starts with; bare id or `provider/id` pattern |

### API & model settings (provider injection)

By default the module configures pi with **OpenLumara's own settings**: the endpoint from
OpenLumara's API settings (`api.url` + `api.key`) is registered inside pi as an
`openlumara` provider (any OpenAI-compatible endpoint), and pi starts on the model from
OpenLumara's model settings (`model.name`). The endpoint's `/models` list is mirrored into pi's
model switcher on every spawn, so `/pi models` and `/pi model <name>` work against
your server. OpenLumara's `api.max_context` / `api.max_output_tokens` / `model.enable_thinking`
are carried over as the injected models' window, output limit and thinking support.

With the switch on, the three manual fields below are **hidden in the settings UI and not
consulted at all** - pi purely follows OpenLumara. Turning `use_openlumara_settings` off reveals
them and makes pi run by those instead: `api_url` + `api_key` for any OpenAI-compatible endpoint
and `model` for the start model (a bare id on that endpoint, or a `provider/id` pattern for one
of pi's own providers). Leaving them all empty injects nothing and leaves pi untouched on its own
providers and auth (e.g. pi's own `/login`).

**Local models without an API key** work out of the box. pi refuses to call a provider whose key
is empty (even for auth-free servers), so when no key is configured anywhere the module gives pi
a harmless dummy key (`openlumara`) — the same pattern pi's own docs prescribe for Ollama and
friends. Local servers ignore the `Authorization` header entirely. If your endpoint does require
auth, set `api_key` (or OpenLumara's `api.key`) and it is used verbatim.

Mechanics: on every spawn the module writes `data/pi_provider.json` (endpoint + model list, no
secrets) and launches pi with `--extension pi_provider_extension.js`; that extension registers
the provider and pi resolves the key from the `PI_OPENLUMARA_API_KEY` environment variable of
the child process. Nothing is ever written into `~/.pi`.

**Settings are applied automatically**: a small watchdog compares the pi-relevant settings
(this module's `pi_path` / `working_dir` / `extra_args` / API & model settings, plus
OpenLumara's `api.url` / `api.key` / `model.name` / context limits) every few seconds; when a
save changes them, the running pi is respawned with the fresh configuration as soon as it is
idle. No manual `/pi restart` needed. (Toggling the module itself enabled/disabled is a core
OpenLumara action that only takes effect on an OpenLumara restart or `/restart` — after which
the module configures pi on load.)

### Your existing pi setup: config, plugins and packages

pi *is* the configuration store — the module deliberately adds no middle layer. pi is spawned as
an ordinary `pi --mode rpc` child inheriting OpenLumara's environment, so everything pi normally
reads is read here too:

- **`~/.pi/agent/settings.json`** — default tools, compaction, resource paths, pi package
  declarations (anything added with `pi install`), everything else pi does.
- **`~/.pi/agent/auth.json` / `models.json`** — pi's own provider logins and model catalog. This
  is also how `use_openlumara_settings = false` with empty `api_url` / `model` runs OpenLumara on
  **pi's own providers**: no injection happens at all and pi starts exactly as it would in your
  terminal.
- **`~/.pi/agent/extensions/`, `skills/`, `prompts/`, `themes/`** — user extensions/plugins,
  skills and prompt templates (they surface as pi slash commands, which `/pi`-driven sessions
  can use), installed pi packages (npm / git / local) loading through settings.json.
- **Context files** (`AGENTS.md`, `CLAUDE.md`, `.pi/SYSTEM.md`) — picked up from `working_dir`
  just as they are for terminal pi.

Two settings cover the corners pi cannot decide on its own:

- **`pi_agent_dir`** moves the whole config folder (it sets `PI_CODING_AGENT_DIR`) — point
  OpenLumara at a different pi profile or installation with one field.
- **`project_trust`** decides about the working directory's own `.pi/` folder (project settings,
  project extensions, project packages). RPC mode cannot show pi's trust prompt, and with `auto`
  pi honors only a previously saved decision — e.g. from running `pi` interactively once in that
  folder and choosing `/trust`. `approve` trusts it on every start (`--approve`), `ignore` never
  loads it (`--no-approve`).

What a plugin feels: extensions run inside the pi child, so `pi.registerTool`, tool-call gates,
registered providers and session hooks all work — except the terminal-only half of `ctx.ui`, and
the dialog half (`select` / `confirm` / `input` / `editor`) degraded deliberately: those requests
reach OpenLumara, which has no dialog widget, and the rpc client answers *cancelled* the moment
one arrives. A confirm-gating plugin therefore **blocks** the operation it guards (exactly what
happens when you press escape at a terminal) instead of hanging the turn or silently approving —
and every such decision, plus every `ctx.ui.notify` message, shows up in OpenLumara's log and as
an activity line in the processing chain, so nothing is declined without a trace.

## How it works

OpenLumara channels never talk to an LLM directly — everything goes through `manager.API`
(`core/api.py`: `send`, `send_stream`, `cancel`, `connect`, `list_models`, `set_model`,
`get_status`, ...). On `on_ready`, the module:

1. spawns `pi --mode rpc` as an asyncio subprocess (`pi_rpc.py` — a strict-JSONL RPC client with
   id-correlated responses and an event queue),
2. swaps `manager.API` for `PiBackend` (`backend.py`), which implements the same client surface on
   top of pi RPC commands, and
3. patches four `core.context.Context` methods while active: the auto-compression trigger measures
   pi's real session fill against the same OpenLumara settings the default backend uses and
   routes the compression itself to pi's `compact` command (silently when automatic, exactly
   like the default backend's automatic pass), and `get_total_tokens()` / `get_size()` report
   pi's session instead of OpenLumara's estimators, so the context pill, the context popup and
   `/status` stay accurate (falling back to the originals whenever pi has no measurement), and
4. watermarks the chat store after every turn, so a message edit / delete / regenerate (webui
   routes that only touch OpenLumara's store) is detected on the next turn and pi's session is
   rebuilt from the store before prompting.

On `on_shutdown` / module reload, `manager.API` and the patched methods are restored and the pi
process is terminated.

Prompt extraction: interactive turns read the message from the active chat's store (the exact
message the channel just added, including multimodal blocks, before end-prompt / message-inject
decoration). Direct backend calls that never touch a chat (the scheduler) take their instruction
from the passed context instead.

Session mapping: before each turn the backend makes sure pi's session matches the active
OpenLumara chat (`switch_session` / `new_session`), keyed by chat id in `data/pi_sessions.json`.
Pi session files survive restarts, so chats resume their pi conversation history across OpenLumara
restarts (and across module disable/enable).

### Controls: no panel, just `/pi`

The module ships **no webui panel, no sidebar tab and no channel-specific UI of its own** — pi's
work already shows up through the normal OpenLumara path in whatever channel you are using:
tool calls as native tool cards (with streamed arguments and live results), thinking and activity
lines in the processing chain, context fill in the context pill, cost and totals in `/status`.
Configuration lives entirely in **OpenLumara's settings menu** (the table above); the runtime
controls are `/pi` subcommands:

| Command | What it does |
| --- | --- |
| `/pi` (`status`) | process, endpoint, model, thinking level, bridged tools, session, context and cost |
| `/pi models` · `/pi model <name>` | list pi's models · switch the active one |
| `/pi thinking <level>` | set pi's thinking level (`/pi thinking` lists the levels pi offers) |
| `/pi new` · `/pi restart` | fresh pi session for this chat · restart the pi process |
| `/pi abort` | abort pi's current run (partial answer stays in history) |
| `/pi compact` | run pi's own context compaction |
| `/pi steer <text>` | steer the turn pi is running right now |
| `/pi follow_up <text>` | queue text for pi to act on after its current turn |

## Tool bridge: OpenLumara tools inside pi

While pi owns the model loop, you usually still want *your* tools available: memory, calendar,
scheduler, file manager, the host shell, other modules — everything OpenLumara's own backend can
call. The `expose_tools` setting (on by default) does exactly that:

1. The module runs a **loopback tool bridge**: an HTTP server bound to `127.0.0.1` on an ephemeral
   port, protected by a per-spawn bearer token that is passed only through the pi child's
   environment (`PI_OPENLUMARA_BRIDGE_URL` / `PI_OPENLUMARA_BRIDGE_TOKEN`, never written to disk).
2. `pi_tool_bridge_extension.js` (loaded via `--extension`) fetches the active OpenLumara tool
   list — **the same array OpenLumara sends its own model** — and registers one pi tool per entry.
   That includes the dynamic-tool-loading meta tools while `model.dynamic_tool_loading` is on:
   `tools_load` and `tools_call` are bridged like any other tool and run through OpenLumara's own
   `ToolLoader`, so pi reaches the entire catalog exactly the way the default backend's model does
   (previously they were filtered out, which left pi with strictly fewer tools than the default
   backend whenever dynamic loading was enabled).
3. When the pi agent calls one of those tools, the extension posts the call to the bridge;
   OpenLumara resolves it through its own tool catalog (same catalog entry, disabled-tool list,
   loaded set, `core.tool_timeout` and result rendering as the default backend's chain) and returns
   the result, which pi feeds back into the model's context.

Routing is strict in both directions:

- **OpenLumara tools** (anything in the catalog) → intercepted and executed by OpenLumara, through
  the bridge.
- **pi's built-in tools** (`read`, `bash`, `edit`, `write`, ...) → always executed natively inside
  pi. The module's `ToolcallManager.process()` guard makes sure core never re-executes them (and
  the guard checks for the pi backend itself — `PiBackend` subclasses `APIClient`, so an
  "is an APIClient" check would wrongly route pi's own tools into OpenLumara's loader).

Because a bridged call is a perfectly normal pi tool call from pi's point of view, its argument
deltas, finalized card and result travel through pi's regular tool events — so bridged tools
render as native tool cards and persist to chat history identically to pi's built-ins and to the
default backend.

Turn the setting off and pi only ever has its own tools. pi rejects a tool whose name shadows one
of its built-ins (`read`, `bash`, `edit`, `write`) — pi's own tool wins there, since pi needs them
for its own agent loop. The extension reports every registration to the bridge, so any OpenLumara
tool pi refused is named in the log and in `/pi status` (`Tools: …
- NOT available to pi: name`) rather than being quietly missing.

With `dynamic_tool_loading` off (OpenLumara's default), the whole catalog is already in that array:
what pi is offered is byte-for-byte the tool list the default backend sends.

## Limitations & design notes

- **One pi process.** All channels share a single pi instance; the OpenLumara chat that is about
  to speak binds the session. If two channels alternate rapidly, pi switches sessions per message.
  Queueing is serialized: one pi turn at a time.
- **Message edit / delete / regenerate** apply like on the default backend, via a session
  rebuild: the default backend rebuilds model context from the chat store on *every* request, so
  those operations simply apply there; pi's context is its session instead, so the module replays
  the store (assistant answers, compact one-line tool summaries, your messages) into a fresh pi
  session on the next turn. pi therefore sees exactly what you see — but it re-reads a recap
  rather than its original session, so deep tool-result detail from before the edit is not
  re-sent (deliberate: stale tool output would be worse than no tool output). The watermark is a
  content fingerprint of the chat store, and there is exactly **one** shape per chat: what pi is
  sitting on right now. The store's own new user message is ignored when comparing (the channel
  appends it right before prompting, so every ordinary turn would otherwise look like an edit),
  which is what keeps normal turns from rebuilding — while a regenerate that rewinds to a
  conversation this session outgrew two turns ago is still detected and resynced.
- **Audio attachments cannot reach pi.** OpenLumara's multimodal pipeline turns an upload into
  OpenAI content blocks before any backend sees it: images (`image_url`), audio (`input_audio`),
  plain files and PDFs already flattened to text. Text blocks and images are delivered; pi's rpc
  accepts no audio attachment, so an audio file is announced to the agent in the prompt ("[Attached
  audio file cannot be delivered to you: voice.ogg]") rather than silently dropped — the default
  backend would hand it to its endpoint, so this is the one upload path that cannot match it.
  Binary files OpenLumara cannot decode arrive as `[Binary file: name]` on both backends.
- **Images and rebuilt sessions.** Normally the image bytes live in pi's own session, so a
  follow-up question about a photo works as it does on the default backend. If the turn is retried
  after an edit / delete / regenerate, the *current* prompt's images go out again with it, while
  images in the replayed history are summarized as `(N image(s) attached)` — re-sending stale
  pixels into a rebuilt session costs a lot of prompt for no benefit.
- **Context metrics come from pi, with honest fallbacks.** The pill, the popup and `/status` all
  funnel through `Context.get_total_tokens()` / `Context.get_size()`; while pi is the backend those
  are patched (restored on unload) to report pi's own reading of its context window, broken down
  from pi's session file — because OpenLumara's estimators would describe a context that is never
  sent, and pi's *cumulative* session totals are no substitute (they add every cached re-read of
  the context and run past the window; measured live: 21 774 cumulative vs 2 717 in a 16 768-token
  window). Where pi has no measurement to give — a session nobody has prompted yet, one compacted
  since its last assistant message, pi not running, or pi still bound to a different chat — the
  original OpenLumara estimation runs instead, which is exactly what the default backend shows
  before its first API measurement. Pi reports a model's context window to the popup; with
  `use_openlumara_settings` on (the default) that window *is* `api.max_context`, so the pill's
  percentage uses the same denominator the default backend does.
- **OpenLumara's system prompt and context management are inert** while pi is the backend — pi has
  its own prompts, AGENTS.md discovery and compaction. OpenLumara *tools*, however, are live: with
  `expose_tools` on they are bridged into pi and executed by OpenLumara itself (see "Tool
  bridge"), so modules like memory, calendar or the file manager stay usable by the agent. Only
  the *AI loop* is pi's. Concretely, while enabled the module: swaps `manager.API` for its
  pi-backed client; gives `Context.is_over_threshold()`/`compress()` pi-backed behavior (real
  session fill, the user's own compaction settings, pi's native compaction, silent on automatic
  triggers); mirrors `model.automatically_compress_context` into pi's own near-full
  auto-compaction (`set_auto_compaction`); runs the loopback tool bridge; and wraps
  `ToolcallManager.process()` so OpenLumara's tool *executor* stays idle while the pi backend is
  active (pi drives its own tool loop — running the
  calls again would double-execute; the default backend's chain passes straight through to the
  original). All of it is restored on module unload.
- **Bridged tools see pi's arguments, not OpenLumara's JSON repair.** pi validates arguments
  against the schema it was given; OpenLumara's chain normally repairs broken tool-call JSON
  itself. OpenLumara tool schemas are generated from Python signatures, so this is rarely an
  issue; a call with bad arguments simply fails inside pi.
- **Scheduler + interactive race**: a scheduler job firing within ~15 seconds of you sending a
  message could pick up your message instead of its instruction (the module can't tell the two
  apart any better than that from the API surface alone). Rare; harmless.
- pi's own permission prompts / extension UI dialogs are not surfaced in OpenLumara; configure pi
  so it can work unattended in your `working_dir` (trust/approval settings are pi's own).

## License

GPL-3.0-or-later (see `LICENSE`). pi itself is a separate project (MIT) — this module only talks to
it over its documented RPC protocol.
