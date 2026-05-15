# Adminbot Handoff Checklist

## Runtime Readiness

- [ ] `venv` exists and the project is installed in that environment
- [ ] web dependencies are available if the manager UI will be used
- [ ] `.adminbot/` is ignored by git
- [ ] `adminbot` can resolve the repo root correctly on the target machine

## Core Verification

- [ ] `venv\Scripts\python.exe -m adminbot.app.main list` runs successfully
- [ ] create bot works with an absolute workspace path
- [ ] create bot works with a `~/...` or `$HOME/...` workspace path
- [ ] start bot works and records pid/state data
- [ ] stop bot works and clears running state
- [ ] restart bot works
- [ ] status/list refreshes stale process state correctly after a manual process exit

## Web Verification

- [ ] `venv\Scripts\python.exe -m adminbot.app.main web --port 8900` starts successfully
- [ ] dashboard loads at `http://127.0.0.1:8900`
- [ ] create bot form loads and submits correctly
- [ ] create bot form shows spinner and `Creating...` state on the submit button while processing
- [ ] create bot form shows validation or submit errors inline without a full page reload
- [ ] dashboard links to each bot detail page
- [ ] log viewer loads for stdout and stderr
- [ ] invalid bot id redirects back with an HTML error message instead of a raw 500
- [ ] Open Shell action launches a shell in the selected bot workspace

## Operational Verification

- [ ] manager restart reconciles stale running entries
- [ ] log rotation behavior is understood by the operator
- [ ] local-only default is understood by the operator
- [ ] nobody plans to expose `ADMINBOT_ALLOW_REMOTE=1` without adding access control

## Documentation

- [ ] `adminbot/README.md` matches the current state of the implementation
- [ ] `adminbot/CHECKLIST.md` reflects completed and pending work
- [ ] `adminbot/PHASE4_TERMINAL_PLAN.md` is reviewed before terminal work begins
