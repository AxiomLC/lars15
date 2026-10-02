# Lars15 — Voice + HUD for Hermes Agent, on LiveKit WebRTC

Successor of [`AxiomLC/lars13`](https://github.com/AxiomLC/lars13) with the same
mission — talk to a local [Hermes Agent](https://github.com/NousResearch/hermes-agent)
through an Iron-Man-style browser HUD — rebuilt for **fewer moving parts**:

- **One main server** (FastAPI, this repo's `server/`) — HUD, auth, Hermes bridge.
- **LiveKit WebRTC** transports the audio instead of bespoke ws/PCM loops:
  true streaming, native barge-in, mic without TLS-cert juggling.
- **Dual engine:** LOCAL (local LiveKit server + whisper base.en + kokoro-onnx)
  or CLOUD (LiveKit Cloud + Groq STT + Deepgram/Fish/Groq/DeepInfra TTS).
- **TTS provider toggle** carried over from lars13 (deepgram default — see lars13
  DECIDED notes for the latency/price evaluation).
- **Hermes bridge:** same verified contract — standalone gateway on `127.0.0.1:8642`,
  'lars' profile session, `POST /api/sessions/{id}/chat/stream` with `{"input": ...}`.

## Build

**Read `LIVEKIT_REFACTOR_SPEC.md` first** — it is the implementation briefing:
config, token endpoint, `server/agent_worker.py`, HUD swap, roadmap, open items.

Layout is inherited from lars13 (`server/`, `server/hud/` single-file HUD,
`client/`, `worker/`, `hermes-plugin/`, `docs/`). `windows/` scripts come over
from lars13 as the porting baseline; `setupREADME.md` is lars13's history/briefing
(kept for context — the verified transports and lessons live there).

Keys go in `.env` (gitignored): `API_SERVER_KEY`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, plus whichever TTS/STT provider keys you use.

## Status

- [x] Repo created, seeded from lars13
- [x] LiveKit refactor spec committed
- [ ] LiveKit deps in uv venv; local LiveKit server on Windows verified
- [ ] `/api/voice/token` + `agent_worker.py`
- [ ] HUD LiveKit client swap
- [ ] Latency bake-off vs lars13 numbers
