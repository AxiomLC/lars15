# Lars13 — J.A.R.V.I.S for Hermes Agent (Windows port of `eadmin2/jarvis_ai`)

Fork of [`eadmin2/jarvis_ai`](https://github.com/eadmin2/jarvis_ai) (178★), retargeted to run on **this Windows 10 box** against a **local** Hermes Agent, with a **local-TTS / API-TTS toggle** for latency/resource testing.

This file consolidates the research done before any code was touched. It is the working briefing for the fork/install. Keep it at the repo root.

---

## 1. What upstream is (ground truth)

`eadmin2/jarvis_ai` is an Iron-Man-style **voice + HUD** pipeline on top of Hermes Agent:

```
Browser HUD (any LAN device)          Host
 ── https/wss :443 ──────┐   ┌──────────────────────────────────────────┐
   mic · speaker · panels ├──►│ voice pipeline server (FastAPI/uvicorn) │
                          │   │ STT: RealtimeSTT + faster-whisper (CPU) ├──► Hermes Agent
 Push-to-talk client      │   │ TTS: ElevenLabs Flash (streaming)       │   API :8642
 ── ws :8765 ─────────────┘   │ HUD + auth + dashboard TLS proxy        │   memory · tools
                              └──────────────────────────────────────────┘
```

- **Everything self-hosted.** Only cloud calls are your LLM provider (via Hermes) and ElevenLabs (voice). STT is local.
- **Features worth keeping:** live transcription, streaming TTS reply (3–5s round trip), STOP/barge-in, approval cards, embedded Kanban/session dashboards, HUD media panels (via bundled `hermes-plugin/hud_display`), usage tracking, optional GPU ear.

### Repo layout (the parts that matter)
```
server/            FastAPI voice pipeline + HUD host       <-- THE CORE
  server.py        voice pipeline (STT + TTS + HermesAPI client + uvicorn)
  hud/             single-file HUD (vanilla JS, no build)
  config/server.example.yaml -> config/server.yaml (voice settings live here)
  scripts/         jarvis-{start,stop,restart,health}.sh, make-certs.sh, make-boot-audio.sh
client/            optional Windows/Linux push-to-talk client (wake-word capable)
worker/            optional GPU sidecars (big-model STT + stats)
hermes-plugin/     Hermes plugin: agent can summon/dismiss HUD media panels
launchd/           macOS auto-start (com.jarvis.voice, com.jarvis.dashboard)   <- REPLACE for Windows
docs/              SETUP, ARCHITECTURE, TROUBLESHOOTING
```

### Confirmed stack / deps (all pip-installable on Windows)
`fastapi uvicorn requests pyyaml numpy anthropic RealtimeSTT faster-whisper silero-vad websockets psutil`

> **IMPORTANT:** `faster-whisper` and `silero-vad` are **REQUIRED**. Recent `RealtimeSTT` releases treat them as optional and fail at runtime without them (silently for VAD, loudly for the engine).

---

## 2. Windows port — what must change

Upstream is macOS/Linux. For this box (Windows 10, user `Admin`):

| Upstream (macOS/Linux) | Windows replacement |
|---|---|
| `launchd/com.jarvis.{voice,dashboard}.plist` | `windows/start.cmd`, `stop.cmd`, `health.cmd` + Task Scheduler (or NSSM) for auto-start |
| `server/scripts/jarvis-*.sh` | `windows/*.cmd` / PowerShell equivalents |
| `scripts/make-certs.sh` (self-signed TLS) | `powershell New-SelfSignedCertificate` + trust step |
| Browser-trust TLS cert | `certutil -user -addstore Root cert.pem` (already in upstream SETUP) |
| Python venv `source .venv/bin/activate` | `python -m venv .venv` ; `.venv\Scripts\activate` ; use `.venv\Scripts\python.exe` |

First start: downloads Whisper `small.en` (~460 MB); 60–90 s warm-up (torch import + model load). TLS required for browser mic (secure-origin only). Bind `server.host: 0.0.0.0` for LAN; loopback otherwise.

### Hermes side (unchanged, already configured on this box)
`jarvis_ai` drives Hermes' **API server on `127.0.0.1:8642`** with key from `API_SERVER_KEY`. This box already runs `:9119` dashboard / `:8642` gateway / `:8000` voice via the **Lars plugin stack** — so the Hermes API end is already present. Point `server.yaml` `hermes.base_url` at `http://127.0.0.1:8642` (loopback) and set `API_SERVER_KEY` in `lars13.env`.

---

## 3. The local-TTS / API-TTS toggle (the key custom feature)

Upstream TTS is **hard-wired to ElevenLabs**. The server method `tts_chunks_sync()`:
- reads key `ELEVENLABS_API_KEY` (falls back `ELEVEN_API_KEY`, `XI_API_KEY`)
- POSTs to ElevenLabs with `voice.model`, `voice_id`, `output_format: pcm_16000`
- **streams 4KB chunks** via `yield bytes` (real streaming, feeds barge-in/partial playback)

**Because TTS is streamed chunk-by-chunk, we can swap the provider cleanly.** Plan:

1. `server/config/server.yaml` — add `voice.provider` (default `elevenlabs`):
   ```yaml
   voice:
     provider: elevenlabs   # elevenlabs | local | groq | deepinfra
     model: eleven_flash_v2_5
     voice_id: ...
   ```
2. At the top of `tts_chunks_sync`, branch on `voice.provider` but **keep the identical streaming contract** (`yield bytes`), so barge-in / partial playback is untouched:

   | Provider | Streaming source | Cost / latency / intent |
   |---|---|---|
   | `elevenlabs` | upstream code, unchanged | best quality; per-char cost; network |
   | `local` | **Piper** (on-device, models in `models/piper/`) or Kokoro | zero cost, lowest first-byte; CPU load |
   | `groq` | Groq TTS endpoint (you have key) | cheap/fast; network |
   | `deepinfra` | DeepInfra TTS endpoint (you have key) | cheap/fast; network |

3. **Toggle = edit `server.yaml` + restart.** (Config is re-read at start; can expose as env var for a `.cmd`/Task one-liner switch: `set LARS13_TTS=local && start.cmd`.)

This gives the **latency/resources experiment** you want: local Piper (CPU, no net) vs Groq/DeepInfra (fast net, cheap) vs ElevenLabs (best quality/most cost), all behind one switch.

---

## 4. Resource & hardware eval (this box)

- **CPU:** i7-6600U (2-core / 4-thread, ~2.8 GHz), **RAM** 16 GB, **no GPU** (Intel HD 520), SSD.
- **Local STT** `faster-whisper small.en` int8 on CPU: **works but is the heavy lift.** On 4 threads expect ~2–4 s transcription. For lower latency test `base.en` or `tiny.en` (upstream measures `tiny` at ~0.3 s for language ID; on 2/4 cores the decoder takes half and live-guest STT auto-disables).
- **Local TTS** (Piper): runs fine on CPU, negligible RAM, zero network. Good low-latency baseline.
- **API TTS** (Groq/DeepInfra/ElevenLabs): cloud synthesis, trivial local CPU, needs network + key.
- Total footprint: venv + Whisper model (~460 MB) + Piper voices (~50–100 MB) + server. Fits easily.

**Your toggle test is the right experiment.** Measure first-byte latency + CPU% for local (Piper) vs DeepInfra/Groq vs ElevenLabs to pick the default.

---

## 5. Proposed location & repo/git plan

**Recommendation: do NOT put Lars13 inside `C:\Users\Admin\AppData\Local\hermes\`.**
Hermes' home is Hermes-managed and hot (runtime, `installs/`, `hermes-agent` source, `state.db`, logs, `desktop-plugins/lars`). `jarvis_ai` is a **separate standalone Python app** with its own venv/models/certs. Mixing it into Hermes' home risks the Hermes updater and clutters shared space. It talks to Hermes over the API (`:8642`) — it does not need to live inside Hermes.

**Proposed layout** (dedicated build tree; keep upstream dir structure):
```
C:\Users\Admin\lars13\
├── jarvis_ai\            # the fork (git remote origin -> github.com/AxiomLC/lars13)
│   ├── server\           # FastAPI voice pipeline + HUD
│   ├── client\ worker\ hermes-plugin\   # as upstream
│   └── windows\          # NEW: start/stop/health .cmd, cert, TTS-toggle wrapper
├── lars13.env            # secrets (ELEVENLABS_API_KEY, API_SERVER_KEY, JARVIS_HUD_TOKEN) — gitignored
└── setupREADME.md        # this file
```
(If OneDrive file-locking is a concern, prefer `C:\Users\Admin\Dev\lars13\` over a OneDrive path.)

### Fork / git steps (when terminal is available)
```bash
# Create the empty repo (or via gh) and connect the fork:
gh repo create lars13 --private --fork eadmin2/jarvis_ai --clone=false
# or manual: create empty repo AxiomLC/lars13, then:
git clone https://github.com/AxiomLC/lars13.git
cd lars13
git remote add upstream https://github.com/eadmin2/jarvis_ai.git
git fetch upstream && git merge upstream/main --allow-unrelated-histories
# then apply the Windows port + TTS toggle on a `windows` / `lars13` branch
```

---

## 6. Security model (from upstream — keep)
- Hermes API key never reaches the browser: the HUD proxies through the voice server (allowlist).
- HUD endpoints + dashboard proxy + WebSockets gated by a token (once per device).
- Hermes API + dashboard bind to loopback only.
- `lars13.env` is gitignored.
- LAN-only by design — do not port-forward.

---

## 7. Immediate next steps (tool-dependent)
Restore the `terminal`/`run-code` tools, then:
1. Verify terminal works (with `cd`, no bare `$` — Git Bash golden rule).
2. Clone upstream, init the `lars13` fork, push to `AxiomLC/lars13`.
3. Apply the Windows port (replace `launchd/` + `.sh` with `windows/*.cmd`; TLS cert; venv).
4. Implement the `voice.provider` toggle in `server.py` + `server.example.yaml`.
5. `pip install` into `.venv`, run health check, then live voice-test toggling local vs Groq/DeepInfra.

---
# ADDENDUM 1
## DECIDED — 2026-10-01 16:06 (+02:00)

### TTS provider eval (final) — latency first, price second

Contract facts locked in: `tts_chunks_sync()` yields bytes → `ws.send_bytes()` → browser expects
raw **pcm_s16le @ 16 kHz**. Barge-in = playback can stop the instant bytes are buffered, so the
metrics are first-byte latency + container-free output.

**Chosen (implement in this order, all behind `voice.provider` in `server.yaml`):**

| # | provider | transport | why |
|---|---|---|---|
| 1 | `deepgram` (default) | REST `POST https://api.deepgram.com/v1/speak?model=aura-2-thalia&encoding=linear16&sample_rate=16000&container=none`, `stream=True`, `iter_content` | streams as generated; ~280 ms TTFB measured by Deepgram (docs), claims ~3× faster than ElevenLabs WS; zero conversion code — linear16@16k is exactly the HUD format |
| 2 | `fishaudio` | HTTP `POST https://api.fish.audio/v1/tts` SSE-stream, `format: pcm`, `sample_rate: 16000`, `latency: low`; later upgrade: WS `stream_websocket` fed by LLM token stream | token-by-token WS mode speaks before the LLM sentence finishes — pairs perfectly with the Hermes `assistant.delta` stream |
| 3 | `groq` | OpenAI-compatible `POST https://api.groq.com/openai/v1/audio/speech`, model `playai-tts` (alt `canopylabs/orpheus-v1-english`), `response_format: wav` | cheapest API tier; needs the shared WAV-header-strip shim |
| 4 | `deepinfra` | OpenAI-compatible `POST https://api.deepinfra.com/v1/audio/speech`, model `hexgrad/Kokoro-82M`, `response_format: wav` | dirt cheap; same WAV-strip shim; native stream endpoint `/v1/text-to-speech/{voice}/stream` as extra option |
| 5 | `elevenlabs` | upstream code unchanged | baseline / quality reference |

Out of scope for now: `xai` (`wss://api.x.ai/v1/realtime`, base64 chunk stream — WSS adapter is a stretch goal; REST `/v1/tts` is not realtime-streaming and costs $15/1M chars).

**Local TTS is REJECTED for the default path.** Piper (and local Kokoro via onnxruntime) is
crystal-by-crystal after a full-shot start, not true streaming: no token-in → audio-out incremental
generation, TTFB advantage vanishes on long sentences, and 16 kHz `low` voices sound robotic.
Piper stays as a commented-out fallback provider (`local`) for offline/emergency use only.
Key that stays: `DEEPINFRA_API_KEY` (already present in Hermes `.env`). To be added same place:
`DEEPGRAM_API_KEY`, `FISH_API_KEY` (or `FISHAUDIO_API_KEY`), `GROQ_API_KEY`, plus existing
`ELEVENLABS_API_KEY` / `API_SERVER_KEY` / `JARVIS_HUD_TOKEN`. All commented alternatives live in
`server/config/server.example.yaml` so providers are switched by editing `server.yaml` only.

### Hermes bindings (verified against official docs 2026-10-01)

Connecting to the **'lars' profile** session. Verified in
`hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration`:

- Transport: API-server session stream, `POST {base}/api/sessions/{id}/chat/stream`
  (`Accept: text/event-stream`) — upstream `HermesAPI.chat_stream_events()` already matches.
- `base_url: http://127.0.0.1:8642` (API-server port; `:9119` dashboard is the fallback host,
  same routes). Key header `Authorization: Bearer $API_SERVER_KEY` (+ optional `X-Hermes-Session-Key`).
- Interrupt/approval verbs confirmed: `POST /v1/runs/{id}/stop`, `POST /v1/runs/{id}/approval`.
- Terminal events (`run.completed|failed|cancelled`) carry real `completed/partial/interrupted` flags —
  upstream slicing already handles this.
- OPEN (verify live once Hermes is up): POST body field name, upstream sends `{"input": text}`;
  ADDENDUM 2 example says `{"message": ...`. Accept either / read the 400 message on first run.

### Comparison: existing Lars voice plugin (Hermes desktop plugin)

Prior art on this box: `C:\Users\Admin\AppData\Local\hermes\desktop-plugins\lars\voice\`
(`agent_bridge.py` = WS-JSON-RPC bridge to the dashboard gateway
`ws://127.0.0.1:9119/api/ws?token=lars-voice-bridge-2026`; `voice_server.py` :8000 pipeline;
`stt/tts_service.py`, `vad.py`, `wake_engine.py` with `silero_vad.onnx` + `hey_lars.txt`).

**Status (user-confirmed 2026-10-01): it successfully binds the 'lars' profile session, but did
NOT achieve true streaming with barge-in and simultaneous agent listen/talk.** Treat it as a
reference for session-binding mechanics only (resume → LIVE session id, attach-per-turn lease,
message.delta handling), not as the latency/streaming target — lars13 supersedes it with the
provider streaming adapters above.

Lars-bridge mechanics worth reusing if the API-server ':8642' transport can't bind the profile:
1. `session.most_recent {profile:'lars'}` → stored id → `session.resume` → USE the live id from
   resume result (stored row id = 'session not found'; verified 2026-09-27).
2. Attach-per-turn: resume at turn start, `session.close` at turn end (otherwise the core
   Sessions UI hits SESSION_NOT_OWNED on the same session).
3. `message.delta`/`message.interim` = incremental; `message.complete.text` is the FULL reply —
   only use it when no deltas streamed. 120 s quiet gap = turn over.
4. `session.interrupt` for stop/barge-in of the run; scale-to-zero → resume+retry once.

OPEN (defers to live test once Hermes is up): pick API-server ':8642' (upstream-style, already
coded) vs dashboard WS ':9119' bridge-style for the 'lars' profile binding; settle
`{"input"}` vs `{"message"}` in chat/stream POST body.

Source reference noted: github.com/EKKOLearnAI/ekko-studio (provider adapters + `docs/voice-dialogue.md`
barge-in model — stop playback on capture, never cancel the in-flight run).
Voice chat - Connecting to Profile chat Session:
For connecting an external real-time service (like a custom voice chat pipeline, mobile app, or audio streamer) directly to the **exact profile/agent's active chat stream**, the official reference documentation is the **Programmatic Integration Guide** in the Hermes Agent Developer Documentation.

### Exact Documentation URLs to Give Your Agent

1. **Primary Developer Spec (Programmatic Integration & Streaming API):**
* **URL:** `[https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration)`
* **Use Case:** Explains session-bound streaming endpoints (`/api/sessions/{id}/chat/stream`), the JSON-RPC event bus protocol, and running real-time event bridges.


2. **Session Lifecycle & Handoff Reference:**
* **URL:** `[https://hermes-agent.nousresearch.com/docs/user-guide/sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions)`
* **Use Case:** Details how Hermes manages session persistence, target session ID binding, and cross-surface handoffs.



---

### The Exact Endpoints to Connect Remote Voice Services

When building a voice chat pipeline to tap into an existing session without losing state or tool context, instruct your remote service/agent to use these specific local gateway routes:

#### 1. Real-Time Chat Stream Connection (SSE)

To push user audio transcript turns and stream back the agent's real-time tokens:

* **Endpoint:** `POST [http://127.0.0.1:9119/api/sessions/](http://127.0.0.1:9119/api/sessions/){session_id}/chat/stream`
* **Headers:** `X-Hermes-Session-Token: <TOKEN>` or `Authorization: Bearer <KEY>`
* **Payload:**
```json
{
  "message": "User spoken transcript text here",
  "stream": true,
  "session_id": "your_exact_active_session_id"
}

```


* **Event Stream Outputs:** Emits `assistant.delta` (for streaming TTS generation as tokens land) and terminal events like `assistant.completed`.

#### 2. Standard OpenAI-Compatible Endpoint (Alternative)

If your voice service relies on standard OpenAI SDK wrappers:

* **Endpoint:** `POST [http://127.0.0.1:9119/v1/chat/completions](http://127.0.0.1:9119/v1/chat/completions)`
* **Header:** Pass `X-Session-Id: <session_id>` to ensure it binds to the exact profile's active session history rather than creating a stateless conversation.

---
# ADDENDUM 3 — STATE OF PLAY — 2026-10-01 ~19:30 (+02:00)

## Process map: who spawns what, and who owns which port

Verified live by process inspection (`wmic` + netstat) on this box:

```
Power on / login
 ├─ Hermes Desktop (Electron, Hermes.exe parent + renderer/gpu/audio children)   [GUI only]
 │    ├─ spawns: hermes.exe serve --host 127.0.0.1 --port 0     <- agent backend, RANDOM port
 │    │           (63020 once, 60126 after respawn; headless, NO platforms, respawns on demand,
 │    │            'Gateway ready' badge in desktop refers to this socket — NOT :8642)
 │    └─ spawns: hermes.exe dashboard                            <- :9119 dashboard UI
 │                (dies if backend restarts; NOT reliably respawned — start manually when missing)
 │
 ├─ (required for lars13, manual/install): hermes gateway run|start  <- THE gateway process
 │    └─ loads ALL platforms per profile .env:
 │         telegram/discord/... + api_server platform  -> 127.0.0.1:8642  (lars13's LLM target)
 │
 └─ lars13 voice pipeline (windows/start.cmd)
      ├─ :443    https/wss  HUD (browser mic needs this TLS port)
      ├─ :8765   ws        push-to-talk client (plain)
      ├─ :8766   https/wss  HUD alt port
      ├─ :9443   https      dashboard TLS proxy (HUD iframes Hermes dashboard :9119
      │                     through it — token-gated)
      └─ STT: faster-whisper small.en int8 on CPU; logs -> server/logs/latency.jsonl
```

Key facts learned 2026-10-01:
- Desktop chat works WITHOUT the gateway: desktop window -> hermes serve backend over internal
  IPC. That is why "Gateway ready" shows while :8642 is dead.
- The API server is a **gateway platform**, enabled per-profile env (`API_SERVER_ENABLED=true`,
  `API_SERVER_KEY` in the profile's `.env` — for the 'lars' profile that is
  `AppData/Local/hermes/profiles/lars/.env`, which currently LACKS them; root `~/.hermes/.env` has
  them set). On gateway start it prints `[API Server] API server listening on http://127.0.0.1:8642`.
- `hermes gateway status` on this box showed all gateways DOWN + stale gateway_state.json
  (ungraceful prior shutdown). FIX: `hermes gateway install` + `hermes gateway start` (scheduled
  task, survives reboot) or run `hermes gateway run` in a permanent window.
- Browser mic requires TLS secure origin -> cert.pem/key.pem in server/certs/ (made by
  windows/make-certs.ps1; pfx->pem conversion via Git Bash openssl). Cert trusted in CurrentUser
  Root store (valid to ~2031).
- If all four lars13 ports are NOT listening after start.cmd, the pem files are missing and
  server.py silently fell back to plain HTTP :8765 only.

## What is expected to work NOW (after: gateway up + dashboard up + start.cmd)

1. HUD loads at https://localhost/hud/ (cert trusted; JARVIS_HUD_TOKEN prompt once).
2. Voice turn end-to-end: mic -> STT (local whisper) -> POST :8642
   /api/sessions/{id}/chat/stream -> 'lars' profile session streams reply -> per-sentence TTS ->
   browser playback. VERIFIED WORKING in turn 2 (transcript -> run created -> reply).
3. Typed chat box shares the same 'lars' session.
4. Kanban/dashboards via :9443 proxy when :9119 is up.

## Known issues / work in progress

- STT finalize ~20 s on first utterance (small.en int8, cold). Switch stt.model to base.en in
  server.yaml for ~3-6 s; tune after TTS works.
- LLM time-to-first-token ~13 s (agent thinking/tools) — Hermes-side; watch, don't fix yet.
- Deepgram default model aura-2-thalia -> HTTP 403 INSUFFICIENT_PERMISSIONS on this project
  (project lacks Aura-2). INTERIM: set voice.model: aura-asteria-en (Aura-1) in server.yaml, or
  enable Aura-2 in Deepgram console. The 403 surfaced as generic HTTP 4xx text.
- HUD /v1/skills panel -> Hermes returns 500 "Failed to enumerate skills" (Hermes-side bug/quirk,
  not lars13; proxy verified faithful). Cosmetic only.
- Dashboard :9119 must be alive for kanban viewers; starts with desktop, does not auto-heal.
- Open questions closed in live test turn 2: POST body key {"input"} ACCEPTED by :8642 chat/stream
  (ADDENDUM 2's "message" example was wrong for API-server transport); profile binding works
  with API_SERVER_KEY bearer over :8642; sessions cached in server/logs/hermes_sessions.json.

## First-byte latency experiment (the POINT of the fork)

After Deepgram model is fixed: speak identical test phrase per provider, compare
`time_to_first_tts_audio_byte_seconds` in server/logs/latency.jsonl:

1. deepgram aura-asteria-en (baseline cloud)
2. fishaudio (latency: low)
3. groq playai-tts / deepinfra Kokoro-82M (wav-strip path — VERIFIES the WAV shim with real audio)
4. elevenlabs (reference) — optional, needs voice_id
5. local piper (fallback proof — expect worst streaming, best offline)

## Second-machine worktree instructions (for the other machine)

```bash
gh repo clone AxiomLC/lars13
cd lars13
git remote add upstream https://github.com/eadmin2/jarvis_ai.git   # reference only
git fetch upstream
# windows/ scripts assume Git Bash openssl + uv; else port. Keys: copy a filled .env from this
# machine (never commit it). Hermes must run the gateway for :8642 — same layout as above.
```

Branching convention going forward: work on feature branches (`windows`, `tts-providers`,
`stt-tuning`), merge to main when a turn logs clean in latency.jsonl.
