# Contributing

Terminal Agent Launcher is a small macOS utility. Focused fixes and additions for terminal-based coding agents are welcome.

## Development setup

You need macOS, Python 3.9 or newer, Node.js 20 or newer, and iTerm2.

```bash
git clone https://github.com/cbolden15/terminal-agent-launcher.git
cd terminal-agent-launcher
make test
make dev
```

Open `http://127.0.0.1:4317` after the development server starts.

## Pull requests

- Keep changes focused and preserve the localhost-only security boundary.
- Add tests for behavior changes and run `make build && make test`.
- Do not commit credentials, local project paths, logs, or generated agent state.
- Explain any new command-line flags or agent permission mappings in the pull request.

Report security problems privately according to [SECURITY.md](SECURITY.md).
