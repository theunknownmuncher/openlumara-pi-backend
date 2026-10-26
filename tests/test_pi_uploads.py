# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
uploads (images, audio, files) behave the same on pi as on the default backend

core/channel._process_multimodal turns an upload into OpenAI content blocks
before any backend sees it, so the default backend simply forwards them:
images as image_url, audio as input_audio, plain files and PDFs already
flattened into text. pi's rpc takes text plus ImageContent attachments, so
the module has to (a) actually hand the images over - pi silently drops
images for any model it does not flag as image-capable - and (b) never let
a block pi cannot carry (audio) vanish without the agent noticing.
"""

import asyncio
import base64
import json
import pathlib
import types

import pytest

from harness import FakeManager

from test_pi_tool_stream import FakeModule, load_pi_module

@pytest.fixture
def pi_backend_cls():
    return load_pi_module("backend").PiBackend


def _backend():
    return load_pi_module("backend")


def png_message(text="what is in this picture?"):
    """a user message exactly as _process_multimodal builds one"""
    pixel = base64.b64encode(b"\x89PNG\r\n\x1a\n fake-png-bytes").decode()
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{pixel}"}},
        ],
        "_metadata": {"filenames": ["", "shot.png"]},
    }


def audio_message(text="transcribe this"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "ogg"}},
        ],
        "_metadata": {"filenames": ["", "voice.ogg"]},
    }


# --- extraction -----------------------------------------------------------

def test_images_are_extracted_intact():
    text, images, notes = _backend()._extract_message(png_message())
    assert text == "what is in this picture?"
    assert notes == []
    assert len(images) == 1
    assert images[0]["type"] == "image"
    assert images[0]["mimeType"] == "image/png"
    assert base64.b64decode(images[0]["data"]).startswith(b"\x89PNG")


def test_plain_files_and_pdfs_already_rode_in_the_text():
    """_process_multimodal flattens those to text blocks - nothing to do, and
    nothing may be reported as missing"""
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "read this"},
            {"type": "text", "text": "File: notes.md\n\n```hello```"},
            {"type": "text", "text": "File: report.pdf\n\n```pdf\npage one\n```"},
        ],
        "_metadata": {"filenames": ["", "notes.md", "report.pdf"]},
    }
    text, images, notes = _backend()._extract_message(message)
    assert "File: notes.md" in text and "File: report.pdf" in text
    assert images == [] and notes == []


def test_audio_is_named_instead_of_vanishing():
    """the default backend forwards input_audio to its endpoint; pi has no
    audio attachment, so the prompt must at least say one arrived"""
    text, images, notes = _backend()._extract_message(audio_message())
    assert images == []
    assert len(notes) == 1 and "voice.ogg" in notes[0]
    assert "transcribe this" in text


def test_images_off_still_tells_the_agent():
    _, images, notes = _backend()._extract_message(png_message(), forward_images=False)
    assert images == []
    assert len(notes) == 1 and "shot.png" in notes[0]


def test_recap_names_attachments():
    """after a session rebuild the replay must not pretend nothing arrived"""
    render = _backend()._render_chat_history
    assert "image(s) attached" in render([png_message()])
    assert "voice.ogg" in render([audio_message()])


# --- the injected model must be flagged image-capable ---------------------
# pi swaps images for a "current model doesn't support image input" note for
# any entry saying input: ["text"] - verified against a real pi process.

async def _injected_models(pi_backend_cls, monkeypatch, forward_images=True):
    backend = pi_backend_cls.__new__(pi_backend_cls)
    backend.module = types.SimpleNamespace(
        PROVIDER_ID="openlumara",
        provider_extension_path=lambda: "/nowhere/ext.js",
        resolved_provider_settings=lambda: ("http://localhost:9/v1", "", "some-model"),
        config=types.SimpleNamespace(get=lambda key, default=None: {
            "forward_images": forward_images}.get(key, default)),
        log=lambda *a: None,
    )

    async def fake_models(base_url, api_key):
        return ["some-model"]

    monkeypatch.setattr(backend, "_fetch_endpoint_models", fake_models)
    env, args, notes = await backend._prepare_provider_injection()
    payload = json.loads(pathlib.Path(env["PI_OPENLUMARA_CONFIG"]).read_text())
    return payload["models"]


@pytest.mark.asyncio
async def test_injected_models_advertise_image_input(pi_backend_cls, monkeypatch):
    """otherwise uploads never leave the building: pi drops them client-side"""
    models = await _injected_models(pi_backend_cls, monkeypatch)
    assert models[0]["input"] == ["text", "image"]


@pytest.mark.asyncio
async def test_image_input_follows_the_forward_images_switch(pi_backend_cls, monkeypatch):
    models = await _injected_models(pi_backend_cls, monkeypatch, forward_images=False)
    assert models[0]["input"] == ["text"]


# --- through a real turn --------------------------------------------------

class RecordingPi:
    """pi rpc double that records what each prompt was made of"""

    def __init__(self, events):
        self._events = list(events)
        self._pos = 0
        self.prompts = []
        self.images = []
        self.running = True
        self.alive = True
        self.stderr_tail = []
        self.pi_version = "test"
        self._process = None

    async def prompt(self, text, images=None, timeout=None):
        self.prompts.append(text)
        self.images.append(list(images or []))

    def clear_events(self):
        pass

    async def next_event(self, timeout=None):
        if self._pos >= len(self._events):
            await asyncio.sleep(3600)
        event = self._events[self._pos]
        self._pos += 1
        return event

    async def get_session_stats(self):
        return {"tokensBefore": 0, "estimatedTokensAfter": 0, "contextWindow": 100000}

    async def stop(self):
        pass


class UploadChannel:
    """chat store the backend can read and append to"""

    def __init__(self, manager, messages):
        self.manager = manager
        self.stored = list(messages)
        channel = self

        class _Messages:
            async def get(self, index=None):
                return list(channel.stored) if index is None else channel.stored[index]

            async def add(self, message, **kwargs):
                channel.stored.append(dict(message))

        self.context = types.SimpleNamespace(
            chat=types.SimpleNamespace(messages=_Messages(), id="chat-1", title="t")
        )


def _turn_events():
    return [
        {"type": "message_start", "message": {"role": "user"}},
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message_start", "message": {"role": "assistant", "model": "m"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "nice"}},
        {"type": "message_end", "message": {
            "role": "assistant", "model": "m", "stopReason": "stop", "usage": {},
            "content": [{"type": "text", "text": "nice"}]}},
        {"type": "agent_settled"},
    ]


async def _run_turn(pi_backend_cls, message, forward_images=True):
    manager = FakeManager()
    module = FakeModule({"forward_images": forward_images})
    backend = pi_backend_cls(manager, module)
    client = RecordingPi(_turn_events())
    backend.pi = client
    channel = UploadChannel(manager, [message])
    manager.channel = channel

    async def _none(channel=None):
        return None

    backend._sync_session = _none
    backend._rebuild_if_history_diverged = _none

    tokens = [t async for t in backend.send_stream([message])]
    return client, tokens


@pytest.mark.asyncio
async def test_turn_hands_the_image_to_pi(pi_backend_cls):
    client, tokens = await _run_turn(pi_backend_cls, png_message())
    assert not [t for t in tokens if t.get("type") == "error"]
    assert client.images[0] and client.images[0][0]["mimeType"] == "image/png"
    assert base64.b64decode(client.images[0][0]["data"]).startswith(b"\x89PNG")
    assert "what is in this picture?" in client.prompts[0]


@pytest.mark.asyncio
async def test_turn_notes_the_audio_for_the_agent(pi_backend_cls):
    client, tokens = await _run_turn(pi_backend_cls, audio_message())
    assert not [t for t in tokens if t.get("type") == "error"]
    assert "transcribe this" in client.prompts[0]
    assert "voice.ogg" in client.prompts[0]


@pytest.mark.asyncio
async def test_turn_with_forwarding_off_drops_but_names(pi_backend_cls):
    client, tokens = await _run_turn(pi_backend_cls, png_message(), forward_images=False)
    assert client.images[0] == []
    assert "shot.png" in client.prompts[0]
