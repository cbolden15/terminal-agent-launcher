from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from .launch_agent import DEFAULT_URL, install, uninstall
from .server import DEFAULT_CONFIG_PATH, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminal-agent-launcher",
        description="Open Claude Code or Codex in any configured folder.",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the local web app.")
    serve_parser.add_argument("--port", type=int, default=4317)
    serve_parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Use a different configuration file.",
    )

    install_parser = subparsers.add_parser(
        "install", help="Install and start the macOS LaunchAgent."
    )
    install_parser.add_argument(
        "--no-open", action="store_true", help="Do not open the browser after installation."
    )
    subparsers.add_parser("uninstall", help="Remove the macOS LaunchAgent.")
    subparsers.add_parser(
        "open", help="Open Terminal Agent Launcher in the default browser."
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "serve"

    if command == "serve":
        serve("127.0.0.1", args.port, args.config)
    elif command == "install":
        path = install(open_browser=not args.no_open)
        print(f"Installed {path}")
    elif command == "uninstall":
        removed = uninstall()
        print(
            "Removed Terminal Agent Launcher LaunchAgent."
            if removed
            else "LaunchAgent was not installed."
        )
    elif command == "open":
        webbrowser.open(DEFAULT_URL)
    else:
        parser.error(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
