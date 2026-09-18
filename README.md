# Terminal Agent Launcher

Terminal Agent Launcher includes a portable foreground CLI for starting Claude Code and Codex sessions in a configured project, plus a local macOS project browser for starting sessions in iTerm2.

It runs on your Mac with no account, analytics, cloud service, or API keys.

## Requirements

- Python 3.9 or newer
- Claude Code, Codex CLI, or both available on your shell `PATH`

The `tal` CLI core is portable across platforms. The browser launcher and its
iTerm2 integration remain macOS-only and require iTerm2 installed in
`/Applications`, plus a modern browser with JavaScript enabled.

## Install

Clone the repository. On macOS, install the browser service with:

```bash
git clone https://github.com/cbolden15/terminal-agent-launcher.git
cd terminal-agent-launcher
make install
```

On macOS, `make install` installs the login service, starts the localhost
browser at [http://127.0.0.1:4317](http://127.0.0.1:4317), and configures it
to start automatically when you log in. To install the commands on your
`PATH`, use `pipx`:

```bash
pipx install git+https://github.com/cbolden15/terminal-agent-launcher.git
```

For a local checkout, use `python3 -m pip install --editable .` instead.

The package provides both commands:

```bash
terminal-agent-launcher install
tal --help
```

`terminal-agent-launcher` remains the browser/service command. `tal` is the
portable foreground command and does not open iTerm2.

## Portable CLI

List the repositories in the CLI's local catalog:

```bash
tal list
```

Preview a local task route without starting an agent:

```bash
tal route "update my global Codex and Claude instructions"
# agent-config    /Users/calebbolden/Projects/agent-config
```

Add `--agent` to launch the selected repository, or pass unresolved multi-word
task text directly to `tal`:

```bash
tal route "change Terminal Agent Launcher project routing" --agent codex
tal "change Terminal Agent Launcher project routing" --agent codex
```

Task routing is deterministic and local. It reads the repository catalog plus
bounded local metadata, makes no network requests or API calls, and uses zero
model tokens. Before launching a task route, `tal` prints the selected canonical
repository, score, confidence margin, and strongest matched terms.

Create an alias for a discovered project, launch it, and remove the alias:

```bash
tal alias add payments ~/Projects/vora-payments
tal payments --agent codex
tal "Terminal Agent Launcher" --agent claude
tal alias remove payments
```

The launch command requires an explicit `--agent` (`claude` or `codex`). It
detects that executable on `PATH`, runs it in the foreground, inherits the
current terminal input/output, and uses the project's canonical directory as
its working directory.

Selectors resolve deterministically in this order:

1. Exact alias.
2. Exact canonical path.
3. Exact project name.
4. A unique case-insensitive partial match against project name or path.
5. For unresolved multi-word input only, a confident local task route.

An alias must point to a project returned by discovery. Missing or ambiguous
selectors, including weak or ambiguous task routes, print ranked candidates and
exit without launching. A stale exact alias is a terminal error and never falls
back to task routing. The CLI never builds a shell command, and it cannot launch
a path outside the discovered project set.

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
