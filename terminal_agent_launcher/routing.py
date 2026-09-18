"""Deterministic, local task-to-project routing for the CLI."""

from __future__ import annotations

from dataclasses import dataclass
from math import log
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


METADATA_FILENAMES = ("AGENTS.md", "CLAUDE.md", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
MAX_METADATA_BYTES = 16 * 1024
MINIMUM_SCORE = 3.0
# A winner must lead the runner-up by at least half a weighted term.  Exact
# ties remain rejected, while a strong multi-term score is not discarded over
# a small amount of shared metadata vocabulary.
MINIMUM_MARGIN = 0.5
NAME_WEIGHT = 10.0
PATH_WEIGHT = 5.0
METADATA_WEIGHT = 0.75

# These words describe work generally, not a repository.  Corpus-wide common
# terms are also removed below, so this is deliberately a short, stable list.
COMMON_TERMS = frozenset(
    {
        "a",
        "agent",
        "an",
        "and",
        "change",
        "for",
        "fix",
        "in",
        "my",
        "of",
        "on",
        "project",
        "the",
        "to",
        "update",
        "want",
        "was",
        "which",
        "with",
        "work",
        "worked",
    }
)
_TOKEN_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^a-zA-Z0-9]+")
_MARKDOWN_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class RepositoryProfile:
    """The bounded local text available to the deterministic router."""

    project: dict[str, Any]
    name_terms: frozenset[str]
    path_terms: frozenset[str]
    metadata_terms: frozenset[str]
    metadata_documents: tuple[frozenset[str], ...]

    @property
    def terms(self) -> frozenset[str]:
        return self.name_terms | self.path_terms | self.metadata_terms


@dataclass(frozen=True)
class RoutingCandidate:
    project: dict[str, Any]
    score: float
    strongest_terms: tuple[str, ...]


@dataclass(frozen=True)
class RoutingEvidence:
    """Structured local evidence for either a selected or rejected route."""

    project: dict[str, Any] | None
    score: float
    confidence_margin: float
    strongest_terms: tuple[str, ...]
    candidates: tuple[RoutingCandidate, ...]

    @property
    def is_confident(self) -> bool:
        return self.project is not None


