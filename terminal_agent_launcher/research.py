"""Bounded autonomous experiments for improving the deterministic router."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence


DEFAULT_MAX_EXPERIMENTS = 20
DEFAULT_MAX_MINUTES = 60.0
DEFAULT_EXPERIMENT_TIMEOUT_SECONDS = 10 * 60
ROUTER_RELATIVE_PATH = Path("terminal_agent_launcher/routing.py")
PUBLIC_CORPUS_RELATIVE_PATH = Path("research/routing_cases.public.jsonl")
PRIVATE_CORPUS_RELATIVE_PATH = Path("research/routing_cases.private.jsonl")
PROPOSAL_SCHEMA_RELATIVE_PATH = Path("research/proposal.schema.json")


class ResearchError(RuntimeError):
    """Raised when a research run cannot continue safely."""


@dataclass(frozen=True)
class GroupMetrics:
    total: int = 0
    correct_routes: int = 0
    correct_abstentions: int = 0
    missed_routes: int = 0
    wrong_confident: int = 0
    errors: int = 0

    @property
    def correct(self) -> int:
        return self.correct_routes + self.correct_abstentions

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "correct_routes": self.correct_routes,
            "correct_abstentions": self.correct_abstentions,
            "missed_routes": self.missed_routes,
            "wrong_confident": self.wrong_confident,
            "errors": self.errors,
        }


@dataclass(frozen=True)
class EvaluationSnapshot:
    groups: Mapping[str, GroupMetrics]
    outcomes: tuple[dict[str, Any], ...]
    source_lines: int
    duration_seconds: float

    @property
    def total(self) -> GroupMetrics:
        values = tuple(self.groups.values())
        return GroupMetrics(
            total=sum(item.total for item in values),
            correct_routes=sum(item.correct_routes for item in values),
            correct_abstentions=sum(item.correct_abstentions for item in values),
            missed_routes=sum(item.missed_routes for item in values),
            wrong_confident=sum(item.wrong_confident for item in values),
            errors=sum(item.errors for item in values),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "groups": {name: value.as_dict() for name, value in self.groups.items()},
            "total": self.total.as_dict(),
            "source_lines": self.source_lines,
            "duration_seconds": round(self.duration_seconds, 4),
        }


@dataclass(frozen=True)
class PatchProposal:
    description: str
    patch: str


@dataclass(frozen=True)
class ProviderResult:
    proposal: Optional[PatchProposal]
    status: str
    detail: str = ""


class ProposalProvider(Protocol):
    name: str

    def propose(
        self,
        worktree: Path,
        prompt: str,
        output_directory: Path,
        experiment: int,
        timeout_seconds: float,
    ) -> ProviderResult:
        ...


@dataclass(frozen=True)
class ResearchRunResult:
    branch: str
    report_path: Path
    results_path: Path
    experiments_run: int
    kept_experiments: int
    baseline: EvaluationSnapshot
    best: EvaluationSnapshot


class CodexProvider:
    """Read-only Codex proposer that returns a structured unified diff."""

    name = "codex"

    def __init__(self, executable: Optional[str] = None) -> None:
        detected = executable or shutil.which("codex")
        if not detected:
            raise ResearchError("Codex was not found on PATH.")
        try:
            self.executable = str(Path(detected).resolve(strict=True))
        except OSError as exc:
            raise ResearchError("Codex was not found on PATH.") from exc

    def propose(
        self,
        worktree: Path,
        prompt: str,
        output_directory: Path,
        experiment: int,
        timeout_seconds: float,
    ) -> ProviderResult:
        schema_path = worktree / PROPOSAL_SCHEMA_RELATIVE_PATH
        response_path = output_directory / f"proposal-{experiment:03d}.json"
        log_path = output_directory / f"provider-{experiment:03d}.log"
        command = [
            self.executable,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(response_path),
            "--cd",
            str(worktree),
            "-",
        ]
        try:
            with log_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=max(timeout_seconds, 1.0),
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return ProviderResult(None, "timeout", "Codex exceeded the experiment timeout.")
        except OSError as exc:
            return ProviderResult(None, "provider_error", f"Could not start Codex: {exc}")

        if completed.returncode != 0 or not response_path.is_file():
            return ProviderResult(
                None,
                "provider_error",
                f"Codex exited with status {completed.returncode}; see {log_path}.",
            )
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            description = payload["description"].strip()
            patch = payload["patch"]
            if not description or not isinstance(patch, str) or not patch.strip():
                raise ValueError("empty proposal fields")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ProviderResult(None, "provider_error", f"Codex returned an invalid proposal: {exc}")
        return ProviderResult(PatchProposal(description, patch), "proposed")


def provider_names() -> tuple[str, ...]:
    return ("codex",)


def create_provider(name: str) -> ProposalProvider:
    if name == "codex":
        return CodexProvider()
    raise ResearchError(
        f"Unknown research provider '{name}'. Available: {', '.join(provider_names())}."
    )


def _run(
    arguments: Sequence[str],
    cwd: Path,
    timeout: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=max(timeout, 1.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResearchError(f"Command failed to run: {' '.join(arguments)}: {exc}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise ResearchError(
            f"Command failed ({completed.returncode}): {' '.join(arguments)}"
            + (f"\n{detail}" if detail else "")
        )
    return completed


def resolve_repository(path: Optional[Path] = None) -> Path:
    candidate = (path or Path(__file__).resolve().parents[1]).expanduser().resolve()
    completed = _run(["git", "rev-parse", "--show-toplevel"], candidate)
    repository = Path(completed.stdout.strip()).resolve(strict=True)
    for required in (
        ROUTER_RELATIVE_PATH,
        PUBLIC_CORPUS_RELATIVE_PATH,
        PROPOSAL_SCHEMA_RELATIVE_PATH,
        Path(".codex-test-command"),
    ):
        if not (repository / required).is_file():
            raise ResearchError(f"Research repository is missing {required}: {repository}")
    return repository


def _load_jsonl(path: Path, group_override: Optional[str] = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResearchError(f"Could not read routing corpus {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResearchError(f"Invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(case, dict):
            raise ResearchError(f"Routing case must be an object at {path}:{line_number}.")
        missing = {"id", "task", "expected", "projects"} - set(case)
        if missing:
            raise ResearchError(
                f"Routing case at {path}:{line_number} is missing: {', '.join(sorted(missing))}."
            )
        if not isinstance(case["id"], str) or not case["id"].strip():
            raise ResearchError(f"Routing case ID must be nonempty at {path}:{line_number}.")
        if not isinstance(case["task"], str) or not case["task"].strip():
            raise ResearchError(f"Routing task must be nonempty at {path}:{line_number}.")
        if not isinstance(case["projects"], list) or not case["projects"]:
            raise ResearchError(f"Routing projects must be a nonempty list at {path}:{line_number}.")
        group = group_override or case.get("split")
        if group not in {"train", "holdout", "private"}:
            raise ResearchError(
                f"Routing case split must be train or holdout at {path}:{line_number}."
            )
        cases.append({**case, "group": group})
    return cases


def load_cases(repository: Path, private_corpus: Optional[Path] = None) -> list[dict[str, Any]]:
    public_cases = _load_jsonl(repository / PUBLIC_CORPUS_RELATIVE_PATH)
    selected_private = private_corpus or repository / PRIVATE_CORPUS_RELATIVE_PATH
    private_cases = _load_jsonl(selected_private, "private") if selected_private.is_file() else []
    cases = [*public_cases, *private_cases]
    identifiers = [case["id"] for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ResearchError("Routing case IDs must be unique across public and private corpora.")
    if not any(case["group"] == "train" for case in cases):
        raise ResearchError("The public routing corpus needs at least one train case.")
    return cases


def _group_metrics(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, GroupMetrics]:
    metrics: dict[str, GroupMetrics] = {}
    for group in ("train", "holdout", "private"):
        selected = [outcome for outcome in outcomes if outcome.get("group") == group]
        metrics[group] = GroupMetrics(
            total=len(selected),
            correct_routes=sum(item.get("status") == "correct_route" for item in selected),
            correct_abstentions=sum(item.get("status") == "correct_abstain" for item in selected),
            missed_routes=sum(item.get("status") == "missed_route" for item in selected),
            wrong_confident=sum(item.get("status") == "wrong_confident" for item in selected),
            errors=sum(item.get("status") == "error" for item in selected),
        )
    return metrics


def _source_lines(router_path: Path) -> int:
    return sum(bool(line.strip()) for line in router_path.read_text(encoding="utf-8").splitlines())


def evaluate_candidate(
    router_path: Path,
    cases: Sequence[Mapping[str, Any]],
    timeout_seconds: float = 30.0,
) -> EvaluationSnapshot:
    evaluator = Path(__file__).with_name("research_eval.py").resolve(strict=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tal-research-eval-request-") as temporary:
        request_path = Path(temporary) / "request.json"
        output_path = Path(temporary) / "output.json"
        request_path.write_text(json.dumps({"cases": list(cases)}), encoding="utf-8")
        completed = _run(
            [
                sys.executable,
                str(evaluator),
                "--router",
                str(router_path),
                "--request",
                str(request_path),
                "--output",
                str(output_path),
            ],
            cwd=router_path.parent.parent,
            timeout=timeout_seconds,
            check=False,
        )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchError(
                f"Routing evaluator produced no valid result: {(completed.stderr or completed.stdout)[-1000:]}"
            ) from exc
        if completed.returncode != 0 or "error" in payload:
            raise ResearchError(f"Routing evaluator failed: {payload.get('error', 'unknown error')}")
        outcomes = tuple(payload.get("outcomes", ()))
        if len(outcomes) != len(cases):
            raise ResearchError("Routing evaluator returned an incomplete case set.")
    return EvaluationSnapshot(
        groups=_group_metrics(outcomes),
        outcomes=outcomes,
        source_lines=_source_lines(router_path),
        duration_seconds=time.monotonic() - started,
    )


def _passes_safety(snapshot: EvaluationSnapshot) -> bool:
    total = snapshot.total
    return total.wrong_confident == 0 and total.errors == 0


def _protected_non_regression(
    current: EvaluationSnapshot, candidate: EvaluationSnapshot
) -> bool:
    for group in ("holdout", "private"):
        before = current.groups[group]
        after = candidate.groups[group]
        if after.correct_routes < before.correct_routes:
            return False
        if after.correct_abstentions < before.correct_abstentions:
            return False
    return True


def should_promote(current: EvaluationSnapshot, candidate: EvaluationSnapshot) -> tuple[bool, str]:
    if not _passes_safety(candidate):
        return False, "candidate has a confident wrong route or evaluation error"
    if not _protected_non_regression(current, candidate):
        return False, "candidate regresses the holdout or private corpus"

    before = current.total
    after = candidate.total
    if before.wrong_confident > 0 and after.wrong_confident == 0:
        return True, "removed all confident wrong routes"
    if after.correct_routes > before.correct_routes:
        return True, "routes more tasks correctly"
    if after.correct_routes == before.correct_routes and after.correct_abstentions > before.correct_abstentions:
        return True, "handles more required abstentions correctly"
    if (
        after.correct_routes == before.correct_routes
        and after.correct_abstentions == before.correct_abstentions
        and candidate.source_lines < current.source_lines
    ):
        return True, "preserves behavior with a smaller router"
    return False, "does not improve the promotion objective"


def _metric_line(snapshot: EvaluationSnapshot) -> str:
    total = snapshot.total
    return (
        f"correct routes {total.correct_routes}, correct abstentions {total.correct_abstentions}, "
        f"missed routes {total.missed_routes}, wrong confident {total.wrong_confident}, "
        f"errors {total.errors}, source lines {snapshot.source_lines}"
    )


def _visible_failures(
    cases: Sequence[Mapping[str, Any]], snapshot: EvaluationSnapshot
) -> list[str]:
    by_id = {case["id"]: case for case in cases if case["group"] == "train"}
    failures: list[str] = []
    for outcome in snapshot.outcomes:
        if outcome.get("group") != "train" or outcome.get("status") in {
            "correct_route",
            "correct_abstain",
        }:
            continue
        case = by_id.get(outcome.get("id"))
        if case is None:
            continue
        failures.append(
            f"- {case['id']}: task={case['task']!r}; expected={case['expected']!r}; "
            f"selected={outcome.get('selected')!r}; status={outcome.get('status')}"
        )
    return failures


def build_prompt(
    cases: Sequence[Mapping[str, Any]],
    current: EvaluationSnapshot,
    experiment: int,
    history: Sequence[Mapping[str, Any]],
) -> str:
    failures = _visible_failures(cases, current)
    history_lines = [
        f"- experiment {item['experiment']}: {item['status']} — {item['description']}"
        for item in history[-5:]
    ]
    return "\n".join(
        [
            "You are proposing one bounded experiment to improve Terminal Agent Launcher's deterministic task router.",
            "",
            f"Experiment: {experiment}",
            f"Current benchmark: {_metric_line(current)}.",
            "",
            "Constraints:",
            "- Inspect terminal_agent_launcher/routing.py and the public training cases.",
            "- Do not edit files, run Git commands, use network access, or inspect cases marked holdout.",
            "- The returned patch may modify only terminal_agent_launcher/routing.py.",
            "- Preserve deterministic, local, zero-token production routing.",
            "- A confident route to the wrong project is a hard failure.",
            "- Make one coherent change. Simpler wins when benchmark behavior is equal.",
            "- Return a git-compatible unified diff with repository-relative a/ and b/ paths.",
            "",
            "Visible training failures:",
            *(failures or ["- none; look for a simpler implementation that preserves behavior"]),
            "",
            "Recent experiments:",
            *(history_lines or ["- none"]),
            "",
            "Return only the JSON object required by the output schema.",
        ]
    )


def _patch_paths(patch: str) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        value = line[4:].split("\t", 1)[0]
        if value == "/dev/null":
            continue
        if not value.startswith(("a/", "b/")):
            raise ResearchError("Patch paths must use repository-relative a/ and b/ prefixes.")
        paths.add(value[2:])
    if not paths:
        raise ResearchError("Proposal does not contain a unified diff.")
    return paths


def apply_proposal(worktree: Path, proposal: PatchProposal, patch_path: Path) -> None:
    allowed = ROUTER_RELATIVE_PATH.as_posix()
    paths = _patch_paths(proposal.patch)
    if paths != {allowed}:
        raise ResearchError(
            f"Proposal changes disallowed paths: {', '.join(sorted(paths - {allowed})) or 'unknown'}."
        )
    patch_path.write_text(proposal.patch, encoding="utf-8")
    _run(["git", "apply", "--check", str(patch_path)], worktree)
    _run(["git", "apply", "--whitespace=nowarn", str(patch_path)], worktree)
    changed = set(
        filter(
            None,
            _run(["git", "diff", "--name-only", "HEAD"], worktree).stdout.splitlines(),
        )
    )
    untracked = set(
        filter(
            None,
            _run(["git", "ls-files", "--others", "--exclude-standard"], worktree).stdout.splitlines(),
        )
    )
    if changed != {allowed} or untracked:
        raise ResearchError("Applied proposal changed files outside the routing allowlist.")


def _restore_router(worktree: Path) -> None:
    _run(
        [
            "git",
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "--",
            ROUTER_RELATIVE_PATH.as_posix(),
        ],
        worktree,
    )


def _test_command(repository: Path) -> list[str]:
    value = (repository / ".codex-test-command").read_text(encoding="utf-8").strip()
    if not value or "\n" in value:
        raise ResearchError(".codex-test-command must contain one nonempty command line.")
    command = shlex.split(value)
    if not command:
        raise ResearchError(".codex-test-command did not contain a command.")
    return command


def run_gate(
    worktree: Path,
    command: Sequence[str],
    log_path: Path,
    timeout_seconds: float,
) -> tuple[bool, str]:
    try:
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                list(command),
                cwd=worktree,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=max(timeout_seconds, 1.0),
                check=False,
            )
    except subprocess.TimeoutExpired:
        return False, "test command timed out"
    except OSError as exc:
        return False, f"test command could not start: {exc}"
    return completed.returncode == 0, f"test command exited {completed.returncode}"


def _state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "terminal-agent-launcher" / "research"


def _append_result(path: Path, result: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(result), sort_keys=True) + "\n")


def _branch_name(repository: Path, started: datetime) -> str:
    stem = f"autoresearch/routing-{started.strftime('%Y%m%d-%H%M%S')}"
    branch = stem
    suffix = 2
    while _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        repository,
        check=False,
    ).returncode == 0:
        branch = f"{stem}-{suffix}"
        suffix += 1
    return branch


def _commit_candidate(
    worktree: Path,
    output_directory: Path,
    experiment: int,
    description: str,
) -> str:
    _run(["git", "add", "--", ROUTER_RELATIVE_PATH.as_posix()], worktree)
    message_path = output_directory / f"commit-{experiment:03d}.txt"
    summary = " ".join(description.split())[:200]
    message_path.write_text(
        f"research: routing experiment {experiment:02d}\n\n{summary}\n",
        encoding="utf-8",
    )
    _run(
        [
            "git",
            "-c",
            "user.name=Terminal Agent Launcher Research",
            "-c",
            "user.email=research@terminal-agent-launcher.invalid",
            "commit",
            "-F",
            str(message_path),
        ],
        worktree,
    )
    return _run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()


def _report_text(
    branch: str,
    base_commit: str,
    provider: str,
    max_experiments: int,
    max_minutes: float,
    baseline: EvaluationSnapshot,
    best: EvaluationSnapshot,
    experiments: Sequence[Mapping[str, Any]],
    results_path: Path,
) -> str:
    rows = ["| # | Status | Result | Description |", "|---:|---|---|---|"]
    for item in experiments:
        description = str(item.get("description", "")).replace("|", "\\|")
        reason = str(item.get("reason", "")).replace("|", "\\|")
        rows.append(
            f"| {item['experiment']} | {item['status']} | {reason} | {description} |"
        )
    if not experiments:
        rows.append("| — | not run | The experiment budget was zero. | — |")
    kept = sum(item.get("status") == "kept" for item in experiments)
    return "\n".join(
        [
            "# Routing research report",
            "",
            f"Branch: `{branch}`  ",
            f"Base commit: `{base_commit}`  ",
            f"Provider: `{provider}`  ",
            f"Budget: {max_experiments} experiments or {max_minutes:g} minutes  ",
            f"Kept experiments: {kept}",
            "",
            "## Outcome",
            "",
            f"Baseline: {_metric_line(baseline)}.",
            "",
            f"Best: {_metric_line(best)}.",
            "",
            "A candidate was eligible only when it had zero confident wrong routes, no evaluator errors, and no holdout or private-corpus regression. The controller committed kept patches to the candidate branch. It did not merge or install them.",
            "",
            "## Experiments",
            "",
            *rows,
            "",
            "## Artifacts",
            "",
            f"The machine-readable ledger is `{results_path}`. Provider logs and proposal files are in the same private state directory.",
            "",
        ]
    )


def run_research(
    repository: Optional[Path] = None,
    provider_name: str = "codex",
    max_experiments: int = DEFAULT_MAX_EXPERIMENTS,
    max_minutes: float = DEFAULT_MAX_MINUTES,
    private_corpus: Optional[Path] = None,
    output_root: Optional[Path] = None,
    provider: Optional[ProposalProvider] = None,
    progress: Callable[[str], None] = print,
) -> ResearchRunResult:
    if max_experiments < 0:
        raise ResearchError("--max-experiments cannot be negative.")
    if max_minutes <= 0:
        raise ResearchError("--max-minutes must be greater than zero.")

    repo = resolve_repository(repository)
    dirty = _run(["git", "status", "--porcelain"], repo).stdout.strip()
    if dirty:
        raise ResearchError("Research requires a clean Git worktree so the base commit is unambiguous.")

    cases = load_cases(repo, private_corpus)
    selected_provider = provider or create_provider(provider_name)
    if selected_provider.name != provider_name:
        raise ResearchError("Injected provider name does not match the requested provider.")

    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + (max_minutes * 60.0)
    base_commit = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    branch = _branch_name(repo, started)
    run_directory = (output_root or _state_root()) / branch.replace("/", "-")
    run_directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        run_directory.chmod(0o700)
    except OSError:
        pass
    results_path = run_directory / "results.jsonl"
    report_path = run_directory / "report.md"
    test_command = _test_command(repo)

    temporary_root = Path(tempfile.mkdtemp(prefix="tal-routing-research-"))
    worktree = temporary_root / "worktree"
    experiments: list[dict[str, Any]] = []
    kept = 0
    baseline: Optional[EvaluationSnapshot] = None
    best: Optional[EvaluationSnapshot] = None

    try:
        _run(["git", "worktree", "add", "-b", branch, str(worktree), base_commit], repo)
        progress(f"Research branch: {branch}")
        progress("Baseline: evaluating router and running the full test command.")
        baseline = evaluate_candidate(worktree / ROUTER_RELATIVE_PATH, cases)
        best = baseline
        baseline_timeout = min(max(deadline - time.monotonic(), 1.0), DEFAULT_EXPERIMENT_TIMEOUT_SECONDS)
        gate_ok, gate_detail = run_gate(
            worktree,
            test_command,
            run_directory / "tests-baseline.log",
            baseline_timeout,
        )
        if not gate_ok:
            raise ResearchError(f"Baseline test command failed: {gate_detail}")
        _append_result(
            results_path,
            {
                "type": "baseline",
                "commit": base_commit,
                "metrics": baseline.as_dict(),
                "tests": "passed",
            },
        )

        consecutive_provider_failures = 0
        for experiment in range(1, max_experiments + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                progress("Run budget reached before the next experiment.")
                break
            progress(f"Experiment {experiment}/{max_experiments}: requesting one {provider_name} proposal.")
            prompt = build_prompt(cases, best, experiment, experiments)
            provider_result = selected_provider.propose(
                worktree,
                prompt,
                run_directory,
                experiment,
                min(remaining, DEFAULT_EXPERIMENT_TIMEOUT_SECONDS),
            )
            if provider_result.proposal is None:
                record = {
                    "experiment": experiment,
                    "status": provider_result.status,
                    "reason": provider_result.detail,
                    "description": "no valid proposal",
                }
                experiments.append(record)
                _append_result(results_path, {"type": "experiment", **record})
                consecutive_provider_failures += 1
                if consecutive_provider_failures >= 2:
                    progress("Stopping after two consecutive provider failures.")
                    break
                continue

            consecutive_provider_failures = 0
            proposal = provider_result.proposal
            patch_path = run_directory / f"proposal-{experiment:03d}.patch"
            try:
                apply_proposal(worktree, proposal, patch_path)
            except ResearchError as exc:
                _restore_router(worktree)
                record = {
                    "experiment": experiment,
                    "status": "invalid_patch",
                    "reason": str(exc),
                    "description": proposal.description,
                }
                experiments.append(record)
                _append_result(results_path, {"type": "experiment", **record})
                continue

            try:
                candidate = evaluate_candidate(worktree / ROUTER_RELATIVE_PATH, cases)
            except ResearchError as exc:
                _restore_router(worktree)
                record = {
                    "experiment": experiment,
                    "status": "evaluation_error",
                    "reason": str(exc),
                    "description": proposal.description,
                }
                experiments.append(record)
                _append_result(results_path, {"type": "experiment", **record})
                continue

            promote, reason = should_promote(best, candidate)
            if promote:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    promote = False
                    reason = "run budget expired before verification"
                else:
                    gate_ok, gate_detail = run_gate(
                        worktree,
                        test_command,
                        run_directory / f"tests-{experiment:03d}.log",
                        min(remaining, DEFAULT_EXPERIMENT_TIMEOUT_SECONDS),
                    )
                    if not gate_ok:
                        promote = False
                        reason = gate_detail

            if promote:
                commit = _commit_candidate(
                    worktree, run_directory, experiment, proposal.description
                )
                best = candidate
                kept += 1
                status = "kept"
                progress(f"Experiment {experiment}: kept ({reason}).")
            else:
                _restore_router(worktree)
                commit = _run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
                status = "discarded"
                progress(f"Experiment {experiment}: discarded ({reason}).")

            record = {
                "experiment": experiment,
                "status": status,
                "reason": reason,
                "description": proposal.description,
                "commit": commit,
                "metrics": candidate.as_dict(),
            }
            experiments.append(record)
            _append_result(results_path, {"type": "experiment", **record})

        report_path.write_text(
            _report_text(
                branch,
                base_commit,
                provider_name,
                max_experiments,
                max_minutes,
                baseline,
                best,
                experiments,
                results_path,
            ),
            encoding="utf-8",
        )
    finally:
        if worktree.exists():
            _run(["git", "worktree", "remove", "--force", str(worktree)], repo, check=False)
        shutil.rmtree(temporary_root, ignore_errors=True)

    if baseline is None or best is None or not report_path.is_file():
        raise ResearchError(f"Research run did not produce a report. Artifacts: {run_directory}")
    return ResearchRunResult(
        branch=branch,
        report_path=report_path,
        results_path=results_path,
        experiments_run=len(experiments),
        kept_experiments=kept,
        baseline=baseline,
        best=best,
    )
