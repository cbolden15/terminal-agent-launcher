"""Private, versioned routing feedback and receipt storage.

This module deliberately has no dependency on the router or CLI.  It owns the
on-disk contracts used by later command and routing seams, keeping normal route
resolution local and making malformed private state fail closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unicodedata
import uuid
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # pragma: no cover - Windows is not a supported runtime, but import safely.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
NORMALIZER_VERSION = 1
APPLICATION_NAME = "terminal-agent-launcher"
ROUTING_DIRECTORY_NAME = "routing"
FEEDBACK_FILENAME = "feedback.jsonl"
RECEIPTS_FILENAME = "receipts.jsonl"
LOCK_SUFFIX = ".lock"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0
MAX_RECEIPTS = 100
RECEIPT_MAX_AGE = timedelta(minutes=15)


class FeedbackError(ValueError):
    """Raised when private feedback state is invalid or unsafe to use."""


class FeedbackBusyError(FeedbackError):
    """Raised when a private-store advisory lock cannot be acquired promptly."""


class ReceiptNotFoundError(FeedbackError):
    """Raised when a requested route receipt does not exist."""


def normalize_task_v1(task: str) -> str:
    """Normalize task text without changing its word order or punctuation."""
    if not isinstance(task, str):
        raise FeedbackError("Task text must be a string.")
    return " ".join(unicodedata.normalize("NFKC", task).casefold().split())


def task_fingerprint_v1(task: str) -> str:
    """Return the stable, exact-match fingerprint for normalized task text."""
    normalized = normalize_task_v1(task)
    if not normalized:
        raise FeedbackError("Task text cannot be empty.")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def project_identity(project: Mapping[str, Any]) -> dict[str, str]:
    """Return a serializable project identity, deriving an ID from its path if needed."""
    if not isinstance(project, Mapping):
        raise FeedbackError("Project identity must be an object.")
    name = project.get("name")
    path = project.get("path")
    project_id = project.get("id")
    if not isinstance(name, str) or not name.strip():
        raise FeedbackError("Project identity requires a nonempty name.")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise FeedbackError("Project identity requires an absolute path.")
    if project_id is None:
        project_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    if not isinstance(project_id, str) or not project_id:
        raise FeedbackError("Project identity requires a nonempty id.")
    return {"id": project_id, "name": name, "path": path}


def xdg_data_home(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    value = environment.get("XDG_DATA_HOME")
    return Path(value) if value else Path.home() / ".local" / "share"


def xdg_state_home(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    value = environment.get("XDG_STATE_HOME")
    return Path(value) if value else Path.home() / ".local" / "state"


def feedback_directory(environ: Mapping[str, str] | None = None) -> Path:
    return xdg_data_home(environ) / APPLICATION_NAME / ROUTING_DIRECTORY_NAME


def receipts_directory(environ: Mapping[str, str] | None = None) -> Path:
    return xdg_state_home(environ) / APPLICATION_NAME / ROUTING_DIRECTORY_NAME


def feedback_path(environ: Mapping[str, str] | None = None) -> Path:
    return feedback_directory(environ) / FEEDBACK_FILENAME


def receipts_path(environ: Mapping[str, str] | None = None) -> Path:
    return receipts_directory(environ) / RECEIPTS_FILENAME


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FeedbackError(f"Could not inspect private store path {path}: {exc}") from exc


def _ensure_private_directory(path: Path) -> Path:
    """Create one application-owned directory with owner-only permissions."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FeedbackError(f"Could not create private store parent {path.parent}: {exc}") from exc
    details = _lstat(path)
    if details is not None:
        if stat.S_ISLNK(details.st_mode):
            raise FeedbackError(f"Private store directory must not be a symlink: {path}")
        if not stat.S_ISDIR(details.st_mode):
            raise FeedbackError(f"Private store path is not a directory: {path}")
    else:
        try:
            path.mkdir(mode=DIRECTORY_MODE)
        except FileExistsError:
            details = _lstat(path)
            if details is None or stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise FeedbackError(f"Private store directory is unsafe: {path}")
        except OSError as exc:
            raise FeedbackError(f"Could not create private store directory {path}: {exc}") from exc
    try:
        path.chmod(DIRECTORY_MODE)
    except OSError as exc:
        raise FeedbackError(f"Could not secure private store directory {path}: {exc}") from exc
    return path


