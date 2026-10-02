#!/usr/bin/env python3
"""lars15 LiveKit voice-pipeline agent worker.

Join the LiveKit room, transcribe the user's mic (plugin STT), drive the LLM
turn through the Hermes session chat/stream bridge, stream the reply back as
agent audio through lars13's provider TTS adapters. Barge-in is LiveKit-native.

Run:  .venv\\Scripts\\python.exe server\\agent_worker.py dev
"""
from __future__ import annotations

import asyncio
import datetime
import json  # used in _parse_sse of the pump thread
import datetime
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import requests
import yaml

from livekit import api as lk_api, rtc
from livekit.agents import (
    APIConnectOptions,
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
    llm,
    tts as lk_tts,
    stt as lk_stt,
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

sys.path.insert(0, str(ROOT))
from server15 import clean_for_tts, tts_chunks, TurnTiming  # noqa: E402


def hermes_headers() -> dict:
    key = os.environ.get(API_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"{API_KEY_ENV} not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def fresh_live_session() -> str:
    """Fresh live session id per turn (never trust cached: issue #16938)."""
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


SENTENCE_END = re.compile(r"[.!?]\s")


def _parse_sse_stream(resp, queue, loop) -> None:
    """Blocking SSE parse; queue ('text',s) increments, (None,) final."""
    event = ""
    for raw in resp.iter_lines(decode_unicode=True):
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
            pass
        elif etype in ("assistant.completed", "run.completed", "run.failed",
                       "run.cancelled", "message.complete"):
            loop.call_soon_threadsafe(queue.put_nowait, (None, None))
            return
    loop.call_soon_threadsafe(queue.put_nowait, (None, None))  # stream ended


class HermesSessionLLM(llm.LLM):
    """LiveKit LLM plugin: one turn through the Hermes session chat/stream SSE
    endpoint (the official voice-live binding pattern)."""

    def __init__(self):
        self.run_id = ""

    def chat(self, *, chat_ctx, tools=None, conn_options=None,
             parallel_tool_calls=None, tool_choice=None, extra_kwargs=None):
        return HermesLLMStream(self, chat_ctx=chat_ctx, tools=tools or [],
                               conn_options=conn_options or APIConnectOptions())


class HermesLLMStream(llm.LLMStream):
    def __init__(self, llm_ref, *, chat_ctx, tools, conn_options):
        super().__init__(llm_ref, chat_ctx=chat_ctx, tools=tools,
                         conn_options=conn_options)
        self._session_llm = llm_ref

    async def _run(self) -> AsyncIterator[llm.ChatChunk]:
        llm_ref = self._session_llm
        latest = ""
        for item in reversed(self.chat_ctx.items):
            if getattr(item, "role", None) == "user":
                latest = item.text_content or ""
                break
        if not latest.strip():
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                session_id = fresh_live_session()
                r = requests.post(
                    f"{HERMES_BASE}/api/sessions/{session_id}/chat/stream",
                    headers={**hermes_headers(), "Accept": "text/event-stream"},
                    json={"input": latest}, stream=True, timeout=(10, 240))
                if r.status_code >= 400:
                    raise RuntimeError(f"Hermes chat HTTP {r.status_code}: {r.text[:200]}")
                r.encoding = "utf-8"
                _parse_sse_stream(r, queue, loop)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                try:
                    r.close()
                except Exception:
                    pass

        await asyncio.to_thread(pump)

        parts = []
        while True:
            kind, value = await queue.get()
            if kind is None:
                break
            if kind == "error":
                raise RuntimeError(value)
            parts.append(value)
            text = "".join(parts)
            # emit in sentence-sized chunks (voice-live sentence buffering)
            m = list(SENTENCE_END.finditer(text))
            if m:
                cut = m[-1].end()
                spoken = text[:cut]
                parts = [text[cut:]]
                clean = clean_for_tts(spoken)
                if clean:
                    yield llm.ChatChunk(text=clean, request_id="hermes")

        clean = clean_for_tts("".join(parts))
        if clean:
            yield llm.ChatChunk(text=clean, request_id="hermes")


# ------------------------------------------------------------------- TTS plugin

class AdapterChunkedStream(lk_tts.ChunkedStream):
    """Pumps lars13 adapter pcm into LiveKit SynthesizedAudio frames."""

    def __init__(self, *, tts, input_text, conn_options, cfg):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._cfg = cfg

    async def _run(self) -> AsyncIterator[lk_tts.SynthesizedAudio]:
        request_id = f"lars15-{int(asyncio.get_event_loop().time()*1e6)}"
        timing = TurnTiming()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                for chunk in tts_chunks(self._cfg, self._input_text, timing):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)

        await asyncio.to_thread(pump)
        seg = 0
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                raise chunk
            n = len(chunk) // 2  # s16le mono
            i16 = memoryview(chunk).cast("h")
            # one rtc frame per 20ms (320 samples @16k)
            step = 320
            next_start = None
            for off in range(0, n - step + 1, step):
                samples = i16[off:off + step]
                fr = rtc.AudioFrame(
                    data=samples,
                    num_channels=1,
                    samples_per_channel=step,
                    sample_rate=16000,
                )
                next_start = (seg + off // step)
                yield lk_tts.SynthesizedAudio(frame=fr, request_id=request_id, is_final=False, segment_id=str(next_start))
            seg += (n // step)
        yield lk_tts.SynthesizedAudio(
            frame=rtc.AudioFrame(data=memoryview(b"\x00\x00" * 320).cast("h"),
                                 num_channels=1, samples_per_channel=320, sample_rate=16000),
            request_id=request_id, is_final=True, segment_id=str(seg))


class AdapterTTS(lk_tts.TTS):
    """Bridges lars13 provider adapters (raw pcm iterator) into LiveKit TTS."""

    def __init__(self, cfg: dict):
        from livekit.agents.tts import TTSCapabilities
        v = cfg["voice"]
        super().__init__(capabilities=TTSCapabilities(streaming=False),
                         sample_rate=v.get("sample_rate", 16000), num_channels=1)
        self._cfg = cfg

    def synthesize(self, text, conn_options=None):
        return AdapterChunkedStream(tts=self, input_text=text,
                                   conn_options=conn_options or APIConnectOptions(),
                                   cfg=self._cfg)


# ------------------------------------------------------------------- STT plugin

def build_stt(mode: str) -> lk_stt.STT:
    if mode == "cloud":
        from livekit.plugins import groq as lk_groq
        return lk_groq.STT(model="whisper-large-v3-turbo")
    # local: whisper sidecar exposing OpenAI-compatible /v1/audio/transcriptions
    from livekit.plugins.openai import STT as OpenAISTT
    return OpenAISTT(base_url="http://127.0.0.1:8768/v1")


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
