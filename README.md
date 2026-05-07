# DSV4本地中转工具1.0

Minimal Python adapter for:

`Codex -> cc-switch -> this adapter -> DeepSeek`

## Window app

If you want a desktop window instead of hand-editing env vars, start:

```powershell
.\.venv\Scripts\python.exe .\window_app.py
```

The window lets you:

- fill only the upstream base URL, upstream model, and optional API key
- start or stop the local adapter on fixed local address `http://127.0.0.1:9468/v1`
- test the upstream connection
- one-click launch the official CC Switch import flow for the local provider
- apply Codex config directly
- check updates when the app starts, compare versions by scanning the configured download page, and show a download link when a newer package is found

Defaults now follow the current DeepSeek official docs:

- upstream base URL: `https://api.deepseek.com`
- upstream endpoint path: `/chat/completions`
- default model: `deepseek-v4-pro`

The GUI stores normal fields in `window_config.json`, including the upstream API key.

## Update check

The desktop app version is currently:

`DSV4本地中转工具1.0`

When the window starts, it can open a configured download page, scan the root page for entries containing:

`DSV4本地中转站`

Then it extracts the version number from the matching file name or link text and compares it with the current version. If it finds a higher version, it shows a prompt and exposes the download link so the user can download the update manually.

Configure the update source URL with environment variable:

```powershell
$env:DSV4_UPDATE_SOURCE_URL = "https://your-pan-root-page"
```

If this variable is empty, startup update check is skipped.

The adapter exposes:

- `GET /v1/models`
- `POST /v1/responses`
- `POST /v1/chat/completions` (non-stream passthrough for debugging)

The first version keeps the scope intentionally small:

- accept OpenAI-style `responses` requests
- translate them to upstream `chat/completions`
- wrap the upstream answer back into a `responses` object
- synthesize `responses` SSE events when `stream=true`

## Quick start

1. Create a virtual environment:

```powershell
python -m venv .venv
```

2. Install dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

3. Set upstream environment variables:

```powershell
$env:UPSTREAM_BASE_URL = "https://api.deepseek.com"
$env:UPSTREAM_CHAT_PATH = "/chat/completions"
$env:UPSTREAM_MODEL = "deepseek-v4-pro"
$env:ADAPTER_MODEL_IDS = "deepseek-v4-pro"
```

Optional:

```powershell
$env:UPSTREAM_API_KEY = "your-upstream-key"
```

4. Start the adapter:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 9468
```

## Point cc-switch at the adapter

Use the window app's `导入到 CC` button to launch CC Switch's official `ccswitch://v1/import?...` flow.

The imported local provider points to:

`http://127.0.0.1:9468/v1`

CC Switch still shows its own confirmation dialog, which is expected. After confirming there, keep Codex pointed at cc-switch as usual.

## Smoke checks

List models:

```powershell
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:9468/v1/models" | Select-Object -ExpandProperty Content
```

Create a non-stream response:

```powershell
$body = @{
  model = "deepseek-v4-pro"
  input = "Say hello."
} | ConvertTo-Json

Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:9468/v1/responses" -Method POST -ContentType "application/json" -Body $body | Select-Object -ExpandProperty Content
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Notes

- DeepSeek official docs currently document OpenAI-compatible `chat/completions`, not `/responses`, so this adapter keeps translating Codex `responses` calls into upstream `chat/completions`.
- CC Switch import now uses its official deeplink interface instead of writing CC config files or the CC database directly.
- The adapter maps Codex-style higher reasoning values to DeepSeek-compatible `reasoning_effort` values. For example, `xhigh` is translated to `max`.
- `stream=true` is synthesized from a completed upstream chat response in this first version.
- function tools are translated to chat-completions tools directly.
- non-function tools are wrapped into a single-string function schema so the chain can be tested earlier.
- if the upstream model does not support tool calling reliably, Codex may connect but still behave poorly during real tool use.
