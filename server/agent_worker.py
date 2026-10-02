#!/usr/bin/env python3
"""lars15 LiveKit voice-pipeline agent worker.

Job: join the LiveKit room, publish/transcribe the user's mic via plugin STT,
drive the LLM turn through the Hermes session chat/stream bridge, and stream
the reply back as agent audio through the TTS provider adapters.

Barge-in is LiveKit-native: preemption of agent playback when the participant
speaks (VAD) — no custom code. Run:  python server/agent_worker.py dev
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

import requests
import yaml

from livekit import agents, api, rtc
from livekit.agents import (
    Agent, AgentSession, JobContext, JobProcess, RoomInputOptions, WorkerOptions,
    cli, llm, tts as lk_tts, stt as lk_stt,
)
from livekit.plugins import silero

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "server.yaml"
ENV_PATHS = [Path.home() / ".hermes" / ".env", ROOT / ".env", ROOT.parent / ".env"]

for path in ENV_PATHS:
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() == "LIVEKIT_API_SECRET":
                value = value.replace(" ", "").replace("\t", "")  # paste-artifact guard
            os.environ.setdefault(key.strip(), value)

CFG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
HERMES_BASE = ((CFG.get("hermes") or {}).get("base_url") or "http://127.0.0.1:8642").rstrip("/")
HERMES_PROFILE = (CFG.get("hermes") or {}).get("profile_session", "lars")
API_KEY_ENV = (CFG.get("hermes") or {}).get("api_key_env", "API_SERVER_KEY")


def hermes_headers() -> dict:
    key = os.environ.get(API_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def fresh_live_session() -> str:
    """Fresh live session id per turn (never trust cached: #16938)."""
    base = f"{HERMES_BASE}/api/sessions"
    r = requests.get(base, headers=hermes_headers(),
                     params={"profile": HERMES_PROFILE, "limit": 1}, timeout=15)
    rows = r.json() if r.ok else []
    rows = rows.get("sessions", rows) if isinstance(rows, dict) else rows
    if rows and isinstance(rows[0], dict) and rows[0].get("id"):
        return rows[0]["id"]
    r = requests.post(base, headers=hermes_headers(),
                      json={"title": f"{HERMES_PROFILE}-voice"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return (data.get("session") or data).get("id")


def stop_run(run_id: str) -> dict:
    r = requests.post(f"{HERMES_BASE}/v1/runs/{run_id}/stop",
                      headers=hermes_headers(), timeout=15)
    return {"status_code": r.status_code, "body": r.text[:200]}


def clean_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r".*?", "", text, flags=re.S)
    text = re.sub(r"```.*?```", " code omitted. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[\s>*#-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class HermesSessionLLM(llm.LLM):
    """LiveKit LLM plugin that drives one turn through the Hermes session
    chat/stream SSE endpoint (the official voice-live binding pattern)."""

    def __init__(self):
        self.run_id = ""

    def chat(self, ctx: llm.ChatContext, options=None) -> AsyncIterator[llm.ChatChunk]:
        return self._stream(ctx)

    async def _stream(self, ctx: llm.ChatContext) -> AsyncIterator[llm.ChatChunk]:
        latest = ""
        for item in reversed(ctx.items):
            if item.role == "user" and isinstance(item, llm.ChatMessage):
                latest = item.text_content or ""
                break
        if not latest.strip():
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            session_id = fresh_live_session()
            try:
                r = requests.post(
                    f"{HERMES_BASE}/api/sessions/{session_id}/chat/stream",
                    headers={**hermes_headers(), "Accept": "text/event-stream"},
                    json={"input": latest}, stream=True, timeout=(10, 240))
                if r.status_code >= 400:
                    raise RuntimeError(f"Hermes chat HTTP {r.status_code}: {r.text[:200]}")
                r.encoding = "utf-8"
                event = ""
                for raw in r.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw == "":
                        event = ""
                        continue
                    if raw.startswith("event:"):
                        event = raw[6:].strip()
                        continue
                    if not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        payload = {"text": data}
                    etype = event or payload.get("event") or payload.get("type") or ""
                    if etype in ("assistant.delta", "message.delta", "delta"):
                        text = payload.get("text") or payload.get("delta") or ""
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, ("text", text))
                    elif etype in ("run.started", "run_started"):
                        self.run_id = str(payload.get("run_id") or payload.get("id") or "")
                    elif etype in ("assistant.completed", "run.completed", "run.failed",
                                   "run.cancelled", "message.complete"):
                        loop.call_soon_threadsafe(queue.put_nowait, (None, None))
                        return
                loop.call_soon_threadsafe(queue.put_nowait, (None, None))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

        import json as _json  # local alias to keep module namespace clean
        await asyncio.to_thread(pump)

        sentence_parts: list[str] = []
        while True:
            kind, value = await queue.get()
            if kind is None:
                break
            if kind == "error":
                raise RuntimeError(value)
            sentence_parts.append(value)
            text = "".join(sentence_parts)
            # emit in sentence-sized chunks (mirrors Hermes voice-live sentence
            # buffering so TTS starts before the reply finishes generating)
            m = list(re.finditer(r"[.!?]\s", text))
            if m:
                cut = m[-1].end()
                spoken = text[:cut]
                sentence_parts = [text[cut:]]
                clean = clean_for_tts(spoken)
                if clean:
                    yield llm.ChatChunk(text=clean)

        clean = clean_for_tts("".join(sentence_parts))
        if clean:
            yield llm.ChatChunk(text=clean)


# ------------------------------------------------------------------- TTS plugin

class AdapterTTS(lk_tts.TTS):
    """Bridges lars13's provider adapters (raw pcm iterator) into LiveKit TTS."""

    def __init__(self, cfg: dict):
        v = cfg["voice"]
        super().__init__(sample_rate=v.get("sample_rate", 16000), num_channels=1)
        self.cfg = cfg

    async def run(self, text: str) -> AsyncIterator[lk_tts.ChunkedAudio]:
        # AdapterTTS synthesizes fully then streams (acceptable for cloud
        # providers whose HTTP path is already streamed by the adapter itself).
        import sys
        sys.path.insert(0, str(ROOT))
        from server15 import tts_chunks, TurnTiming, clean_for_tts
        timing = TurnTiming()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                for chunk in tts_chunks(self.cfg, clean_for_tts(text), timing):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        await asyncio.to_thread(pump)
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                raise chunk
            yield lk_tts.ChunkedAudio(audio=chunk)


# ------------------------------------------------------------------- STT plugin

def build_stt(mode: str) -> lk_stt.STT:
    if mode == "cloud":
        from livekit.plugins import groq as lk_groq
        return lk_groq.STT(model="whisper-large-v3-turbo")
    # local: faster-whisper through LiveKit's stt fallback plugin adapter
    from livekit.plugins.openai import STT as OpenAIStyleSTT  # shape-compatible shim
    return OpenAIStyleSTT(base_url="http://127.0.0.1:8768/v1")  # worker/stt_server.py sidecar


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    lk = CFG.get("livekit") or {}
    mode = os.getenv("LIVEKIT_ENGINE_MODE", lk.get("engine_mode", "cloud"))
    stt_engine = build_stt(mode)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt_engine,
        llm=HermesSessionLLM(),
        tts=AdapterTTS(CFG),
    )

    @session.on("user_input_transcribed")
    def _on_transcript(ev):
        print(f"TRANSCRIPT ({ev.is_final}): {ev.transcript}", flush=True)

    agent = Agent(
        instructions=(
            "You are speaking aloud through a voice pipeline. Plain conversational "
            "prose only: no markdown, no lists, no code blocks. One to three short "
            "sentences by default. Never speak secrets aloud."
        ),
    )
    await session.start(room=ctx.room, room_input_options=RoomInputOptions(), agent=agent)
    await session.say("Lars voice system initialized.", allow_interruptions=True)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
