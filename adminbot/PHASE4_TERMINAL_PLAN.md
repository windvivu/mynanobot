# Adminbot Phase 4 Terminal Plan

## Goal

Improve the operator experience beyond logs-only management without forcing one visible terminal window per bot.

For the current scope, this means better monitoring and safe shell fallback, not embedded interactive terminals.

## Constraints

- Windows is the primary environment.
- PTY/ConPTY integration is possible but carries complexity and maintenance risk.
- The current UI already works with logs-first operations, so terminal work should not destabilize the manager.

## Recommended Sequence

### Step 1: Keep Logs-First UX

- preserve the current log viewer as the default operational surface
- improve logs before terminal complexity if that delivers most of the practical value

### Step 2: Add Shell-Tab Fallback

- allow the UI to launch a separate shell or shell-tab action for a bot when an operator explicitly needs it
- this is lower risk than full embedded PTY
- it preserves the "one control surface" model while keeping terminal complexity optional

### Step 3: Evaluate PTY/ConPTY

- research Windows-compatible PTY strategy
- identify package, lifecycle, and security implications
- only proceed if the UX gain is worth the added runtime complexity

Current decision for this scope:

- do not proceed with PTY/ConPTY implementation
- do not add embedded interactive terminal tabs
- keep `Open Shell` as the explicit debug fallback path
- invest remaining Phase 4 effort into monitoring and log UX instead

## What Phase 4 Does Not Need Immediately

- multi-user collaborative terminal sessions
- remote-access hardening
- browser-native terminal emulation if logs and explicit shell fallback already solve the main workflow
- PTY/ConPTY integration for the current operator workflow

## Candidate Deliverables

- add a "Open Shell" action from the bot detail page
- optionally open a Windows Terminal tab if available
- document fallback behavior when Windows Terminal is not installed
- improve monitoring-first UX:
  - log refresh behavior
  - better tailing
  - lightweight search/filter if needed
  - clearer bot status visibility

## Current Implementation Status

- `Open Shell` is now the first concrete Phase 4 fallback path.
- On Windows:
  - prefer `wt new-tab` when Windows Terminal is available
  - fall back to a dedicated PowerShell window when it is not
- The shell opens in the bot workspace and stays intentionally separate from embedded terminal work.
- PTY/ConPTY is intentionally deferred and considered unnecessary for the current scope.

## PTY Evaluation Questions

These remain as future-reference questions only. They are not part of the active scope now.

- which Windows PTY library is stable enough for this repo
- how process ownership and cleanup should work
- how terminal sessions map to existing bot process lifecycle
- whether terminal interaction should be read/write or read-mostly
- how to avoid exposing an unsafe remote shell surface through the manager UI

## Exit Criteria For Phase 4 Start

- operators understand logs-first remains the default
- shell fallback path is specified
- PTY work is treated as an explicit non-go for the current scope, not accidental scope creep

## Phase 4 Focus Now

- keep `Open Shell` as fallback only
- improve monitoring and log visibility
- avoid building interactive terminal features that the operator does not need
