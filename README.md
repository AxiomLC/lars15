# Lars13 — Voice + HUD for Hermes Agent (Windows)

A self-hosted, Iron-Man-style voice assistant and command center for
[Hermes Agent](https://github.com/NousResearch/hermes-agent), ported to run on
**Windows 10** against a **local** Hermes instance. Forked from
[`eadmin2/jarvis_ai`](https://github.com/eadmin2/jarvis_ai) (upstream, macOS).
Talk to a *real* agent — persistent memory, terminal access, web search, file
tools, skills — through the arc-reactor HUD in any browser on your LAN, or the
push-to-talk client.

**The key custom feature:** a **TTS provider toggle** — switch between
Deepgram, Fish Audio, Groq, DeepInfra, ElevenLabs, or a local fallback engine
by editing one line of config. Built for a latency-first experiment (lowest
first audio byte so barge-in stays snappy), with price as the tiebreaker.

## What it does

Click the ring and speak. Your words transcribe **live on screen**. The
transcript goes to the **'lars' profile session** in the local Hermes Agent,
which actually *does things* — runs commands, searches, remembers — and the
reply streams back as speech, sentence by sentence, while the rest is still
being generated.

- **Live agent activity** — tool calls with command previews
- **STOP / barge-in** — halt or cut off the agent mid-sentence
- **Approval cards** — dangerous commands pause for ALLOW/DENY
- **Embedded dashboards** — Hermes kanban and session browser in animated viewers
- **HUD media panels** — agent-driven video/image panels via the bundled
  `hermes-plugin/hud_display`
- **Usage tracking** — tokens, turns, TTS character counts
- **Privacy filter** — secret-shaped strings redacted before text reaches
  cloud TTS
- **Cinematic boot** — press `B`

## TTS providers (the toggle)

All adapters live in `server/server.py` behind `voice.provider` in
`server/config/server.yaml`. Every adapter yields raw **pcm_s16le @ 16 kHz**
to the WebSocket playback path — barge-in behavior is provider-independent.

| `voice.provider` | Transport | Notes |
|---|---|---|
| `deepgram` (**default**) | REST `/v1/speak`, raw `linear16@16k`, streams as generated | fastest guaranteed first byte; no header stripping |
| `fishaudio` | HTTP `/v1/text-to-speech`, `pcm`, `latency: low` | WS token-feed upgrade path |
| `groq` | OpenAI-compatible `/v1/audio/speech`, WAV → header-stripped | cheapest API tier |
| `deepinfra` | OpenAI-compatible `/v1/audio/speech` (Kokoro-82M), WAV → header-stripped | cheapest quality option |
| `elevenlabs` | upstream streaming code, unchanged | quality baseline |
| `local` | Piper (fallback **only**) | not true streaming; offline/emergency use |

Keys live in `lars13/.env` (gitignored; auto-loaded). See
[setupREADME.md](setupREADME.md) **DECIDED** for the full evaluation and
date-stamped design decisions.

## Architecture

```
 Browser HUD (any LAN device)          Host (this Windows box)
 ── https/wss :443 ──────────┐   ┌──────────────────────────────────────┐
   mic · speaker · panels    ├──►│ voice pipeline server (this repo)    │
                             │   │  STT: faster-whisper (local, free)   │   ┌──────────────────┐
 Push-to-talk client         │   │  TTS: provider toggle (see above)    ├──►│ Hermes Agent      │
 ── ws :8765 ────────────────┘   │  HUD + auth + dashboard TLS proxy    │   │  API  :8642 (lo)  │
                                 └──────────────────────────────────────┘   │  Dash :9119 (lo)  │                                                                             │  memory · tools   │
                                                                            └──────────────────┘
```

Voice and typed chat share one persistent Hermes session (the `lars` profile's
session), so each knows what you said to the other.

## Requirements (this box, verified)

- Windows 10, Python 3.13 (venv via **uv**), `uv` on PATH
- [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) installed —
  API server on `127.0.0.1:8642`, dashboard on `127.0.0.1:9119`
- Keys in `.env`: `API_SERVER_KEY`, `JARVIS_HUD_TOKEN`, and any of
  `DEEPGRAM_API_KEY`, `FISH_API_KEY`, `GROQ_API_KEY`,
  `DEEPINFRA_API_KEY`, `ELEVENLABS_API_KEY` (only the selected provider's key
  is required)
- Any modern browser on the LAN (TLS required for mic access)

## Install (Windows)

```powershell
# 1. venv
uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe fastapi uvicorn requests pyyaml `
    numpy anthropic websockets psutil RealtimeSTT faster-whisper silero-vad

# 2. config
Copy-Item server\config\server.example.yaml server\config\server.yaml
#   edit voice.provider + provider settings; put keys in .env

# 3. TLS (self-signed + user-store trust; browser mic needs it)
powershell -ExecutionPolicy Bypass -File windows\make-certs.ps1

# 4. run
powershell -File windows\start.cmd          # or: .venv\Scripts\python.exe server\server.py
# open https://localhost/hud/ -> accept cert -> JARVIS_HUD_TOKEN -> talk
```

First start downloads Whisper `small.en` (~460 MB) and warms up for 60–90 s.

## Operations (Windows)

| Task | Command (PowerShell) |
|---|---|
| Start | `windows\start.cmd` |
| Stop (kills uvicorn ports) | `windows\stop.cmd` |
| Health | `windows\health.cmd` |
| TTS provider switch | edit `server\config\server.yaml` `voice.provider` → restart |
| Temp provider override | `set LARS13_TTS=<provider>` then `start.cmd` |

Upstream macOS files (`launchd/`, `server/scripts/*.sh`) are kept for
reference; `windows/` supersedes them on this box. Auto-start: Task Scheduler
pointing at `windows\start.cmd` (NSSM optional).

## Repo layout

```
server/          FastAPI voice pipeline + HUD host (the core)
server/hud/      single-file HUD (vanilla JS, no build step)
server/scripts/  upstream .sh helpers (reference)
windows/         this fork's start/stop/health/make-certs for Windows
client/          optional push-to-talk client (wake word capable)
worker/          optional GPU sidecars
hermes-plugin/   agent tool plugin: summon/dismiss HUD media panels
launchd/         upstream macOS auto-start (reference only)
docs/            upstream SETUP, ARCHITECTURE, TROUBLESHOOTING
setupREADME.md   the research/build briefing + DECIDED section (frequently updated)
```

## Status / current work

- [x] Repo forked & connected: `AxiomLC/lars13` (upstream = `eadmin2/jarvis_ai`; unrelated histories merged)
- [x] TTS provider toggle implemented (compile-clean, per-provider adapters)
- [x] Windows scripts + venv (uv) + TLS certs + smoke boot verified (all four ports listen)
- [x] Voice turn end-to-end verified: 'lars' profile session via `:8642` streams a reply
- [ ] Deepgram model fix (project lacks Aura-2 → use `aura-asteria-en` or enable Aura-2)
- [ ] First-byte latency bake-off across providers on this hardware
- [ ] STT speed tuning (`base.en` vs `small.en` — first utterance ~20 s is too slow)
- [ ] Hermes-side: `/v1/skills` enumeration 500 (HUD panel cosmetic; Hermes bug)

## Troubleshooting (Windows box)

| Symptom | Check / fix |
|---|---|
| `https://localhost/hud/` unreachable, only `:8765` in log | cert.pem/key.pem missing in `server/certs/` → re-run `windows\make-certs.ps1` (or convert pfx via Git Bash openssl); server silently falls back to plain HTTP |
| Turn error: connection refused `127.0.0.1:8642` | Hermes **gateway** process not running → `hermes gateway run` (or `install`+`start`). Note: "Gateway ready" inside the Hermes desktop app is its internal backend — NOT the API server |
| Dashboard viewers dead; `/api/hermes/*` 500s | dashboard `:9119` down → `hermes dashboard` (desktop spawns but doesn't auto-heal it) |
| Deepgram HTTP 403 `INSUFFICIENT_PERMISSIONS` | project lacks Aura-2 → set `voice.model: aura-asteria-en` |
| Skills panel empty + console 500 | Hermes `Failed to enumerate skills` — Hermes-side; ignore |
| Slow first turn | Whisper small.en cold start (~20 s finalize); switch `stt.model: base.en` |

## Security model

- Hermes API key never reaches the browser: the HUD talks through a strict
  allowlist proxy on the voice server.
- HUD endpoints + dashboard proxy + WebSockets gated by `JARVIS_HUD_TOKEN`.
- Hermes API + dashboard bind to loopback only.
- Secret-shaped strings redacted before text leaves for cloud TTS.
- LAN-only by design — do not port-forward.

## Credits & license

Forked from [`eadmin2/jarvis_ai`](https://github.com/eadmin2/jarvis_ai) —
thanks for the pipeline and HUD. Built on
[Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
STT by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) /
[RealtimeSTT](https://github.com/KoljaB/RealtimeSTT). TTS by whichever
provider you toggle: Deepgram, Fish Audio, Groq, DeepInfra, or ElevenLabs.

MIT — see [LICENSE](LICENSE). Use it, fork it, build your own Jarvis.
