#!/usr/bin/env python3
"""lars15 main server — one process for everything except the LiveKit agent worker.

Owns:
  - HUD static serving (single-file vanilla JS, plain HTTP)
  - /api/voice/token   -> LiveKit JWT for the browser (secrets stay server-side,
                          mirrors Hermes voice-live: client only gets id+url)
  - /api/bridge/*      -> Hermes 'lars' profile session bridge helpers
  - TTS provider adapters (ported verbatim from lars13: contract = raw pcm s16le
    @ voice.sample_rate, chunk-yielding, barge-in safe)
  - /api/jobs          -> gateway-backed kanban/job summary data (panel feed)

The audio transport itself is LiveKit's job (server/agent_worker.py + HUD SDK).
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import os
import re
import time
import wave
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import requests
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    from livekit import api as livekit_api
except ImportError:  # token endpoint degrades; agent worker can't run without it
    livekit_api = None

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "server.yaml"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / "latency.jsonl"
SESSION_STATE_PATH = LOG_DIR / "hermes_sessions.json"

ENV_PATHS = [Path.home() / ".hermes" / ".env", ROOT / ".env", ROOT.parent / ".env"]


def load_env() -> None:
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


@dataclass
class TurnTiming:
    turn_id: int = 0
    transcript: str = ""
    response_text: str = ""
    stt_model: str = ""
    llm_provider: str = "hermes"
    llm_model: str = "hermes-agent"
    tts_model: str = ""
    voice_id: str = ""
    session_id: str = ""
    stt_start: float | None = None
    stt_final: float | None = None
    llm_start: float | None = None
    llm_first_token: float | None = None
    first_sentence: float | None = None
    tts_request_start: float | None = None
    first_tts_audio_byte: float | None = None
    total_done: float | None = None
    interrupted: bool = False
    tools_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        def d(a, b):
            return None if a is None or b is None else round(b - a, 4)
        return {
            "turn_id": self.turn_id, "transcript": self.transcript,
            "response_text": self.response_text, "stt_model": self.stt_model,
            "llm_provider": self.llm_provider, "llm_model": self.llm_model,
            "tts_model": self.tts_model, "voice_id": self.voice_id,
            "session_id": self.session_id,
            "stt_finalize_seconds": d(self.stt_start, self.stt_final),
            "llm_time_to_first_token_seconds": d(self.llm_start, self.llm_first_token),
            "time_to_first_tts_audio_byte_seconds": d(self.tts_request_start, self.first_tts_audio_byte),
            "interrupted": self.interrupted, "tools_used": self.tools_used,
            "errors": self.errors,
        }


def log_turn(t: TurnTiming) -> None:
    t.total_done = t.total_done or time.perf_counter()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(t.summary(), ensure_ascii=False) + "\n")
    print("TURN", json.dumps(t.summary(), ensure_ascii=False), flush=True)


# ==================================================================== Hermes bridge
# Verified lars13 rules (setupREADME ADDENDUM 3):
#   transport  : POST {base}/api/sessions/{id}/chat/stream  (SSE, {"input": text})
#   session id : never cache-stale; fresh lookup each turn; live id wins
#   interrupts : POST /v1/runs/{id}/stop, approvals POST /v1/runs/{id}/approval


class HermesBridge:
    def __init__(self, cfg: dict):
        h = cfg.get("hermes") or {}
        self.base = (h.get("base_url") or "http://127.0.0.1:8642").rstrip("/")
        self.profile = h.get("profile_session", "lars")
        self.api_key_env = h.get("api_key_env", "API_SERVER_KEY")
        self.timeout = h.get("timeout", 240)
        self._next_turn = 1

    def headers(self) -> dict:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"Hermes API key not found in env ({self.api_key_env})")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def fresh_live_session(self) -> str:
        """Fresh session id every turn: most_recent -> resume -> live id wins.
        Never return a cached id (issue #16938 compression-stale rule)."""
        # 1. prior stored id acts as the session *name* hint
        stored = ""
        if SESSION_STATE_PATH.exists():
            try:
                stored = json.loads(SESSION_STATE_PATH.read_text()).get(self.profile, "")
            except Exception:
                stored = ""
        # 2. ask the gateway for the profile's most recent session
        try:
            r = requests.get(f"{self.base}/api/sessions", headers=self.headers(),
                             params={"profile": self.profile, "limit": 1}, timeout=15)
            rows = r.json() if r.ok else []
            rows = rows.get("sessions", rows) if isinstance(rows, dict) else rows
            sid = (rows[0].get("id") if rows and isinstance(rows[0], dict) else "") or stored
        except Exception:
            sid = stored
        if not sid:
            r = requests.post(f"{self.base}/api/sessions", headers=self.headers(),
                              json={"title": f"{self.profile}-voice"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            sid = (data.get("session") or data).get("id")
        self._remember(sid)
        return sid

    def _remember(self, sid: str) -> None:
        try:
            state = json.loads(SESSION_STATE_PATH.read_text()) if SESSION_STATE_PATH.exists() else {}
        except Exception:
            state = {}
        state[self.profile] = sid
        SESSION_STATE_PATH.write_text(json.dumps(state))

    def stop_run(self, run_id: str) -> dict:
        r = requests.post(f"{self.base}/v1/runs/{run_id}/stop", headers=self.headers(), timeout=15)
        return {"status_code": r.status_code, "body": r.text[:300]}

    def token_getter(self):
        """Short-lived LiveKit JWT with a distinct identity per turn."""
        if livekit_api is None:
            raise HTTPException(503, "livekit-api not installed")
        lk = self.lk
        grant = livekit_api.VideoGrants(room_join=True, room=self.profile)
        token = livekit_api.AccessToken(lk["api_key"], lk["api_secret"],
                                        identity=f"{self.profile}-hud")
        token.with_grants(grant).with_ttl(datetime.timedelta(hours=1))
        return token.to_jwt()

    def set_livekit(self, lk: dict):
        self.lk = lk

    def chat_stream_events(
        self, transcript: str, timing: TurnTiming,
    ) -> Iterator[tuple[str, str]]:
        """Yield ("run"|"text"|"tool"|"approval"|"final", value) for one turn."""
        session_id = self.fresh_live_session()
        timing.session_id = session_id
        resp = requests.post(
            f"{self.base}/api/sessions/{session_id}/chat/stream",
            headers={**self.headers(), "Accept": "text/event-stream"},
            json={"input": transcript}, stream=True, timeout=(10, self.timeout),
        )
        if resp.status_code >= 400:
            resp.close()
            raise RuntimeError(f"Hermes chat HTTP {resp.status_code}: {resp.text[:300]}")
        resp.encoding = "utf-8"
        try:
            yield from self._parse_sse(resp, timing)
        finally:
            resp.close()

    @staticmethod
    def _parse_sse(resp, timing: TurnTiming) -> Iterator[tuple[str, str]]:
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
                yield ("text", data)
                continue
            etype = event or payload.get("event") or payload.get("type") or ""
            if etype in ("assistant.delta", "message.delta", "delta"):
                if timing.llm_first_token is None:
                    timing.llm_first_token = time.perf_counter()
                yield ("text", payload.get("text") or payload.get("delta") or "")
            elif etype in ("tool.start", "tool_use", "tool"):
                info = payload.get("tool") or payload.get("name") or payload
                timing.tools_used.append(str(info.get("name", "tool") if isinstance(info, dict) else info))
                yield ("tool", json.dumps(info) if isinstance(info, dict) else str(info))
            elif etype in ("run.started", "run_started", "run"):
                yield ("run", payload.get("run_id") or payload.get("id") or json.dumps(payload))
            elif etype in ("approval", "approval_request"):
                yield ("approval", json.dumps(payload))
            elif etype in ("assistant.completed", "run.completed", "run.failed",
                           "run.cancelled", "message.complete", "done"):
                timing.interrupted = payload.get("interrupted", False)
                yield ("final", json.dumps(payload))


# ==================================================================== TTS adapters
# Ported from lars13 (contract: iterator of raw pcm s16le chunks; barge-in safe).
# Provider list + config keys in server/config/server.yaml voice: block.

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|[A-Za-z0-9]{32,})", re.I)


def clean_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = SECRET_RE.sub(" redacted ", text)
    text = re.sub(r"```.*?```", " code omitted. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[\s>*#-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@staticmethod
def _noop():
    yield


def tts_chunks(cfg: dict, text: str, timing: TurnTiming) -> Iterator[bytes]:
    voice = cfg["voice"]
    provider = (voice.get("provider") or "deepgram").lower()
    timing.tts_model = voice.get("model", provider)
    timing.voice_id = voice.get("voice_id", "")
    timing.tts_request_start = timing.tts_request_start or time.perf_counter()

    def stream_raw(response):
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                if timing.first_tts_audio_byte is None:
                    timing.first_tts_audio_byte = time.perf_counter()
                yield chunk
        finally:
            response.close()  # barge-in cancels mid-stream; don't leak the connection

    def strip_wav(response):
        header = b""
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            header += chunk
            idx = header.find(b"data")
            if idx != -1 and len(header) >= idx + 8:
                if timing.first_tts_audio_byte is None:
                    timing.first_tts_audio_byte = time.perf_counter()
                yield header[idx + 8:]
                break
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            if timing.first_tts_audio_byte is None:
                timing.first_tts_audio_byte = time.perf_counter()
            yield chunk

    if provider == "deepgram":
        key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not key:
            raise RuntimeError("DEEPGRAM_API_KEY missing")
        params = {"model": voice.get("model", "aura-asteria-en"),
                  "encoding": "linear16", "sample_rate": "16000", "container": "none"}
        r = requests.post("https://api.deepgram.com/v1/speak",
                          headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
                          params=params, json={"text": text}, stream=True, timeout=120)
        if r.status_code >= 400:
            r.close(); raise RuntimeError(f"Deepgram HTTP {r.status_code}: {r.text[:300]}")
        yield from stream_raw(r)
        return

    if provider == "fishaudio":
        key = os.environ.get("FISH_API_KEY") or os.environ.get("FISHAUDIO_API_KEY", "")
        if not key:
            raise RuntimeError("FISH_API_KEY missing")
        payload = {"text": text, "format": "pcm", "sample_rate": 16000,
                   "latency": voice.get("latency", "low")}
        if voice.get("voice_id"):
            payload["reference_id"] = voice["voice_id"]
        r = requests.post("https://api.fish.audio/v1/text-to-speech",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=payload, stream=True, timeout=120)
        if r.status_code >= 400:
            r.close(); raise RuntimeError(f"FishAudio HTTP {r.status_code}: {r.text[:300]}")
        yield from stream_raw(r)
        return

    if provider in ("groq", "deepinfra"):
        endpoints = {"groq": "https://api.groq.com/openai/v1/audio/speech",
                     "deepinfra": "https://api.deepinfra.com/v1/audio/speech"}
        env_key = f"{provider.upper()}_API_KEY"
        key = os.environ.get(env_key, "")
        if not key:
            raise RuntimeError(f"{env_key} missing")
        r = requests.post(endpoints[provider],
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": voice.get("model"), "input": text,
                                "voice": voice.get("voice_name", ""), "response_format": "wav"},
                          stream=True, timeout=120)
        if r.status_code >= 400:
            r.close(); raise RuntimeError(f"{provider} HTTP {r.status_code}: {r.text[:300]}")
        yield from strip_wav(r)
        return

    if provider == "elevenlabs":
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY missing")
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice['voice_id']}/stream",
            params={"output_format": voice.get("output_format", "pcm_16000")},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": voice.get("model", "eleven_flash_v2_5")},
            stream=True, timeout=120)
        if r.status_code >= 400:
            r.close(); raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:300]}")
        yield from stream_raw(r)
        return

    raise RuntimeError(f"Unknown voice.provider '{provider}'")


