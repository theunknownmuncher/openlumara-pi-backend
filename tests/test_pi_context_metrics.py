# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
the context pill, the context popup and /status must show pi's real numbers

every context metric the UI renders funnels through two Context methods:
get_total_tokens() (the pill's numerator) and get_size() (the popup's hero,
its stacked bar, the "compacted N times" line, and /status). The default
backend fills them from the API's reported prompt size plus an estimator for
what landed since. Those numbers describe the context OpenLumara assembles -
which is never sent while pi is the backend - so the module replaces both
methods with measurements taken from pi's session.
"""

import json
import time

import pytest

import core
from harness import FakeManager, apply_config

from test_pi_chat_sync import BASE, StoreChannel
from test_pi_tool_stream import load_pi_module

# captured before any test applies the module's patches
ORIGINAL_GET_TOTAL = core.context.Context.get_total_tokens
ORIGINAL_GET_SIZE = core.context.Context.get_size

SYSTEM_SECTIONS = {
    "preamble": "You are an expert coding assistant. " * 20,
    "rules": "Rules for tools. " * 40,
}

TOOLS = [
    {"name": "read", "description": "Read a file. " * 10, "parameters": {"type": "object"}},
    {"name": "bash", "description": "Run a command. " * 10, "parameters": {"type": "object"}},
]


@pytest.fixture
def pi_backend_cls():
    return load_pi_module("backend").PiBackend


@pytest.fixture
def patched_context():
    """apply the module's Context patches the way Pi.on_ready does, and
    restore the originals afterwards"""
    module_cls = load_pi_module("module").Pi
    shell = module_cls.__new__(module_cls)
    shell._patches = []
    shell._patch_context()
    yield shell._patches
    for cls, name, original in reversed(shell._patches):
        setattr(cls, name, original)


def write_pi_session(path, *, history=("first question here", "an answer", "second question"),
                     compactions=0, tools=None, sections=None):
    """a pi session file shaped like the ones pi really writes"""
    entries = [
        {"type": "session", "id": "s1", "cwd": "/tmp"},
        {"type": "message", "message": {"role": "system", "content": "",
                                        "sections": sections or SYSTEM_SECTIONS,
                                        "toolsAdded": tools if tools is not None else TOOLS}},
    ]
    for i, text in enumerate(history):
        role = "user" if i % 2 == 0 else "assistant"
        entries.append({"type": "message", "message": {
            "role": role, "content": [{"type": "text", "text": text}]}})
    for _ in range(compactions):
        entries.append({"type": "compaction", "summary": "compact handoff summary",
                        "tokensBefore": 9000, "estimatedTokensAfter": 400})
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return str(path)


# a store with more messages than the chat's token_usage_mark, so the
# original estimator takes its "api measurement + estimate the delta" path
STORE = [
    {"role": "user", "content": "first ask", "_metadata": {}},
    {"role": "assistant", "content": "first answer", "_metadata": {}},
    {"role": "user", "content": "second ask", "_metadata": {}},
    {"role": "assistant", "content": "second answer", "_metadata": {}},
]


def _backend(pi_backend_cls, tmp_path, *, tokens=4200, window=16768, session=None, bound=None):
    from test_pi_chat_sync import SyncFakeModule

    apply_config(**BASE)
    manager = FakeManager()
    module = SyncFakeModule()
    backend = pi_backend_cls(manager, module)
    channel = StoreChannel(manager, messages=list(STORE))
    channel.context.channel = channel
    manager.channel = channel
    manager.API = backend # the patches read the active backend off the manager

    backend.pi_stats_override = {
        "sessionFile": session,
        "contextUsage": {"tokens": tokens, "contextWindow": window,
                         "percent": (tokens / window) * 100 if window else 0},
        "tokens": {"input": 100, "output": 20, "cacheRead": 90, "cacheWrite": 0,
                   "total": tokens or 120},
    }
    if bound is not None:
        backend._bound_chat = bound
    return backend, channel


def _with_stats(pi_backend_cls, tmp_path, **kwargs):
    """backend whose fake pi answers get_session_stats with fixed numbers"""
    backend, channel = _backend(pi_backend_cls, tmp_path, **kwargs)

    class StatsClient:
        alive = True

        async def get_session_stats(self_inner):
            return backend.pi_stats_override

    backend.pi = StatsClient()
    return backend, channel


# --- the pill -------------------------------------------------------------

async def test_pill_shows_pis_measured_context(pi_backend_cls, patched_context, tmp_path):
    """the default backend's pill shows the API's prompt size; pi's equivalent
    is what pi reports in its own context window - including everything only
    pi knows about (its system prompt, its own tool calls and results)"""
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, window=16768,
                                   session=session)

    assert await channel.context.get_total_tokens() == 4200


async def test_pill_falls_back_before_pi_has_measured(pi_backend_cls, patched_context, tmp_path):
    """a session nobody has prompted yet reports 0; the UI then shows what the
    default backend would show before its first API measurement: an estimate.
    the cumulative session totals must not be used instead - they add every
    cached re-read of the context, so they run past the context window"""
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=0, window=16768,
                                   session=session)

    estimate = await ORIGINAL_GET_TOTAL(channel.context)
    assert estimate > 0
    assert backend.pi_stats_override["tokens"]["total"] == 120 # bigger, but not the pill's number
    assert await channel.context.get_total_tokens() == estimate


async def test_pill_falls_back_while_pi_serves_another_chat(pi_backend_cls, patched_context, tmp_path):
    """pi holds one session at a time; its numbers describe that session, so
    they must not be shown for a different chat's context pill"""
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, session=session,
                                   bound="some-other-chat")

    assert await channel.context.get_total_tokens() == await ORIGINAL_GET_TOTAL(channel.context)


