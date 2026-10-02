# LIVEKIT_REFACTOR_SPEC.md — lars15

lars15 = lars13 rebuilt around **LiveKit WebRTC** for one goal: fewer moving parts
(one main server + LiveKit; no bespoke ws/PCM plumbing, no TLS-cert juggling for
the mic) with **true streaming + barge-in**. Styling and HUD outline are reused
from lars13. This file is the working spec for the coding agent.

Source: technical refactoring specification drafted 2026-10-02 from `AxiomLC/lars13`
(Windows host, Python 3.13 via uv, faster-whisper, FastAPI, single-file vanilla JS HUD).

## 1. Goal

Replace lars13's raw WebSocket PCM audio loop with a **LiveKit WebRTC pipeline
agent**, preserving:

1. Single-file vanilla JS browser HUD (`server/hud/index.html`) — restyle basis.
2. Hermes agent bridge (`http://127.0.0.1:8642`, 'lars' profile session state & memory).
3. TTS provider toggle (deepgram | fishaudio | groq | deepinfra | elevenlabs | local).
4. Dual engine modes:
   - **LOCAL**: local LiveKit server (Windows exe), local faster-whisper (base.en int8),
     local kokoro-onnx.
   - **CLOUD**: LiveKit Cloud SFU, Groq STT (whisper-large-v3-turbo),
     DeepInfra/Fish/Deepgram TTS.

## 2. Transport map

```
                Browser HUD (Vanilla JS)
             LiveKit WebRTC SDK + HTML5 Audio
                        |
                 [ LiveKit Room ]
                        |
        +---------------+---------------+
        |                               |
  MODE 1: LOCAL ENGINE           MODE 2: CLOUD ENGINE
  LiveKit Server (Win exe)       LiveKit Cloud SFU
  - STT: Silero VAD + Whisper    - STT: Groq whisper-large-v3-turbo
  - LLM: Hermes (:8642)          - LLM: Hermes Bridge (:8642)
  - TTS: Kokoro / Piper          - TTS: DeepInfra / Fish / Deepgram
                        |
             Hermes Session Bridge (:8642)
             ('lars' profile state & memory)
```

Carried over from lars13 (VERIFIED 2026-10-01, see lars13 setupREADME.md):
- `:8642` requires the **standalone gateway** (`hermes gateway run|install`), NOT the
  desktop's internal "Gateway ready". Desktop chat works over internal IPC regardless.
- `/api/sessions/{id}/chat/stream` POST body key is `{"input": text}` (not "message").
- Live session id must come from a fresh lookup, not a stale cache.

## 3. Config additions (server/config/server.yaml)

```yaml
livekit:
  engine_mode: "cloud"        # local | cloud
  local_ws_url: "ws://127.0.0.1:7880"
  cloud_ws_url: "wss://your-project.livekit.cloud"

voice:
  provider: "deepgram"        # deepgram | fishaudio | groq | deepinfra | elevenlabs | local
  stt_provider: "groq"        # groq (cloud) | faster_whisper (local)

hermes:
  api_url: "http://127.0.0.1:8642/v1"
  profile_session: "lars"
```

## 4. Backend changes

### A. Token endpoint (server/server.py)

```python
from livekit.api import AccessToken, VideoGrants

@app.post("/api/voice/token")
async def get_livekit_token(payload: dict):
    session_id = payload.get("sessionId", "lars")
    engine_mode = payload.get("mode", config["livekit"]["engine_mode"])
    grant = VideoGrants(room_join=True, room=session_id)
    token = AccessToken(os.getenv("LIVEKIT_API_KEY","devkey"),
                        os.getenv("LIVEKIT_API_SECRET","secret"),
                        grants=grant, identity=f"user_{session_id}")
    ws_url = (config["livekit"]["cloud_ws_url"] if engine_mode == "cloud"
              else config["livekit"]["local_ws_url"])
    return {"token": token.to_jwt(), "wsUrl": ws_url}
```