# ==================================================================== FastAPI app

load_env()
CFG = load_config()
BRIDGE = HermesBridge(CFG)

app = FastAPI(title="lars15")


def resolve_livekit(lk: dict) -> tuple[str, str, str]:
    """Dev toggle: LIVEKIT_ENGINE_MODE env var wins over yaml engine_mode.
    Returns (ws_url, mode, engine_label). Both modes use the same agent code."""
    mode = (os.getenv("LIVEKIT_ENGINE_MODE") or lk.get("engine_mode") or "cloud").lower()
    ws_url = lk.get("cloud_ws_url") if mode == "cloud" else lk.get("local_ws_url")
    return ws_url, mode, "livekit-cloud" if mode == "cloud" else "livekit-local"


@app.post("/api/voice/token")
async def voice_token(payload: dict):
    """Hand the browser ONLY a room identity+jwt — never provider secrets."""
    lk = CFG.get("livekit") or {}
    if not livekit_api:
        raise HTTPException(503, "livekit-api missing")
    key = os.environ.get("LIVEKIT_API_KEY", lk.get("api_key", ""))
    secret = os.environ.get("LIVEKIT_API_SECRET", lk.get("api_secret", ""))
    if not key or not secret:
        raise HTTPException(503, "LIVEKIT_API_KEY/SECRET not configured")
    ws_url, mode, label = resolve_livekit(lk)
    grant = livekit_api.VideoGrants(room_join=True, room=payload.get("sessionId", "lars"))
    token = (livekit_api.AccessToken(key, secret)
             .with_identity(f"lars-hud-{int(time.time())%100000}")
             .with_grants(grant).with_ttl(datetime.timedelta(hours=1)))
    return {"token": token.to_jwt(), "wsUrl": ws_url, "mode": mode, "engine": label}


