# Milestone 2A: teach and exact routing corrections

Implement Milestone 2A from `docs/designs/2026-09-18-phase-2-routing-feedback.md` in the Python standard-library CLI. The current branch is `feat/task-routing`; preserve the existing deterministic router, research loop, browser application, and `agent_launchpad` compatibility package.

Success means a user can capture a wrong route with `tal teach PROJECT --last` or `tal teach PROJECT --task TASK`, inspect and revoke corrections, and have the same normalized task route to the taught project before the heuristic router runs. Normal routing must remain local and zero-token.

Constraints:

- Keep direct selector and alias precedence unchanged.
- Taught corrections use exact normalized fingerprints only. Never add fuzzy matching.
- A correction whose project is absent from the current catalog is a terminal stale-correction error.
- Store private feedback under the XDG data directory and bounded route receipts under the XDG state directory, with secure directories/files, symlink rejection, version validation, and locked writes.
- `--last` accepts only a route receipt from the same terminal that is younger than 15 minutes and shows the task, route, receipt ID, and age before confirmation.
- Non-interactive callers use `--task` or `--receipt`; destructive revocation requires confirmation unless `--yes` is present.
- Stay inside Milestone 2A. Do not implement profile snapshots, private-corpus import, research lifecycle events, provider adapters, tournaments, or agent-config changes.
- Run `.codex-test-command` and `make build` before completion.

<!-- model: sonnet -->
## Phase 1: secure feedback and receipt storage

- [ ] Add a focused `terminal_agent_launcher/feedback.py` module for `normalize_task_v1`, append-only teach/revoke events, reduction to active corrections, and version validation.
- [ ] Add XDG data/state path helpers, mode-`0700` directories, mode-`0600` files, advisory locking, symlink rejection, and bounded receipt persistence.
- [ ] Represent route receipts with a stable ID, UTC timestamp, terminal identity, raw task, selected project identity, and routing evidence needed by `tal teach --last`.
- [ ] Add unit tests for normalization, reducer transitions, malformed/versioned data, permissions, symlinks, locking behavior, receipt bounds, terminal matching, and expiry.

<!-- model: sonnet -->
## Phase 2: exact override routing

- [ ] Resolve active taught fingerprints after direct selectors but before `route_task` in both preview and launch paths.
- [ ] Resolve the taught target against the current discovered catalog by stable project identity and canonical path; fail closed when it is stale or ambiguous.
- [ ] Record route receipts before returning a confident heuristic route so `--last` survives a later agent launch or process exit.
- [ ] Add regression tests proving exact overrides win, similar untaught text still uses the heuristic router, aliases retain precedence, stale corrections stop routing, and the router makes no provider call.

<!-- model: sonnet -->
## Phase 3: teach CLI and verification

- [ ] Add `tal teach PROJECT --last`, `--receipt ID`, and `--task TASK`, including the interactive confirmation and non-interactive safeguards.
- [ ] Add `tal teach --list`, `--show FEEDBACK_ID`, and `--revoke FEEDBACK_ID [--yes]` with clear human-readable output and append-only revocation.
- [ ] Update CLI help and the README with the correction workflow, local data locations, exact-match behavior, and recovery from stale corrections.
- [ ] Run `make test`, `make build`, and `git diff --check`; leave the branch committed and report any skipped verification.
