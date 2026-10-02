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

# ADDENDUM 2 OCt 2026 10:45am
re:the HUD display:
**Hardcoding a static whitelist (manifest)** of allowed HUD items—and exposing it directly to both the LLM's system prompt and the UI panel—is significantly cleaner, faster, and virtually bulletproof against rendering errors.

Instead of expecting the LLM to invent arbitrary HTML or guess parameters, the LLM simply picks an **Item ID** from a fixed list, and your UI renders the pre-tested component.

---

### Why This Architecture Wins

1. **Zero Render Errors:** The HUD frontend only ever loads verified, pre-tested components, static URLs, or stripped `iframe` embeds.
2. **Deterministic LLM Calls:** The LLM's system prompt doesn't need complex parameter instructions—it just learns: *"When user asks for X, execute `hud_display(item_id='x')`"*.
3. **Double Visibility:** The user can trigger items via voice/chat OR click them directly from a scrollable UI drawer.

---

### 1. The HUD Manifest (`server/hud/manifest.json`)

Maintain a single JSON file that defines every allowed item, its voice trigger aliases, and how the HUD renders it:

```json
{
  "items": [
    {
      "id": "stock_chart",
      "title": "Stock Market Ticker",
      "type": "iframe",
      "url": "https://s.tradingview.com/widgetembed/?symbol=AAPL",
      "position": "right",
      "aliases": ["stocks", "market", "trading view", "share prices"]
    },
    {
      "id": "sheets_db",
      "title": "Project Database",
      "type": "iframe_stripped",
      "url": "https://docs.google.com/spreadsheets/d/e/2PACX.../pubhtml?widget=true&headers=false",
      "position": "left",
      "aliases": ["sheets", "database", "spreadsheet", "records", "data table"]
    },
    {
      "id": "hermes_kanban",
      "title": "Hermes Task Board",
      "type": "iframe",
      "url": "http://127.0.0.1:9119/kanban",
      "position": "right",
      "aliases": ["tasks", "kanban", "todo list", "hermes board"]
    }
  ]
}

```

---

### 2. Streamlined `hud_display` Tool Schema

Because the manifest handles the URLs and render types, your Python tool payload becomes dead simple:

```yaml
# hermes-plugin/hud_display/plugin.yaml
name: hud_display
description: "Display an allowed visual module on the HUD or clear active panels."
parameters:
  type: object
  properties:
    action:
      type: string
      enum: ["show", "clear"]
    item_id:
      type: string
      description: "The ID from the allowed HUD manifest (e.g., 'stock_chart', 'sheets_db', 'hermes_kanban')."
  required: ["action"]

```

---

### 3. Lars System Prompt Injection

In `~/.hermes/profiles/lars.yaml` (or system instructions), give Lars the exact list and fuzzy-matching guidance:

```yaml
system_prompt: |
  You are Lars, an AI voice assistant with a visual HUD.
  
  ALLOWED HUD MODULES:
  - 'stock_chart': Stocks, market graphs, share prices.
  - 'sheets_db': Spreadsheet, project database, data tables.
  - 'hermes_kanban': Task board, kanban, todos.
  
  INSTRUCTIONS:
  1. When the user asks to see, show, open, or view any of these topics (or similar synonyms), call `hud_display(action='show', item_id='<matching_id>')`.
  2. If the user asks to close, clear, or clean the screen, call `hud_display(action='clear')`.
  3. Do NOT attempt to show unlisted modules or raw arbitrary URLs.

```

---

### 4. Client Side Renderer + Scrollable Drawer (`server/hud/index.html`)

On the frontend, load the manifest once. Use it to build both the **voice event handler** and the **scrollable UI panel**:

```javascript
// Load HUD Manifest
let manifest = [];
fetch('/hud/manifest.json')
  .then(res => res.json())
  .then(data => {
    manifest = data.items;
    buildScrollableDrawer(manifest);
  });

// Handle LLM Tool Call
function handleHudToolCall(itemId, action) {
  if (action === 'clear') {
    document.getElementById('hud-active-container').innerHTML = '';
    return;
  }

  const item = manifest.find(i => i.id === itemId);
  if (!item) return; // Ignores invalid calls, preventing HUD crashes!

  renderModule(item);
}

// Render Module
function renderModule(item) {
  const container = document.getElementById('hud-active-container');
  container.innerHTML = `
    <div class="hud-panel hud-${item.position} p-2 bg-black/90 border border-cyan-500/50 rounded">
      <div class="flex justify-between border-b border-cyan-500/30 pb-1 mb-2">
        <span class="text-xs font-bold text-cyan-400">${item.title}</span>
        <button onclick="this.parentElement.parentElement.remove()" class="text-red-400 text-xs">✕</button>
      </div>
      <iframe src="${item.url}" class="w-full h-80 border-0"></iframe>
    </div>
  `;
}

// Build Scrollable Left/Right Drawer for Manual Clicks
function buildScrollableDrawer(items) {
  const drawer = document.getElementById('hud-sidebar-drawer');
  drawer.innerHTML = items.map(item => `
    <button onclick='renderModule(${JSON.stringify(item)})' 
            class="w-full text-left p-2 my-1 text-xs bg-cyan-950/40 hover:bg-cyan-800/50 border border-cyan-500/30 rounded text-cyan-300">
      ▶ ${item.title}
    </button>
  `).join('');
}

```

---

### Developer Workflow Going Forward

Whenever you want to add a new visual tool or site:

1. Open `server/hud/manifest.json`.
2. Add a new JSON object with an `id`, `title`, `url`, and keywords.
3. Done—Lars instantly knows how to call it via voice, and it immediately appears in your scrollable HUD sidebar.
