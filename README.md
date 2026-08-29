# Terminal Agent Launcher

Terminal Agent Launcher is a local macOS project browser for starting Claude Code and Codex sessions in iTerm2. Pick a folder, choose a permission level, and open the session in a new tab or window.

It runs on your Mac with no account, analytics, cloud service, or API keys.

## Requirements

- macOS with iTerm2 installed in `/Applications`
- Python 3.9 or newer
- Claude Code, Codex CLI, or both available on your shell `PATH`
- A modern browser with JavaScript enabled

## Install

Clone the repository and install the login service:

```bash
git clone https://github.com/cbolden15/terminal-agent-launcher.git
cd terminal-agent-launcher
make install
```

The installer starts a localhost service and opens [http://127.0.0.1:4317](http://127.0.0.1:4317). It will start automatically when you log in.

You can also install the command with `pipx`:

```bash
pipx install git+https://github.com/cbolden15/terminal-agent-launcher.git
terminal-agent-launcher install
```

## Use

1. Click **Claude** or **Codex** beside a project.
2. Choose a permission preset.
3. Select **New Tab** or **New Window**.
4. Review the command preview and launch the session.

Press `C` or `X` to open the launch modal for the selected row. Press `Command-K` to search.

Full Bypass is highlighted in red and requires confirmation every time. It removes approval or sandbox protections from the underlying agent.

## Folder locations

The first run scans the folders immediately inside `~/Projects`. Use **Add Folder** to add a single project or another parent directory.

The server stores folder locations in:

```text
~/.config/terminal-agent-launcher/config.json
```

Favorites, recents, and non-bypass launch preferences stay in browser storage for `127.0.0.1:4317`.

## Updating or removing it

For a source checkout:

```bash
git pull --ff-only
make install
```

Remove the login service without deleting configuration or browser preferences:

```bash
make uninstall
```

## Migration from Agent Launchpad

Running the new installer removes the former `com.calebbolden.agent-launchpad` login service before installing the canonical service. On first load, the app copies the old config and browser preferences into their new locations. Legacy data remains in place as a backup.

The old `python -m agent_launchpad` command remains as a compatibility shim for the 0.1 release.

## Security and privacy

The HTTP server enforces loopback-only binding. It does not support LAN or internet access.

Launch requests accept only a project ID discovered by the server, a fixed permission preset, and a tab or window destination. Browser-provided commands and paths are ignored. AppleScript receives the resulting command as an argument rather than executable script source.

The service reads folder names and metadata to populate the project list. It does not send project data anywhere. Logs are written under `~/Library/Logs/TerminalAgentLauncher`.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and the supported security boundary.

## Development

```bash
make dev
make build
make test
```

The suite covers discovery, config and browser-storage migration, localhost request guards, command allowlists, bypass confirmation, LaunchAgent migration, search behavior, and package resources.

## Project status

Terminal Agent Launcher is alpha software and currently supports macOS with iTerm2. Planned improvements are tracked in [backlog.md](backlog.md).

Terminal Agent Launcher is an independent project. It is not affiliated with or endorsed by Anthropic or OpenAI. Claude and Codex are referenced only to describe compatibility with their command-line tools.

## License

[MIT](LICENSE)
