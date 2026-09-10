# Qwen Chat — CUTM AI Gateway Frontend

A lightweight, vanilla JavaScript chat UI for the CUTM AI Gateway. It talks directly to the gateway's `/api/generate` and `/health` endpoints — no build step, no framework, no dependencies.

## Features

- **Multi-model support** — switch between `Auto` (intent-based routing), `mistral:7b` (chat/coding/reasoning), and `qwen2.5vl:7b` (vision).
- **Conversation history** — multiple conversations stored locally, with a sidebar to switch between them.
- **Live connection status** — periodic health checks against the gateway with a visual indicator.
- **Export** — download any conversation as JSON or plain text.
- **Keyboard shortcuts** — `Enter` to send, `Shift+Enter` for a new line, `?` to toggle the shortcuts panel.
- **Responsive layout** — usable on desktop and mobile.

## Project structure

```
index.html          Markup and layout
css/
  main.css          Base theme and layout styles
  chat.css          Chat/message-specific styles
  responsive.css     Mobile/responsive breakpoints
js/
  config.js         Gateway URL resolution, model list, storage keys, limits
  api.js            Fetch wrappers for /api/generate and /health
  chat.js           Message rendering and chat flow
  conversations.js  Conversation persistence (localStorage) and sidebar list
  utils.js          Shared helper functions
  main.js           App entry point, wires up DOM events
```

## Running locally

This is a static site — serve the `Frontend` directory with any static file server, for example:

```bash
npx serve .
```

Then open the printed URL in your browser.

## Gateway connection

The frontend auto-detects which gateway to talk to (see [`js/config.js`](js/config.js)):

1. A custom URL saved in `localStorage` under `aig_gateway_url` (highest priority).
2. If served from port `8000`, it uses the current page's origin.
3. If served from `localhost`/`127.0.0.1`, it defaults to `http://localhost:8000`.
4. Otherwise it falls back to the university network gateway at `http://172.16.8.4:8000`.

To point the app at a different gateway, set the override in the browser console:

```js
localStorage.setItem('aig_gateway_url', 'http://your-gateway-host:8000');
```

## Configuration

Key settings live in [`js/config.js`](js/config.js):

- `MODEL_OPTIONS` — available models shown in the model selector.
- `MAX_TOKENS` — per-intent token limits (`chat`, `coding`, `vision`, `reasoning`).
- `CONNECTION_CHECK_MS` — how often the health check runs.
- `MAX_MESSAGE_LENGTH`, `MAX_MESSAGES_PER_CONVERSATION`, `MAX_CONVERSATIONS` — client-side limits for input and stored history.
