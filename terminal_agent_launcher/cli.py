"""Foreground command-line launcher for configured coding-agent projects."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .server import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    canonical_path,
    discover_projects,
    expand_path,
    load_config,
    save_config,
)


class SelectorError(ConfigError):
    """Raised when a project selector does not identify one project."""


class AgentNotFoundError(ConfigError):
    """Raised when a requested coding-agent executable is unavailable."""


class LaunchError(ConfigError):
    """Raised when a detected coding-agent executable cannot start."""


def _candidate_lines(projects: Sequence[dict[str, Any]]) -> str:
    return "; ".join(f"{project['name']} ({project['path']})" for project in projects)


def _canonical_selector(selector: str) -> str:
    try:
        return str(canonical_path(expand_path(selector), strict=False))
    except OSError:
        return selector


def resolve_project(
    selector: str,
    projects: Sequence[dict[str, Any]],
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a selector without ever returning an undiscovered project."""
    if not isinstance(selector, str) or not selector.strip():
        raise SelectorError("Project selector cannot be empty.")

    selector = selector.strip()
    aliases = aliases or {}
    if selector in aliases:
        target = aliases[selector]
        project = next(
            (candidate for candidate in projects if candidate["path"] == target), None
        )
        if project is None:
            raise SelectorError(
                f"Alias '{selector}' is stale: {_candidate_lines(projects) or 'no projects discovered.'}"
            )
        return project

    canonical_selector = _canonical_selector(selector)
    exact_paths = [
        project for project in projects if project["path"] == canonical_selector
    ]
    if exact_paths:
        return exact_paths[0]

    exact_names = [project for project in projects if project["name"] == selector]
    if len(exact_names) == 1:
        return exact_names[0]
    if len(exact_names) > 1:
        raise SelectorError(
            f"Ambiguous project '{selector}': {_candidate_lines(exact_names)}"
        )

    needle = selector.casefold()
    matches = [
        project
        for project in projects
        if needle in project["name"].casefold() or needle in project["path"].casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SelectorError(
            f"Ambiguous project '{selector}': {_candidate_lines(matches)}"
        )
    raise SelectorError(
        f"No project matches '{selector}'. Available: {_candidate_lines(projects) or 'none.'}"
    )


def validate_alias_name(alias: str) -> str:
    if not isinstance(alias, str) or not alias.strip():
        raise ConfigError("Alias cannot be empty.")
    return alias.strip()


def add_alias(
    config: dict[str, Any], alias: str, project: dict[str, Any]
) -> dict[str, Any]:
    """Store a validated alias for a canonical discovered project path."""
    alias = validate_alias_name(alias)
    aliases = config.setdefault("aliases", {})
    if any(existing.casefold() == alias.casefold() for existing in aliases):
        raise ConfigError(f"Alias '{alias}' already exists.")
    aliases[alias] = project["path"]
    return config


def remove_alias(config: dict[str, Any], alias: str) -> dict[str, Any]:
    alias = validate_alias_name(alias)
    aliases = config.setdefault("aliases", {})
    if alias not in aliases:
        raise ConfigError(f"Alias '{alias}' does not exist.")
    del aliases[alias]
    return config


def detect_agent(agent: str) -> str:
    if agent not in {"claude", "codex"}:
        raise ConfigError("Agent must be claude or codex.")
    executable = shutil.which(agent)
    if executable is None:
        raise AgentNotFoundError(f"{agent} was not found on PATH.")
    try:
        return str(Path(executable).resolve(strict=True))
    except OSError as exc:
        raise AgentNotFoundError(f"{agent} was not found on PATH.") from exc


def launch_project(project: dict[str, Any], agent: str) -> int:
    """Run the detected agent in the foreground with inherited terminal I/O."""
    executable = detect_agent(agent)
    try:
        completed = subprocess.run([executable], cwd=project["path"], check=False)
    except OSError as exc:
        raise LaunchError(f"Could not start {agent}: {exc}") from exc
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tal", description="Launch Claude Code or Codex in a configured project."
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Configuration file."
    )
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("arguments", nargs="*", metavar="COMMAND")
    return parser


def _list_projects(projects: Sequence[dict[str, Any]]) -> None:
    for project in projects:
        print(f"{project['name']}\t{project['path']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arguments = args.arguments

    try:
        config = load_config(args.config)
        projects = discover_projects(config)

        if arguments == ["list"] and args.agent is None:
            _list_projects(projects)
            return 0

        if len(arguments) >= 2 and arguments[0] == "alias":
            action = arguments[1]
            if args.agent:
                raise ConfigError("--agent is only valid when launching a project.")
            if action == "add" and len(arguments) == 4:
                project = resolve_project(arguments[3], projects, config["aliases"])
                add_alias(config, arguments[2], project)
                save_config(config, args.config)
                print(f"Alias '{arguments[2]}' -> {project['path']}")
                return 0
            if action == "remove" and len(arguments) == 3:
                remove_alias(config, arguments[2])
                save_config(config, args.config)
                print(f"Removed alias '{arguments[2]}'.")
                return 0
            raise ConfigError("Usage: tal alias add NAME PROJECT | tal alias remove NAME")

        if len(arguments) != 1:
            raise ConfigError("Usage: tal list | tal alias ... | tal PROJECT --agent claude|codex")
        if args.agent is None:
            raise ConfigError("Launching a project requires --agent claude or --agent codex.")
        project = resolve_project(arguments[0], projects, config["aliases"])
        return launch_project(project, args.agent)
    except ConfigError as exc:
        print(f"tal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