### B. Pipeline agent (server/agent_worker.py — NEW FILE)

```python
from livekit import agents
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import silero, groq, openai

HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642/v1")

async def entrypoint(ctx: JobContext):
    await ctx.connect()
    await ctx.wait_for_participant()
    session_id = ctx.room.name    # 'lars' session binding
    mode = os.getenv("LIVEKIT_ENGINE_MODE", "cloud")

    stt_engine = (groq.STT(model="whisper-large-v3-turbo") if mode == "cloud"
                  else agents.stt.StreamAdapter(stt=LocalWhisperSTT("base.en"),
                                                vad=silero.VAD.load()))

    provider = os.getenv("TTS_PROVIDER", "deepgram")
    tts_engine = ...   # dispatch: deepinfra -> openai.TTS(base_url=deepinfra, model=kokoro)
                       #            groq -> groq.TTS(); local -> LocalKokoroTTS("af_heart")
                       # NOTE(lars13 lesson): groq/deepinfra return WAV - strip header OR let
                       # plugin decode; deepgram returns raw pcm (no strip) - keep adapter.

    agent = agents.VoicePipelineAgent(
        vad=silero.VAD.load(), stt=stt_engine,
        llm=openai.LLM(base_url=HERMES_API_URL, api_key=os.getenv("API_SERVER_KEY","")),
        tts=tts_engine,
    )
    agent.start(ctx.room)
    await agent.say("Lars voice system initialized.", allow_interruptions=True)

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
```

### C. What gets DELETED from lars13's server.py in lars15

- Custom `ws :8765` audio loop (websocket_endpoint, ConnState, _run_turn,
  _send_tts_sentence, _maybe_schedule_partial, raw-PCM chunk plumbing).
- HUD audio auth-token gate (LiveKit JWT replaces it), TLS ports 443/8766/9443 + certs.
- RealtimeSTT recorder thread (kept only as LOCAL-mode whisper worker).
- Kept: TTS provider adapters (yield PCM -> LiveKit source), usage tracking,
  'lars' session bridge, HUD static serving (plain HTTP; WebRTC media is
  DTLS-encrypted at transport level).

## 5. Frontend changes (server/hud/index.html)

```html
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
```

```javascript
let currentRoom = null, currentEngineMode = "cloud";

async function connectLiveKitVoice(engineMode = "cloud") {
  if (currentRoom) await currentRoom.disconnect();
  const { token, wsUrl } = await (await fetch('/api/voice/token', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({sessionId:'lars', mode: engineMode})
  })).json();
  currentRoom = new LivekitClient.Room({ adaptiveStream: true, dynacast: true });
  currentRoom.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === LivekitClient.Track.Kind.Audio) {
      document.body.appendChild(track.attach());
      setReactorState("speaking");
    }
  });
  await currentRoom.connect(wsUrl, token);
  await currentRoom.localParticipant.setMicrophoneEnabled(true);
  setReactorState("listening");
}

// engine toggle btn -> swaps LOCAL/CLOUD and reconnects
```

## 6. Execution roadmap

1. `uv pip install livekit-agents livekit-api livekit-plugins-silero livekit-plugins-groq
   livekit-plugins-openai kokoro-onnx` in lars15/.venv
2. Add `livekit` block to server.yaml; LIVEKIT_API_KEY/SECRET in .env
3. `/api/voice/token` route in server.py
4. `server/agent_worker.py`; run `.venv\Scripts\python.exe server\agent_worker.py dev`
5. HUD: swap ws-audio for the LiveKit client SDK
6. Latency test: same protocol as lars13 (time_to_first_tts_audio_byte), plus
   LiveKit-native opus/red jitter metrics.

## 7. Open items to verify during build

- LiveKit local server on Windows (single exe, `--dev` mode) - confirm port 7880 + UDP range.
- Whether VoicePipelineAgent's `openai.LLM` can stream from Hermes' OpenAI-compatible
  `/v1/chat/completions` (should work; Hermes SSE keepalive 30 s) vs keeping our
  chat/stream bridge - measure both.
