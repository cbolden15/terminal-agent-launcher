"""Foreground command-line launcher for configured coding-agent projects."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .feedback import (
    FeedbackCorrection,
    FeedbackError,
    FeedbackStore,
    ReceiptStore,
    active_corrections_by_fingerprint,
    make_route_receipt,
    project_identity,
    task_fingerprint_v1,
    terminal_identity,
)
from .server import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    canonical_path,
    discover_cli_projects,
    expand_path,
    load_config,
    save_config,
)
from .routing import RoutingEvidence, route_task
from .research import (
    DEFAULT_MAX_EXPERIMENTS,
    DEFAULT_MAX_MINUTES,
    ResearchError,
    provider_names,
    run_research,
)


class SelectorError(ConfigError):
    """Raised when a project selector does not identify one project."""


class UnknownSelectorError(SelectorError):
    """Raised when direct selector resolution found no possible project."""


class StaleCorrectionError(SelectorError):
    """Raised when an exact taught correction no longer has one catalog target."""


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
    raise UnknownSelectorError(
        f"No project matches '{selector}'. Available: {_candidate_lines(projects) or 'none.'}"
    )


def _routing_error(task: str, evidence: RoutingEvidence) -> SelectorError:
    candidates = "; ".join(
        f"{candidate.project['name']} ({candidate.project['path']}, score {candidate.score:.2f})"
        for candidate in evidence.candidates[:3]
    )
    return SelectorError(
        f"Could not confidently route '{task}' "
        f"(score {evidence.score:.2f}, margin {evidence.confidence_margin:.2f}). "
        f"Candidates: {candidates or 'none.'}"
    )


def _canonical_project_path(project: dict[str, Any]) -> str:
    path = project.get("path")
    if not isinstance(path, str) or not path:
        raise StaleCorrectionError("Taught correction has an invalid project path.")
    try:
        return str(canonical_path(Path(path), strict=True))
    except OSError as exc:
        raise StaleCorrectionError(
            f"Taught correction target is stale: {path} is no longer available."
        ) from exc


def _resolve_taught_correction(
    task: str,
    projects: Sequence[dict[str, Any]],
    corrections: Sequence[FeedbackCorrection],
) -> dict[str, Any] | None:
    """Resolve one exact correction, rejecting missing or ambiguous catalog targets."""
    matches = active_corrections_by_fingerprint(
        {correction.feedback_id: correction for correction in corrections}
    ).get(task_fingerprint_v1(task), ())
    if not matches:
        return None
    if len(matches) != 1:
        raise StaleCorrectionError(
            f"Taught correction for '{task}' is ambiguous; revoke conflicting corrections."
        )

    expected = matches[0].expected_project
    expected_path = _canonical_project_path(expected)
    matching_ids = [
        project for project in projects if project_identity(project)["id"] == expected["id"]
    ]
    matching_paths = [
        project for project in projects if _canonical_project_path(project) == expected_path
    ]
    if len(matching_ids) > 1 or len(matching_paths) > 1:
        raise StaleCorrectionError(
            f"Taught correction for '{task}' has an ambiguous catalog target."
        )
    if len(matching_ids) != 1 or len(matching_paths) != 1 or matching_ids[0] is not matching_paths[0]:
        raise StaleCorrectionError(
            f"Taught correction for '{task}' is stale: expected project "
            f"{expected['name']} ({expected_path}) is no longer in the catalog."
        )
    return matching_ids[0]


def resolve_task(
    task: str,
    projects: Sequence[dict[str, Any]],
    aliases: dict[str, str] | None = None,
    routing_roots: Sequence[Path | str] = (),
    feedback_store: FeedbackStore | None = None,
) -> tuple[dict[str, Any], RoutingEvidence | None]:
    """Resolve direct selectors, exact corrections, then unresolved task text."""
    try:
        return resolve_project(task, projects, aliases), None
    except UnknownSelectorError:
        if len(task.split()) < 2:
            raise

    corrections = (feedback_store or FeedbackStore()).active_corrections()
    taught_project = _resolve_taught_correction(task, projects, tuple(corrections.values()))
    if taught_project is not None:
        return taught_project, None

    evidence = route_task(task, projects, routing_roots)
    if evidence.project is None:
        raise _routing_error(task, evidence)
    return evidence.project, evidence


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
        prog="tal",
        description=(
            "Launch Claude Code or Codex in a configured project, or route a "
            "task locally without model tokens."
        ),
        epilog=(
            "Commands:\n"
            "  tal list\n"
            "  tal route TASK\n"
            "  tal route TASK --agent claude|codex\n"
            "  tal PROJECT --agent claude|codex\n"
            "  tal research [--provider codex]\n"
            "\n"
            "Unresolved multi-word PROJECT values are routed locally using "
            "repository names, paths, and bounded local metadata. Research "
            "runs are explicit, bounded, and isolated from the installed router."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Configuration file."
    )
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("--provider", choices=provider_names(), default="codex")
    parser.add_argument("--repo", type=Path, help="Source checkout used by tal research.")
    parser.add_argument(
        "--private-corpus",
        type=Path,
        help="Optional private routing JSONL corpus used only by the evaluator.",
    )
    parser.add_argument(
        "--max-experiments",
        type=int,
        default=DEFAULT_MAX_EXPERIMENTS,
        help=f"Maximum research proposals (default: {DEFAULT_MAX_EXPERIMENTS}).",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=DEFAULT_MAX_MINUTES,
        help=f"Research wall-clock limit (default: {DEFAULT_MAX_MINUTES:g}).",
    )
    parser.add_argument("arguments", nargs="*", metavar="COMMAND")
    return parser


def _list_projects(projects: Sequence[dict[str, Any]]) -> None:
    for project in projects:
        print(f"{project['name']}\t{project['path']}")


def _routing_roots(config: dict[str, Any]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for location in config["locations"]:
        try:
            roots.append(canonical_path(expand_path(location["path"]), strict=True))
        except OSError:
            continue
    return tuple(roots)


def _print_route(evidence: RoutingEvidence) -> None:
    if evidence.project is None:
        return
    print(f"{evidence.project['name']}\t{evidence.project['path']}")
    print(
        f"Score: {evidence.score:.2f}; margin: {evidence.confidence_margin:.2f}; "
        f"matched: {', '.join(evidence.strongest_terms)}",
        flush=True,
    )


def _receipt_evidence(evidence: RoutingEvidence) -> dict[str, Any]:
    return {
        "score": evidence.score,
        "margin": evidence.confidence_margin,
        "matched_terms": list(evidence.strongest_terms),
        "candidates": [
            {
                "project_id": candidate.project.get("id"),
                "score": candidate.score,
                "matched_terms": list(candidate.strongest_terms),
            }
            for candidate in evidence.candidates[:3]
        ],
    }


def _record_route_receipt(
    task: str,
    project: dict[str, Any],
    evidence: RoutingEvidence | None,
    receipt_store: ReceiptStore | None = None,
) -> None:
    """Persist heuristic route evidence before previewing or launching it."""
    if evidence is None:
        return
    terminal = terminal_identity()
    if terminal is None:
        return
    receipt = make_route_receipt(
        task,
        project,
        _receipt_evidence(evidence),
        terminal_id=terminal,
    )
    (receipt_store or ReceiptStore()).record(receipt)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arguments = args.arguments

    try:
        if arguments == ["research"]:
            if args.agent is not None:
                raise ConfigError("--agent is not valid for tal research; use --provider.")
            result = run_research(
                repository=args.repo,
                provider_name=args.provider,
                max_experiments=args.max_experiments,
                max_minutes=args.max_minutes,
                private_corpus=args.private_corpus,
            )
            print(f"Candidate branch: {result.branch}")
            print(f"Kept: {result.kept_experiments}/{result.experiments_run} experiments")
            print(f"Report: {result.report_path}")
            return 0

        research_options_used = (
            args.repo is not None
            or args.private_corpus is not None
            or args.provider != "codex"
            or args.max_experiments != DEFAULT_MAX_EXPERIMENTS
            or args.max_minutes != DEFAULT_MAX_MINUTES
        )
        if research_options_used:
            raise ConfigError("Research options are only valid with 'tal research'.")

        config = load_config(args.config)
        projects = discover_cli_projects(config)

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

        routing_roots = _routing_roots(config)
        if len(arguments) >= 2 and arguments[0] == "route":
            task = " ".join(arguments[1:])
            project, evidence = resolve_task(
                task, projects, config["aliases"], routing_roots
            )
            _record_route_receipt(task, project, evidence)
            if evidence is not None:
                _print_route(evidence)
            else:
                print(f"{project['name']}\t{project['path']}")
            if args.agent is None:
                return 0
            return launch_project(project, args.agent)

        if not arguments:
            raise ConfigError("Usage: tal list | tal route TASK | tal PROJECT --agent claude|codex")
        if args.agent is None:
            raise ConfigError("Launching a project requires --agent claude or codex.")
        selector = " ".join(arguments)
        project, evidence = resolve_task(
            selector, projects, config["aliases"], routing_roots
        )
        _record_route_receipt(selector, project, evidence)
        if evidence is not None:
            _print_route(evidence)
        return launch_project(project, args.agent)
    except (ConfigError, FeedbackError, ResearchError) as exc:
        print(f"tal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
