# Adminbot Checklist

## Phase 1

### Foundation

- [x] Create `adminbot/` root
- [x] Add `PLAN.md`
- [x] Add `CHECKLIST.md`
- [x] Add `README.md`
- [x] Add launcher placeholders
- [x] Add package skeleton
- [x] Add runtime data directory conventions to code

### Registry

- [x] Define bot record schema
- [x] Implement registry load/save helpers
- [x] Handle empty/missing registry safely
- [x] Prevent duplicate workspace registration
- [x] Validate port conflicts

### Runtime

- [x] Resolve repo root dynamically
- [x] Support `ADMINBOT_REPO_ROOT` override and validate resolved repo root
- [x] Resolve local Python executable dynamically
- [x] Verify Nanobot CLI entrypoints to depend on in `adminbot`
- [x] Create per-bot config directories in `.adminbot/instances/`
- [x] Create per-bot log directories in `.adminbot/logs/`
- [x] Create per-bot pid/state files in `.adminbot/run/`
- [x] Persist process metadata

### Process Management

- [x] Start bot in background without opening a dedicated terminal window
- [x] Stop bot gracefully
- [x] Restart bot
- [x] Refresh bot status from active processes
- [x] Handle stale pid/state files
- [x] Verify pid plus process identity before stop/status actions

## Phase 2

### Backlog To Address Early

- [x] Fix Windows `stop()` false-positive when process exits between identity check and `taskkill`
- [x] Preserve accurate `updated_at` semantics when batch-refreshing bot state

### Manager UI

- [x] Add minimal web app entrypoint
- [x] Add bot list page
- [x] Add create bot form
- [x] Add bot detail page
- [x] Add log viewer page
- [x] Add action buttons for start/stop/restart
- [x] Choose Manager UI web framework
- [x] Default Manager UI to local-only bind unless explicitly configured otherwise

### Integration

- [x] Reuse existing Nanobot onboard flow
- [x] Reuse Nanobot gateway web dashboard per bot
- [x] Link from manager UI to each bot dashboard
- [x] Decide whether to embed dashboards or open separately

## Phase 3

### Reliability And Operations

- [x] Add orphan-process recovery strategy after adminbot crash/restart
- [x] Add log rotation policy or size guardrails
- [x] Review manager UI auth needs before any non-local exposure

## Phase 4

### Terminal Experience

- [x] Ship Phase 1 with logs-only UX
- [x] Evaluate Windows PTY/ConPTY options — concluded: not needed for the current monitoring-first scope
- [x] Decide if interactive terminal tab is worth the complexity — concluded: not worth it for the current scope
- [x] Add optional shell/terminal tab fallback if needed

### Monitoring Focus

- [x] Improve log viewer refresh and tail experience
- [x] Consider lightweight search/filter in logs if needed
- [x] Improve bot status visibility for monitoring-first workflows

## Cross-Cutting

### Portability

- [ ] Verify folder can be copied into another repo
- [ ] Minimize repo-specific assumptions
- [ ] Document required dependencies
- [ ] Decide whether `adminbot` ships its own dependency manifest
- [ ] Document startup commands

### Quality

- [ ] Add smoke test path for registry/process helpers
- [ ] Add failure-mode notes in README
- [ ] Review Windows-specific behavior
- [ ] Review cleanup and shutdown behavior