async def test_pill_falls_back_when_pi_is_gone(pi_backend_cls, patched_context, tmp_path):
    backend, channel = _backend(pi_backend_cls, tmp_path)
    backend.pi = None

    assert await channel.context.get_total_tokens() == await ORIGINAL_GET_TOTAL(channel.context)


# --- the popup / /status breakdown ---------------------------------------

async def test_breakdown_describes_pis_session(pi_backend_cls, patched_context, tmp_path):
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, window=16768,
                                   session=session)

    size = await channel.context.get_size()

    # same shape, same semantics as the default backend's get_size()
    assert size["total_tokens"] == 4200
    assert size["max_context"] == 16768
    assert size["percent_full"] == round((4200 / 16768) * 100) # the default's formula
    assert size["compaction_count"] == 0
    for part in ("system_prompt", "message_history", "end_prompt"):
        assert set(size[part]) == {"tokens", "words"}
    assert set(size["tools"]) == {"active", "tokens", "words"}
    assert isinstance(size["total_words"], int)

    # pi's standing prompt and tool schemas, from pi's own session file
    expected_system = sum(len(str(v)) for v in SYSTEM_SECTIONS.values()) // 4
    assert size["system_prompt"]["tokens"] == expected_system
    assert size["tools"]["active"] == 2
    assert size["tools"]["tokens"] > 0

    # the end prompt belongs to openlumara's assembly, which pi never sees
    assert size["end_prompt"] == {"tokens": 0, "words": 0}


async def test_breakdown_never_adds_up_to_more_than_the_total(pi_backend_cls, patched_context, tmp_path):
    """the popup stacks the parts into one bar next to the hero percentage;
    estimates may not contradict the measured total"""
    session = write_pi_session(tmp_path / "session.jsonl",
                               history=("x" * 400000,)) # far bigger than the total
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, window=16768,
                                   session=session)

    size = await channel.context.get_size()
    parts = (size["system_prompt"]["tokens"] + size["tools"]["tokens"]
             + size["message_history"]["tokens"] + size["end_prompt"]["tokens"])
    assert parts <= size["total_tokens"]


async def test_compactions_are_counted_from_pis_session(pi_backend_cls, patched_context, tmp_path):
    """the popup's 'compacted N times' line, and the history part, follow pi's
    own compaction entries: pi's context restarts at the summary"""
    session = write_pi_session(tmp_path / "session.jsonl",
                               history=("ancient detail " * 200, "answer"),
                               compactions=1)
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, window=16768,
                                   session=session)

    size = await channel.context.get_size()
    assert size["compaction_count"] == 1

    plain = write_pi_session(tmp_path / "plain.jsonl", history=("ancient detail " * 200, "answer"))
    assert backend._session_projection_metrics(plain)["history_chars"] > \
        backend._session_projection_metrics(session)["history_chars"]


async def test_metrics_follow_a_refresh(pi_backend_cls, patched_context, tmp_path):
    """after a turn (or a compaction) pi's stats are refreshed; the numbers the
    UI reads must move with them instead of freezing at the first reading"""
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=4200, window=16768,
                                   session=session)
    assert await channel.context.get_total_tokens() == 4200

    backend.pi_stats_override["contextUsage"]["tokens"] = 9000
    backend._stats_taken = 0.0 # the turn ended: the cached reading is stale
    assert await channel.context.get_total_tokens() == 9000
    assert (await channel.context.get_size())["percent_full"] == round((9000 / 16768) * 100)


async def test_pill_and_breakdown_agree(pi_backend_cls, patched_context, tmp_path):
    """the popup derives its own hero from get_size while the pill uses
    get_total_tokens - they must not tell two stories"""
    session = write_pi_session(tmp_path / "session.jsonl")
    backend, channel = _with_stats(pi_backend_cls, tmp_path, tokens=5555, window=16768,
                                   session=session)

    assert (await channel.context.get_size())["total_tokens"] == await channel.context.get_total_tokens()


# --- patch hygiene --------------------------------------------------------

async def test_context_metric_patches_are_restored(pi_backend_cls):
    module_cls = load_pi_module("module").Pi
    shell = module_cls.__new__(module_cls)
    shell._patches = []
    before = (core.context.Context.get_total_tokens, core.context.Context.get_size)

    shell._patch_context()
    assert core.context.Context.get_total_tokens is not before[0]
    for cls, name, original in reversed(shell._patches):
        setattr(cls, name, original)

    assert (core.context.Context.get_total_tokens, core.context.Context.get_size) == before
