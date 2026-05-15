# Adminbot Phase 2 UI Plan

## Framework Decision

Use `FastAPI + Jinja2 + Uvicorn`.

## Why This Stack

- The repo already uses `FastAPI` for `nanobot/web`.
- The optional `web` dependency set in `pyproject.toml` already includes:
  - `fastapi`
  - `uvicorn`
  - `jinja2`
- Reusing the same stack keeps deployment, debugging, and developer onboarding simpler.
- We can borrow patterns from `nanobot/web/app.py` instead of inventing a parallel web architecture.

## Scope For Phase 2

Phase 2 should add one shared manager UI for local operators.

Phase 2 does not need to include:

- interactive PTY terminal tabs
- iframe embedding of each bot dashboard by default
- remote/multi-user hosting concerns beyond safe local defaults

## UI Goals

- show all bots in one place
- allow create/start/stop/restart actions
- expose health/status clearly
- show recent logs without opening extra terminals
- provide links to each bot dashboard

## Recommended Package Layout

```text
adminbot/
  app/
    __init__.py
    main.py
    manager.py
    paths.py
    process_manager.py
    registry.py
    runtime_config.py
    utils.py
    web/
      __init__.py
      app.py
      auth.py
      cli.py
      viewmodels.py
      routes/
        __init__.py
        dashboard.py
        bots.py
        logs.py
        api.py
      templates/
        base.html
        dashboard.html
        bot_detail.html
        create_bot.html
        log_viewer.html
      static/
        adminbot.css
        adminbot.js
```

## Runtime Model

- `adminbot` web server runs as one local process
- it uses the existing Phase 1 `AdminbotManager`
- `AdminbotManager` is created once in `create_app()` and stored in `app.state.manager`
- route handlers access the shared manager via `request.app.state.manager`
- HTTP handlers call manager methods for bot lifecycle actions
- route handlers should use `def` (sync), not `async def`, so FastAPI can offload blocking manager work to its threadpool
- templates render server-side HTML first
- small JS can progressively enhance refresh/actions later

## Route Proposal

### HTML Pages

- `GET /`:
  - manager dashboard with bot table/cards
- `GET /bots/new`:
  - create bot form
- `GET /bots/{bot_id}`:
  - bot detail page
- `GET /bots/{bot_id}/logs`:
  - log viewer page

### Action Endpoints

- `POST /bots`:
  - create bot
- `POST /bots/{bot_id}/start`
- `POST /bots/{bot_id}/stop`
- `POST /bots/{bot_id}/restart`

Action endpoints should follow PRG:

- perform action
- redirect to `GET /bots/{bot_id}` or another safe GET page
- show result/error message after redirect

### JSON Endpoints

- `GET /api/bots`
- `GET /api/bots/{bot_id}`
- `GET /api/bots/{bot_id}/logs?stream=stdout|stderr&tail=200`

Log viewer JSON should return an array of lines/strings to keep the JS side simple.

## UI Structure

### Dashboard

- summary cards:
  - total bots
  - running bots
  - stopped bots
- bot table:
  - name
  - status
  - workspace
  - web port
  - pid
  - last updated
  - actions

### Bot Detail

- identity and runtime metadata
- dashboard link
- last known pid/process info
- action buttons
- recent stdout/stderr preview

### Log Viewer

- stdout/stderr toggle
- tail view first
- simple auto-refresh button or interval

## Design Direction

Keep the visual language close to `nanobot/web` so the experience feels related, but simplify it:

- reuse `Jinja2` templates
- keep dark/operator-centric styling
- avoid copying the whole Nanobot dashboard shell unless needed
- prioritize dense operational visibility over decorative effects

## Security Defaults

- bind to `127.0.0.1` by default
- no auth required for local-only default mode
- if later exposed beyond localhost, add auth before recommending that setup

## Technical Notes To Carry Into Phase 2

- fix Windows `stop()` false-positive when process exits between identity check and `taskkill`
- preserve correct `updated_at` semantics during batch refresh
- decide whether manager log polling is enough or whether SSE/WebSocket is worth it
- keep file access read-only for logs in the UI layer
- `adminbot/app/web/cli.py` must bind `127.0.0.1`, not `0.0.0.0`
- do not copy the host binding from `nanobot/web/cli.py`

## Routing Notes

- Register `GET /bots/new` before `GET /bots/{bot_id}` so `"new"` is not captured as a bot id.

## Recommended First Implementation Order

1. add `adminbot.app.web.app`
2. add dashboard route and template
3. show bot list with start/stop/restart actions
4. add create bot form
5. add log viewer
6. add JSON endpoints only where the UI benefits from them