def _ensure_store_directory(path: Path) -> Path:
    """Create the application and routing directories without widening XDG parents."""
    application_directory = path.parent
    _ensure_private_directory(application_directory)
    return _ensure_private_directory(path)


def _prepare_store_path(path: Path) -> Path:
    path = Path(path)
    _ensure_store_directory(path.parent)
    details = _lstat(path)
    if details is not None:
        if stat.S_ISLNK(details.st_mode):
            raise FeedbackError(f"Private store file must not be a symlink: {path}")
        if not stat.S_ISREG(details.st_mode):
            raise FeedbackError(f"Private store path is not a regular file: {path}")
        try:
            path.chmod(FILE_MODE)
        except OSError as exc:
            raise FeedbackError(f"Could not secure private store file {path}: {exc}") from exc
    return path


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + LOCK_SUFFIX)


def _open_private_file(path: Path, flags: int) -> int:
    path = _prepare_store_path(path)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | no_follow, FILE_MODE)
    except OSError as exc:
        raise FeedbackError(f"Could not open private store file {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise FeedbackError(f"Private store path is not a regular file: {path}")
        os.fchmod(descriptor, FILE_MODE)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def advisory_lock(
    path: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold an owner-only, process-released advisory lock for one store path."""
    if fcntl is None:  # pragma: no cover
        raise FeedbackError("Advisory file locking is unavailable on this platform.")
    if timeout_seconds < 0:
        raise FeedbackError("Lock timeout cannot be negative.")
    descriptor = _open_private_file(_lock_path(Path(path)), os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise FeedbackBusyError(
                        f"Private store is busy: {path}. Try again in a moment."
                    )
                time.sleep(min(0.02, max(deadline - time.monotonic(), 0.0)))
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _utc_timestamp(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise FeedbackError("Timestamps must be UTC.")
    return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str = "created_at") -> datetime:
    if not isinstance(value, str):
        raise FeedbackError(f"{field} must be a UTC timestamp.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackError(f"{field} must be a UTC timestamp.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise FeedbackError(f"{field} must be a UTC timestamp.")
    return timestamp.astimezone(timezone.utc)


def _require_string(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise FeedbackError(f"{field} must be a nonempty string.")
    return value


def _validate_project(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise FeedbackError(f"{field} must be a project identity object.")
    try:
        identity = project_identity(value)
    except FeedbackError as exc:
        raise FeedbackError(f"{field}: {exc}") from exc
    if set(value) != {"id", "name", "path"}:
        raise FeedbackError(f"{field} has unsupported fields.")
    return identity


def _validate_observed(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise FeedbackError("observed must be an object.")
    allowed = {"selected_project_id", "score", "margin", "matched_terms"}
    if set(value) - allowed:
        raise FeedbackError("observed has unsupported fields.")
    observed: dict[str, Any] = {}
    selected_project_id = value.get("selected_project_id")
    if selected_project_id is not None:
        if not isinstance(selected_project_id, str) or not selected_project_id:
            raise FeedbackError("observed.selected_project_id must be a nonempty string.")
        observed["selected_project_id"] = selected_project_id
    for field in ("score", "margin"):
        number = value.get(field)
        if number is not None:
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise FeedbackError(f"observed.{field} must be a number.")
            observed[field] = float(number)
    terms = value.get("matched_terms")
    if terms is not None:
        if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
            raise FeedbackError("observed.matched_terms must be a list of strings.")
        observed["matched_terms"] = list(terms)
    return observed


@dataclass(frozen=True)
class FeedbackCorrection:
    """A currently active exact routing correction."""

    feedback_id: str
    task: str
    task_fingerprint: str
    normalizer_version: int
    expected_project: dict[str, str]
    observed: dict[str, Any] | None
    created_at: datetime


def make_teach_event(
    task: str,
    expected_project: Mapping[str, Any],
    *,
    observed: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one validated append-only teach event."""
    identity = project_identity(expected_project)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": "teach",
        "event_id": event_id or uuid.uuid4().hex,
        "created_at": _utc_timestamp(created_at),
        "task": task,
        "task_fingerprint": task_fingerprint_v1(task),
        "normalizer_version": NORMALIZER_VERSION,
        "expected_project": identity,
    }
    validated_observed = _validate_observed(observed)
    if validated_observed is not None:
        record["observed"] = validated_observed
    _validate_feedback_event(record)
    return record


def make_revoke_event(
    feedback_id: str,
    *,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one validated append-only revoke event."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "event": "revoke",
        "event_id": event_id or uuid.uuid4().hex,
        "created_at": _utc_timestamp(created_at),
        "feedback_id": feedback_id,
    }
    _validate_feedback_event(record)
    return record


def _validate_feedback_event(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise FeedbackError("Feedback records must be JSON objects.")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise FeedbackError("Unsupported feedback schema version.")
    event = _require_string(record, "event")
    _require_string(record, "event_id")
    _parse_timestamp(record.get("created_at"))
    if event == "teach":
        allowed = {
            "schema_version", "event", "event_id", "created_at", "task",
            "task_fingerprint", "normalizer_version", "expected_project", "observed",
        }
        if set(record) - allowed:
            raise FeedbackError("Teach event has unsupported fields.")
        task = _require_string(record, "task")
        if record.get("normalizer_version") != NORMALIZER_VERSION:
            raise FeedbackError("Unsupported task normalizer version.")
        fingerprint = _require_string(record, "task_fingerprint")
        if fingerprint != task_fingerprint_v1(task):
            raise FeedbackError("Teach event task fingerprint does not match its task.")
        validated: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event": "teach",
            "event_id": record["event_id"],
            "created_at": record["created_at"],
            "task": task,
            "task_fingerprint": fingerprint,
            "normalizer_version": NORMALIZER_VERSION,
            "expected_project": _validate_project(record.get("expected_project"), "expected_project"),
        }
        observed = _validate_observed(record.get("observed"))
        if observed is not None:
            validated["observed"] = observed
        return validated
    if event == "revoke":
        allowed = {"schema_version", "event", "event_id", "created_at", "feedback_id"}
        if set(record) != allowed:
            raise FeedbackError("Revoke event has unsupported or missing fields.")
        return {
            "schema_version": SCHEMA_VERSION,
            "event": "revoke",
            "event_id": record["event_id"],
            "created_at": record["created_at"],
            "feedback_id": _require_string(record, "feedback_id"),
        }
    raise FeedbackError(f"Unsupported feedback event type: {event}")


def reduce_feedback_events(events: Sequence[Mapping[str, Any]]) -> dict[str, FeedbackCorrection]:
    """Reduce valid ordered events to their active teach corrections by ID."""
    active: dict[str, FeedbackCorrection] = {}
    event_ids: set[str] = set()
    teach_ids: set[str] = set()
    for raw_event in events:
        event = _validate_feedback_event(raw_event)
        event_id = event["event_id"]
        if event_id in event_ids:
            raise FeedbackError(f"Duplicate feedback event ID: {event_id}")
        event_ids.add(event_id)
        if event["event"] == "teach":
            teach_ids.add(event_id)
            active[event_id] = FeedbackCorrection(
                feedback_id=event_id,
                task=event["task"],
                task_fingerprint=event["task_fingerprint"],
                normalizer_version=event["normalizer_version"],
                expected_project=event["expected_project"],
                observed=event.get("observed"),
                created_at=_parse_timestamp(event["created_at"]),
            )
            continue
        feedback_id = event["feedback_id"]
        if feedback_id not in teach_ids:
            raise FeedbackError(f"Revoke references unknown feedback ID: {feedback_id}")
        if feedback_id not in active:
            raise FeedbackError(f"Feedback ID is not active and cannot be revoked: {feedback_id}")
        del active[feedback_id]
    return active


def active_corrections_by_fingerprint(
    corrections: Mapping[str, FeedbackCorrection],
) -> dict[str, tuple[FeedbackCorrection, ...]]:
    """Group active corrections by exact task fingerprint without fuzzy matching."""
    grouped: dict[str, list[FeedbackCorrection]] = {}
    for correction in corrections.values():
        grouped.setdefault(correction.task_fingerprint, []).append(correction)
    return {
        fingerprint: tuple(sorted(items, key=lambda item: (item.created_at, item.feedback_id)))
        for fingerprint, items in grouped.items()
    }


def _read_jsonl(path: Path, validator: Callable[[Any], Any]) -> list[Any]:
    path = _prepare_store_path(path)
    if not path.exists():
        return []
    descriptor = _open_private_file(path, os.O_RDONLY)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        records: list[Any] = []
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                raise FeedbackError(f"Malformed JSONL record at {path}:{number}.")
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FeedbackError(f"Malformed JSONL record at {path}:{number}.") from exc
            try:
                records.append(validator(decoded))
            except FeedbackError as exc:
                raise FeedbackError(f"Invalid record at {path}:{number}: {exc}") from exc
        return records


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise FeedbackError("Could not complete private store write.")
        view = view[written:]


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    payload = (json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = _open_private_file(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path = _prepare_store_path(path)
    if _lstat(path) is not None and stat.S_ISLNK(_lstat(path).st_mode):
        raise FeedbackError(f"Private store file must not be a symlink: {path}")
    payload = "".join(
        json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(FILE_MODE)
    except OSError as exc:
        raise FeedbackError(f"Could not atomically write private store {path}: {exc}") from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class FeedbackStore:
    """Append-only feedback events with locked writes and fail-closed reads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else feedback_path()

    def events(self) -> list[dict[str, Any]]:
        with advisory_lock(self.path):
            return _read_jsonl(self.path, _validate_feedback_event)

    def active_corrections(self) -> dict[str, FeedbackCorrection]:
        with advisory_lock(self.path):
            return reduce_feedback_events(_read_jsonl(self.path, _validate_feedback_event))

    def append_teach(
        self,
        task: str,
        expected_project: Mapping[str, Any],
        *,
        observed: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FeedbackCorrection:
        event = make_teach_event(
            task, expected_project, observed=observed, event_id=event_id, created_at=created_at
        )
        with advisory_lock(self.path):
            events = _read_jsonl(self.path, _validate_feedback_event)
            reduce_feedback_events([*events, event])
            _append_jsonl(self.path, event)
        return FeedbackCorrection(
            feedback_id=event["event_id"],
            task=event["task"],
            task_fingerprint=event["task_fingerprint"],
            normalizer_version=event["normalizer_version"],
            expected_project=event["expected_project"],
            observed=event.get("observed"),
            created_at=_parse_timestamp(event["created_at"]),
        )

    def append_revoke(
        self,
        feedback_id: str,
        *,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        event = make_revoke_event(feedback_id, event_id=event_id, created_at=created_at)
        with advisory_lock(self.path):
            events = _read_jsonl(self.path, _validate_feedback_event)
            reduce_feedback_events([*events, event])
            _append_jsonl(self.path, event)
        return event


@dataclass(frozen=True)
class RouteReceipt:
    """Private evidence for one confident local route."""

    receipt_id: str
    created_at: datetime
    terminal_id: str
    task: str
    selected_project: dict[str, str]
    evidence: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": self.receipt_id,
            "created_at": _utc_timestamp(self.created_at),
            "terminal_id": self.terminal_id,
            "task": self.task,
            "selected_project": self.selected_project,
            "evidence": self.evidence,
        }


def terminal_identity() -> str | None:
    """Return the current terminal device, or None when this process has no TTY."""
    for descriptor in (0, 1, 2):
        try:
            if os.isatty(descriptor):
                return os.ttyname(descriptor)
        except OSError:
            continue
    return None


def _validate_receipt_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FeedbackError("Receipt evidence must be an object.")
    allowed = {"score", "margin", "matched_terms", "candidates"}
    if set(value) - allowed:
        raise FeedbackError("Receipt evidence has unsupported fields.")
    evidence: dict[str, Any] = {}
    for field in ("score", "margin"):
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise FeedbackError(f"Receipt evidence requires numeric {field}.")
        evidence[field] = float(number)
    terms = value.get("matched_terms")
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise FeedbackError("Receipt evidence requires matched_terms as strings.")
    evidence["matched_terms"] = list(terms)
    candidates = value.get("candidates", [])
    if not isinstance(candidates, list):
        raise FeedbackError("Receipt evidence candidates must be a list.")
    evidence["candidates"] = candidates
    return evidence


def make_route_receipt(
    task: str,
    selected_project: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    terminal_id: str | None = None,
    receipt_id: str | None = None,
    created_at: datetime | None = None,
) -> RouteReceipt:
    """Create a validated receipt; callers must provide a real terminal identity."""
    terminal = terminal_id if terminal_id is not None else terminal_identity()
    if not isinstance(terminal, str) or not terminal:
        raise FeedbackError("A route receipt requires a terminal identity.")
    if not isinstance(task, str) or not task.strip():
        raise FeedbackError("Route receipt task cannot be empty.")
    identifier = receipt_id or uuid.uuid4().hex
    if not isinstance(identifier, str) or not identifier:
        raise FeedbackError("Receipt ID must be a nonempty string.")
    receipt = RouteReceipt(
        receipt_id=identifier,
        created_at=_parse_timestamp(_utc_timestamp(created_at)),
        terminal_id=terminal,
        task=task,
        selected_project=project_identity(selected_project),
        evidence=_validate_receipt_evidence(evidence),
    )
    _validate_receipt(receipt.to_record())
    return receipt


def _validate_receipt(record: Any) -> RouteReceipt:
    if not isinstance(record, Mapping):
        raise FeedbackError("Route receipts must be JSON objects.")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise FeedbackError("Unsupported route receipt schema version.")
    required = {
        "schema_version", "receipt_id", "created_at", "terminal_id", "task",
        "selected_project", "evidence",
    }
    if set(record) != required:
        raise FeedbackError("Route receipt has unsupported or missing fields.")
    task = _require_string(record, "task")
    return RouteReceipt(
        receipt_id=_require_string(record, "receipt_id"),
        created_at=_parse_timestamp(record.get("created_at")),
        terminal_id=_require_string(record, "terminal_id"),
        task=task,
        selected_project=_validate_project(record.get("selected_project"), "selected_project"),
        evidence=_validate_receipt_evidence(record.get("evidence")),
    )


class ReceiptStore:
    """Bounded per-terminal route receipts with locked, atomic rewrites."""

    def __init__(self, path: Path | None = None, *, max_receipts: int = MAX_RECEIPTS) -> None:
        if max_receipts < 1:
            raise FeedbackError("Receipt bound must be at least one.")
        self.path = Path(path) if path is not None else receipts_path()
        self.max_receipts = max_receipts

    def receipts(self) -> list[RouteReceipt]:
        with advisory_lock(self.path):
            return _read_jsonl(self.path, _validate_receipt)

    def record(self, receipt: RouteReceipt) -> RouteReceipt:
        validated = _validate_receipt(receipt.to_record())
        with advisory_lock(self.path):
            receipts = _read_jsonl(self.path, _validate_receipt)
            if any(item.receipt_id == validated.receipt_id for item in receipts):
                raise FeedbackError(f"Duplicate route receipt ID: {validated.receipt_id}")
            retained = [*receipts, validated][-self.max_receipts :]
            _atomic_write_jsonl(self.path, [item.to_record() for item in retained])
        return validated

    def find(self, receipt_id: str) -> RouteReceipt:
        for receipt in self.receipts():
            if receipt.receipt_id == receipt_id:
                return receipt
        raise ReceiptNotFoundError(f"Route receipt not found: {receipt_id}")

    def last_for_terminal(
        self,
        terminal_id: str,
        *,
        now: datetime | None = None,
        max_age: timedelta = RECEIPT_MAX_AGE,
    ) -> RouteReceipt | None:
        if not isinstance(terminal_id, str) or not terminal_id:
            raise FeedbackError("Terminal identity is required.")
        if max_age < timedelta(0):
            raise FeedbackError("Receipt maximum age cannot be negative.")
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time.utcoffset() != timedelta(0):
            raise FeedbackError("Current time must be UTC.")
        matching = [
            receipt
            for receipt in self.receipts()
            if receipt.terminal_id == terminal_id
            and timedelta(0) <= current_time - receipt.created_at < max_age
        ]
        return max(matching, key=lambda item: (item.created_at, item.receipt_id), default=None)