def tokenize(value: str) -> tuple[str, ...]:
    """Split punctuation and camel case into normalized, useful task terms."""
    if not isinstance(value, str):
        return ()
    return tuple(
        token.casefold()
        for token in _TOKEN_BOUNDARY.split(value)
        if len(token) > 1 and token.casefold() not in COMMON_TERMS
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _metadata_files(project_root: Path) -> Iterable[Path]:
    try:
        children = list(project_root.iterdir())
    except OSError:
        return ()
    allowed = set(METADATA_FILENAMES)
    return sorted(
        (
            child
            for child in children
            if child.name in allowed or child.name.startswith("README")
        ),
        key=lambda child: child.name.casefold(),
    )


def _is_markdown_metadata(candidate: Path) -> bool:
    return candidate.suffix.casefold() in {".md", ".markdown"}


def _without_fenced_markdown_blocks(content: str) -> str:
    """Remove Markdown fenced blocks so examples cannot influence routing."""
    visible_lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in content.splitlines(keepends=True):
        line_without_newline = line.rstrip("\r\n")
        if fence_character is not None:
            closing_fence = re.fullmatch(
                rf" {{0,3}}{re.escape(fence_character)}{{{fence_length},}} *",
                line_without_newline,
            )
            if closing_fence:
                fence_character = None
            continue
        opening_fence = _MARKDOWN_FENCE_OPEN.match(line_without_newline)
        if opening_fence:
            fence_character = opening_fence.group(1)[0]
            fence_length = len(opening_fence.group(1))
            continue
        visible_lines.append(line)
    return "".join(visible_lines)


def _read_metadata(project_root: Path) -> tuple[frozenset[str], tuple[frozenset[str], ...]]:
    terms: set[str] = set()
    documents: list[frozenset[str]] = []
    for candidate in _metadata_files(project_root):
        try:
            resolved = candidate.resolve(strict=True)
            if not _is_within(resolved, project_root) or not resolved.is_file():
                continue
            with resolved.open("rb") as handle:
                content = handle.read(MAX_METADATA_BYTES)
        except OSError:
            continue
        text = content.decode("utf-8", errors="ignore")
        if _is_markdown_metadata(candidate):
            text = _without_fenced_markdown_blocks(text)
        document_terms = frozenset(tokenize(text))
        terms.update(document_terms)
        documents.append(document_terms)
    return frozenset(terms), tuple(documents)


def _relative_path(project_root: Path, roots: Sequence[Path]) -> Path:
    matching_roots = [root for root in roots if _is_within(project_root, root)]
    if not matching_roots:
        return Path(project_root.name)
    root = max(matching_roots, key=lambda candidate: len(candidate.parts))
    try:
        return project_root.relative_to(root)
    except ValueError:
        return Path(project_root.name)


def build_profiles(
    projects: Sequence[dict[str, Any]], roots: Sequence[Path | str] = ()
) -> tuple[RepositoryProfile, ...]:
    """Build profiles from repository-local, allowlisted, bounded input only."""
    canonical_roots: list[Path] = []
    for root in roots:
        try:
            canonical_roots.append(Path(root).resolve(strict=True))
        except OSError:
            continue

    profiles: list[RepositoryProfile] = []
    for project in projects:
        try:
            project_root = Path(project["path"]).resolve(strict=True)
            if not project_root.is_dir():
                continue
        except (KeyError, OSError, TypeError):
            continue
        relative = _relative_path(project_root, canonical_roots)
        metadata_terms, metadata_documents = _read_metadata(project_root)
        profiles.append(
            RepositoryProfile(
                project=project,
                name_terms=frozenset(tokenize(project_root.name)),
                path_terms=frozenset(tokenize(str(relative))),
                metadata_terms=metadata_terms,
                metadata_documents=metadata_documents,
            )
        )
    return tuple(profiles)


def _idf(profiles: Sequence[RepositoryProfile], term: str) -> float:
    document_frequency = sum(term in profile.terms for profile in profiles)
    return log((len(profiles) + 1) / (document_frequency + 1)) + 1.0


def route_task(
    task: str,
    projects: Sequence[dict[str, Any]],
    roots: Sequence[Path | str] = (),
) -> RoutingEvidence:
    """Rank local projects and select only a clear, sufficiently strong winner.

    A term contributes once per source. Repository names outrank relative path
    components, which outrank metadata; IDF downweights corpus-common terms.
    """
    task_terms = frozenset(tokenize(task))
    profiles = build_profiles(projects, roots)
    if not task_terms or not profiles:
        return RoutingEvidence(None, 0.0, 0.0, (), ())

    useful_terms = {
        term
        for term in task_terms
        if sum(term in profile.terms for profile in profiles) / len(profiles) < 0.75
        or sum(term in profile.name_terms for profile in profiles) == 1
    }
    if not useful_terms:
        return RoutingEvidence(None, 0.0, 0.0, (), ())

    candidates: list[RoutingCandidate] = []
    for profile in profiles:
        contributions: list[tuple[str, float]] = []
        for term in useful_terms:
            source_weight = max(
                NAME_WEIGHT if term in profile.name_terms else 0.0,
                PATH_WEIGHT if term in profile.path_terms else 0.0,
                METADATA_WEIGHT if term in profile.metadata_terms else 0.0,
            )
            if source_weight:
                contributions.append((term, source_weight * _idf(profiles, term)))
        coverage = (
            len(contributions) / len(useful_terms)
            if len(useful_terms) >= 4
            else 1.0
        )
        coherent_terms = max(
            (len(useful_terms & document) for document in profile.metadata_documents),
            default=0,
        )
        # A task whose terms occur together in one allowlisted file is stronger
        # evidence than the same words scattered across unrelated files.
        coherence_bonus = max(coherent_terms - 1, 0) * 2.0
        score = (sum(contribution for _, contribution in contributions) + coherence_bonus) * coverage
        strongest_terms = tuple(
            term
            for term, _ in sorted(contributions, key=lambda item: (-item[1], item[0]))[:3]
        )
        candidates.append(RoutingCandidate(profile.project, score, strongest_terms))

    candidates.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.project["name"].casefold(),
            candidate.project["path"],
        )
    )
    ranked = tuple(candidate for candidate in candidates if candidate.score > 0)
    if not ranked:
        return RoutingEvidence(None, 0.0, 0.0, (), ())

    winner = ranked[0]
    runner_up_score = ranked[1].score if len(ranked) > 1 else 0.0
    margin = winner.score - runner_up_score
    selected = (
        winner.project
        if winner.score >= MINIMUM_SCORE and margin >= MINIMUM_MARGIN
        else None
    )
    return RoutingEvidence(selected, winner.score, margin, winner.strongest_terms, ranked)
