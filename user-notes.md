HUD display - super cool for Lars-type UI
**`hermes-plugin/hud_display`** is a custom tool plugin for Hermes Agent that gives the AI direct control over the browser HUD UI.

It allows the agent to programmatically summon, position, or clear "holographic" media panels (like videos, images, web pages, or charts) on your screen during a conversation (e.g., when you ask *"show me a video of X on screen"*), sending display payloads straight to the HUD frontend.

A custom browser UI (like `lars13`) can directly query and render your custom Hermes plugins by consuming the **Hermes Gateway API (`[http://127.0.0.1:8642](http://127.0.0.1:8642)`)** without ever launching or running the resource-heavy Electron Desktop GUI.

---

### How the Architecture Components Divide

| Component | Port | Role in Plugin Ecosystem | Is it needed for a custom WebUI? |
| --- | --- | --- | --- |
| **Hermes Gateway API** | `:8642` | **The Core Backend Engine.** Executes agent turns, runs tool/plugin code (fetching stocks, Google Sheets, social APIs), and exposes REST endpoints. | **REQUIRED** (Runs via `hermes gateway run` or background service) |
| **Hermes Web UI Proxy** | `:9119` | The local web server that hosts the standalone dashboard pages and embedded plugin iframe components. | **OPTIONAL** (Lightweight background service) |
| **Hermes Desktop GUI** | N/A | Electron shell wrapper around the dashboard. | **NOT NEEDED** (Saves 500MB+ RAM) |

---

### How Custom Plugins Work in a Light Client (e.g., `lars13`)

Hermes plugins are essentially **Python tool definitions + execution handlers**. When the agent calls a plugin (e.g., `get_stock_chart` or `pull_sheets_db`), the execution flow works as follows:

1. **Tool Invocation via Gateway API (`:8642`):**
When your browser UI sends a prompt over WebSockets or REST to `:8642`, the Hermes Python gateway runs the tool logic locally (making third-party API calls, fetching data, processing JSON).
2. **Data & HUD Payload Emission:**
The plugin returns structured data (JSON, HTML fragments, or image/video URLs) back to the Hermes session loop. If the plugin uses the HUD display protocol (like `hermes-plugin/hud_display`), it emits an event payload over the proxy/event stream.
3. **Rendering on Your Custom WebUI:**
Your custom browser UI captures these tool events from the `:8642` event feed and renders them directly into your own vanilla JS panels or WebSockets without needing the official React dashboard at all.

---

### Benefits of Running "Backend Only" for `lars13`

* **Resource Savings:** The Hermes Electron app consumes significant system resources (often 400MB–800MB RAM + GPU acceleration overhead). Running only `hermes gateway run` keeps background memory consumption under **~80MB–120MB**.
* **Zero Dependency on Port `:9119`:** If you are rendering stock graphs or social stats directly in your custom HUD panels (via libraries like Chart.js, Plotly, or HTML templates in `server/hud/index.html`), you can bypass the official dashboard entirely.
* **Direct Event Bridge:** Your LiveKit / FastAPI server proxies requests directly to `:8642`, keeping latency as low as possible for barge-in audio and tool updates.

---

1. **Start Hermes API Backend Only:**
Launch the headless gateway in background/services mode without starting the Electron app:

```powershell
hermes gateway run

```


2. **Deploy Custom Plugin Code:**
Place your custom Python plugin files into `~/.hermes/plugins/` (or the local Hermes plugin root directory).


3. **Consume Tool Events in Custom UI:**
In `lars13`, listen to tool execution frames on `[http://127.0.0.1:8642/v1/sessions/lars](http://127.0.0.1:8642/v1/sessions/lars)` to catch custom plugin outputs and render them inside your HUD panels.


---
