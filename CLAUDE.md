# Terminal Agent Launcher

**Last Updated:** 2026-08-29
**GitHub:** https://github.com/cbolden15/terminal-agent-launcher
**Stack:** Python standard library backend with HTML, CSS, and JavaScript

## Quick Reference

| Property | Value |
|----------|-------|
| Local Dev | http://127.0.0.1:4317 |
| Staging | Not applicable |
| Production | Local macOS LaunchAgent |

## Architecture

A loopback-only Python server discovers configured folders and serves a browser interface. Its launch endpoint resolves project IDs server-side, builds Claude Code and Codex commands from fixed permission allowlists, and creates an iTerm2 tab or window through AppleScript. Browser storage holds favorites, recents, and non-bypass launch preferences.

The canonical Python package is `terminal_agent_launcher`. The `agent_launchpad` package is a compatibility shim for the pre-0.1 local installation and must remain until a future migration removes it deliberately.

## Key Files

| File | Purpose |
|------|---------|
| `terminal_agent_launcher/server.py` | Local HTTP server, config migration, and folder discovery API |
| `terminal_agent_launcher/launch_agent.py` | macOS LaunchAgent installation and legacy-service migration |
| `terminal_agent_launcher/web/` | Finder-style browser interface and browser-storage migration |
| `agent_launchpad/` | Legacy Python module compatibility shims |
| `tests/` | Backend and browser-logic tests |

## Common Commands

```bash
# Development
make dev

# Build and test
make build
make test

# Install at login
make install

# Remove login service
make uninstall
```

## Environment Variables

No secrets or API keys are required. See `.env.example` for the optional config-home override.

## Deployment

**Target:** Local macOS LaunchAgent

Run `make install` to migrate any legacy service and start Terminal Agent Launcher at login. Run `make uninstall` to remove both canonical and legacy service registrations.

## Security invariants

- The server must reject non-loopback bind hosts.
- Mutations require a matching localhost origin and launcher marker header.
- Launch requests never accept raw paths, shell commands, or arbitrary flags.
- Full-bypass presets require explicit browser and server confirmation.
- Config and browser migrations preserve legacy data as a backup.

## Gotchas

Critical non-obvious issues are tracked in `docs/engineering/gotchas/`.

## Available Skills

No project-specific skills exist yet.