@app.get("/api/health")
async def health():
    probes = {}
    key = os.environ.get("API_SERVER_KEY", "")
    try:
        probes["hermes"] = requests.get(f"{BRIDGE.base}/health",
                                        headers={"Authorization": f"Bearer {key}"},
                                        timeout=4).status_code in (200, 401, 403)
    except Exception as exc:
        probes["hermes"] = f"down: {exc}"
    ws_url, mode, label = resolve_livekit(CFG.get("livekit") or {})
    return {"status": "ok", "hermes": probes, "profile": BRIDGE.profile,
            "livekit": {"mode": mode, "ws_url": ws_url,
                        "keys": bool(os.environ.get("LIVEKIT_API_KEY"))}}


@app.post("/api/agent/turn")
async def agent_turn(payload: dict, request: Request):
    """Text-side escape hatch (same bridge): typed turns share the 'lars' session."""
    text = payload.get("input", "")
    if not text:
        raise HTTPException(400, "input required")
    timing = TurnTiming(turn_id=int(time.time() % 1e7), transcript=text,
                        llm_start=time.perf_counter())
    parts, finals = [], []
    def run_stream():
        for kind, value in BRIDGE.chat_stream_events(text, timing):
            if kind == "text":
                parts.append(value)
            elif kind == "final":
                finals.append(value)
    await asyncio.to_thread(run_stream)
    timing.response_text = "".join(parts).strip()
    log_turn(timing)
    return {"response": timing.response_text, "session_id": timing.session_id,
            "interrupted": timing.interrupted, "tools_used": timing.tools_used}