- Kokoro-onnx local first-token latency on i7-6600U - lars13 rejected local TTS for the
  default path; local mode here is a MENU OPTION, not the default.
- Browser autoplay policy: first speaking audio needs one user gesture (ring click).

---
# ADDENDUM A — RESEARCH FINDINGS LOCKED — 2026-10-02

## 1. Official precedent: Hermes full-duplex voice (`voice-live`, desktop GPT-Live)

Hermes docs (user-guide/features/voice-mode) ship a full-duplex WebRTC voice mode whose
binding pattern lars15 MUST MIRROR:

- Voice client opens a WebRTC session; the GATEWAY (host) creates it via
  `POST /api/audio/voice-live/session`. Secrets never reach the client — the client only
  ever holds session id + SDP answer.
- Each voice exchange becomes `session.delegation.created` -> a NORMAL TURN on the open
  chat session (same session id, full history continuity).
- Streaming reply is sentence-buffered (min 20 chars), markdown stripped, per-sentence
  audio; chunked-PCM providers stream raw for lowest time-to-first-word.
- Relay path (providers that must run on the host): `POST /api/audio/transcribe`
  + gateway speech WebSocket. `voice.client_direct: false` forces everything through it.
- Stop phrase ends the conversation; tool activity is narrated as quiet context.

LESSON for lars15: bind the LiveKit room to the OPEN 'lars' session and emit turns as
normal session turns -> continuity across desktop/dashboard hops is native (all
surfaces share one SessionDB/state.db session store).

## 2. Continuity hazards (external clients)

- GitHub issue #16938: when Hermes auto-COMPRESSES a session, the API server keeps
  echoing the stale parent `X-Hermes-Session-Id` -> external clients reload uncompressed
  parent history. Rule for lars15: NEVER cache-and-blindly-reuse session ids. Always
  re-read the session id from response/SSE metadata each turn; on stale/missing-id
  continue via `SessionDB.get_compression_tip()`-equivalent behavior: fresh lookup then
  rebind (same 'resume -> LIVE id' discipline proven in the lars Lars-bridge).
- Session lease: one client turn owns the session at a time; parallel writes from
  another surface get busy/SENT_NOT_OWNED style conflicts while a run is live.
  lars15 must do attach-per-turn (bind at turn start, release cleanly after),
  reading fresh live ids (see lars13 setupREADME Lars-bridge mechanics).

## 3. DECIDED for lars15 (was the only open design question)

- LLM interface of the LiveKit pipeline agent = the SESSION chat/stream bridge
  (`POST /api/sessions/{id}/chat/stream`, body `{"input": text}`, Accept: text/event-stream),
  implemented as a custom LiveKit LLM plugin — NOT the stateless
  `/v1/chat/completions` shim. Rationale: identical to the official voice-live pattern,
  preserves 'lars' session continuity + compression handling.
- STT/TTS: provider toggles carried from lars13 (default deepgram aura-asteria-en —
  Aura-2 is permission-locked on this project). Cloud/local engine switch via
  livekit.engine_mode.
- Providers run through lars15's adapters (yield PCM) feeding LiveKit source tracks.
  Barge-in = LiveKit-native (agent playback interrupt) — no custom code.
- HUD: 4-panel outline (top/left/center/right) from lars13 as the frame; Hermes
  gateway endpoints only feed panels — NO extra servers, NO :9119 dependency for
  panel data (dashboard proxy only optional for kanban embeds).
- `hermes-plugin/hud_display` remains the agent's channel to summon/clear media
  panels inside the HUD; lars15 consumes its payloads via the session event feed.

## 4. Deferred (later, user-initiated)

- Single full-page kanban-style summary of all Hermes jobs, summonable by Lars
  (separate page, gateway endpoints as data source).
