# Adminbot

`adminbot` is a portable local manager for running and supervising multiple Nanobot instances from one control surface.

## Goals

- avoid one visible terminal window per bot
- keep bot definitions and runtime state in one place
- provide one operator-facing control UI
- stay easy to copy into another Nanobot repo

## Layout

```text
adminbot/
  __init__.py
  PLAN.md
  CHECKLIST.md
  HANDOFF_CHECKLIST.md
  PHASE2_UI_PLAN.md
  PHASE4_TERMINAL_PLAN.md
  README.md
  launcher.cmd
  launcher.ps1
  app/
    __init__.py
    main.py
    manager.py
    paths.py
    runtime_config.py
    process_manager.py
    registry.py
    utils.py
    web/
      __init__.py
      app.py
      cli.py
      viewmodels.py
      routes/
      templates/
      static/
```

Runtime data lives in:

```text
.adminbot/
  bots.json
  instances/
  logs/
  run/
```

## Installation

In PowerShell:

```powershell
cd E:\CODES2\nanobotalone
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e "."
```

To run the Adminbot web UI, the repo also needs the `web` dependencies used by Nanobot:

- `fastapi`
- `uvicorn`
- `jinja2`

If the current environment does not already include them, install the repo with web extras or otherwise ensure those packages are available in the same `venv`.

## Quick Start

CLI manager:

```powershell
.\adminbot\launcher.ps1
```

Or run the module directly:

```powershell
venv\Scripts\python.exe -m adminbot.app.main
```

Web UI:

```powershell
venv\Scripts\python.exe -m adminbot.app.main web --port 8900
```

Then open:

```text
http://127.0.0.1:8900
```

## Common Commands

```powershell
venv\Scripts\python.exe -m adminbot.app.main list
venv\Scripts\python.exe -m adminbot.app.main create --workspace .\my-workspace --web-port 8899
venv\Scripts\python.exe -m adminbot.app.main start my-bot
venv\Scripts\python.exe -m adminbot.app.main stop my-bot
venv\Scripts\python.exe -m adminbot.app.main restart my-bot
venv\Scripts\python.exe -m adminbot.app.main status my-bot
venv\Scripts\python.exe -m adminbot.app.main web --port 8900
```

## Workspace Paths

`adminbot` accepts:

- absolute paths
- relative paths
- `~/my-bot`
- `$HOME/my-bot`

Examples:

```powershell
venv\Scripts\python.exe -m adminbot.app.main create --workspace E:\WORKSPACES\bot-a --web-port 8899
venv\Scripts\python.exe -m adminbot.app.main create --workspace .\bot-b --web-port 8901
venv\Scripts\python.exe -m adminbot.app.main create --workspace "~/.nanobot-bot-c" --web-port 8902
venv\Scripts\python.exe -m adminbot.app.main create --workspace "$HOME\.nanobot-bot-d" --web-port 8903
```

The web UI create form also includes quick buttons for `~/`, `$HOME/`, and `./`.

## Current Status

Phase 1 is in place with:

- registry in `.adminbot/bots.json`
- per-bot config instances in `.adminbot/instances/`
- per-bot logs in `.adminbot/logs/`
- per-bot pid/state files in `.adminbot/run/`
- background process control via CLI

Phase 2 is in place with:

- local-only FastAPI app factory
- dashboard route
- create bot form
- bot detail page
- log viewer page
- log viewer auto refresh, tail presets, and lightweight filter controls
- richer monitoring signals on dashboard and bot detail views

Phase 3 is in place with:

- startup reconciliation of saved bot runtime state
- local-only enforcement middleware for the manager UI
- log size guardrails with archive rotation on next bot start

Phase 4 is started at the planning/design level:

- terminal strategy documented
- PTY/ConPTY evaluation separated from immediate UI delivery
- shell-tab fallback retained as the safer early path
- Open Shell action available from bot detail

## Reliability Notes

- On manager startup, `adminbot` reconciles saved bot state against live processes so stale `running` entries recover after crash or restart.
- Per-bot stdout and stderr logs use simple size guardrails:
  - current log rotates when it reaches roughly `5 MB`
  - up to `3` archived log files are kept per stream
  - rotation is triggered when a bot starts again; a continuously running bot is not monitored for live rollover yet

## Security Notes

- The web UI is local-only by default:
  - Uvicorn binds `127.0.0.1`
  - a middleware rejects non-loopback requests unless `ADMINBOT_ALLOW_REMOTE=1`
- `ADMINBOT_ALLOW_REMOTE=1` is only a transport override. It does not add authentication.
- Do not expose Adminbot beyond localhost unless you intentionally add an access-control layer in front of it.

## Troubleshooting

- If `adminbot web` says web dependencies are missing, install the repo environment with the packages needed for Nanobot web support.
- If bot creation fails during `onboard`, verify the repo `venv` has all dependencies required by `nanobot.cli.commands`.
- If a bot looks `running` after a crash, restart `adminbot`; startup reconciliation should refresh the saved state.
- If `Open Shell` opens a PowerShell window instead of a Windows Terminal tab, that is the expected fallback when `wt` is not available.