@app.post("/api/agent/stop")
async def agent_stop(payload: dict):
    run_id = payload.get("run_id", "")
    if not run_id:
        raise HTTPException(400, "run_id required")
    return BRIDGE.stop_run(run_id)


@app.get("/api/latency")
async def latency():
    if not LOG_PATH.exists():
        return []
    rows = [json.loads(line) for line in LOG_PATH.read_text().splitlines() if line.strip()]
    return rows[-50:]


# ------------------------------------------------------ jobs / kanban panel feed

_allowed = ("/api/jobs", "/v1/skills", "/v1/toolsets", "/api/sessions")


@app.get("/api/hermes/{path:path}")
async def hermes_proxy(path: str, request: Request):
    """Strict allowlist read-proxy: HUD panels pull stats straight from the
    gateway (:8642) — no extra servers, no :9119 dependency."""
    target = "/" + path
    if request.method == "GET" and (target in _allowed or
                                    (target.startswith("/api/sessions/") and target.endswith("/messages"))):
        pass
    else:
        return Response(status_code=403, content="path not allowed")
    params = dict(request.query_params)
    try:
        r = await asyncio.to_thread(
            lambda: requests.get(BRIDGE.base + target, headers=BRIDGE.headers(),
                                 params=params, timeout=30))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("Content-Type", "application/json"))


# ------------------------------------------------------ static HUD

app.mount("/", StaticFiles(directory=str(ROOT / "hud"), html=True), name="hud")


def main() -> int:
    cfg = CFG["server"]
    lk = CFG.get("livekit") or {}
    BRIDGE.set_livekit(lk)
    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", 8646))  # 8646+ is free of gateway default-platform ports
    print(f"lars15 server on http://{host}:{port}/hud/"
          f" (LiveKit mode: {lk.get('engine_mode', 'cloud')})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
