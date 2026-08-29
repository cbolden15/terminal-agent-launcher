from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = Path(__file__).resolve().parent / "web"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DEFAULT_CONFIG_PATH = CONFIG_HOME / "terminal-agent-launcher" / "config.json"
LEGACY_CONFIG_PATH = CONFIG_HOME / "agent-launchpad" / "config.json"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_IGNORED_NAMES = {
    ".git",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
CLAUDE_PERMISSION_ARGUMENTS = {
    "plan": ("--permission-mode", "plan"),
    "default": ("--permission-mode", "default"),
    "accept-edits": ("--permission-mode", "acceptEdits"),
    "auto": ("--permission-mode", "auto"),
    "bypass": ("--dangerously-skip-permissions",),
}
CODEX_PERMISSION_ARGUMENTS = {
    "read-only": ("--sandbox", "read-only", "--ask-for-approval", "on-request"),
    "workspace": (
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
    ),
    "autonomous": (
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
    ),
    "bypass": ("--dangerously-bypass-approvals-and-sandbox",),
}
LAUNCH_DESTINATIONS = {"tab", "window"}
ITERM_LAUNCH_SCRIPT = """on run argv
    set launchCommand to item 1 of argv
    set launchDestination to item 2 of argv

    tell application "iTerm2"
        activate
        if launchDestination is "window" then
            set targetWindow to (create window with default profile)
            tell current session of targetWindow to write text launchCommand
        else if (count of windows) is 0 then
            set targetWindow to (create window with default profile)
            tell current session of targetWindow to write text launchCommand
        else
            tell current window
                create tab with default profile
                tell current session to write text launchCommand
            end tell
        end if
    end tell
end run
"""


class ConfigError(ValueError):
    """Raised when the local configuration is invalid."""


class ItermLaunchError(RuntimeError):
    """Raised when iTerm2 cannot create the requested session."""


def default_config() -> dict[str, Any]:
    return {
        "locations": [
            {
                "path": str(Path.home() / "Projects"),
                "kind": "root",
            }
        ],
        "ignored_names": sorted(DEFAULT_IGNORED_NAMES),
    }


def save_config(config: dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=config_path.parent,
        prefix="config-",
        suffix=".json.tmp",
        delete=False,
    ) as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(config_path)


def load_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    legacy_config_path: Path | None = None,
) -> dict[str, Any]:
    if legacy_config_path is None and config_path == DEFAULT_CONFIG_PATH:
        legacy_config_path = LEGACY_CONFIG_PATH

    source_path = config_path
    if (
        not config_path.exists()
        and legacy_config_path is not None
        and legacy_config_path.exists()
    ):
        source_path = legacy_config_path

    if not source_path.exists():
        config = default_config()
        save_config(config, config_path)
        return config

    try:
        config = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read {source_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object.")
    if not isinstance(config.get("locations"), list):
        raise ConfigError("Configuration locations must be a list.")

    ignored_names = config.get("ignored_names", sorted(DEFAULT_IGNORED_NAMES))
    if not isinstance(ignored_names, list) or not all(
        isinstance(name, str) for name in ignored_names
    ):
        raise ConfigError("Configuration ignored_names must be a list of strings.")
    config["ignored_names"] = ignored_names

    normalized_locations: list[dict[str, str]] = []
    for location in config["locations"]:
        if not isinstance(location, dict):
            continue
        path = location.get("path")
        kind = location.get("kind")
        if isinstance(path, str) and path and kind in {"root", "folder"}:
            normalized_locations.append({"path": path, "kind": kind})
    config["locations"] = normalized_locations
    if source_path != config_path:
        save_config(config, config_path)
    return config


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def canonical_path(path: Path, *, strict: bool = False) -> Path:
    return path.resolve(strict=strict)


def validate_location(path_value: str, kind: str) -> tuple[Path, str]:
    if kind not in {"root", "folder"}:
        raise ConfigError("Location kind must be root or folder.")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConfigError("Enter a folder path.")

    path = expand_path(path_value.strip())
    try:
        resolved = canonical_path(path, strict=True)
    except OSError as exc:
        raise ConfigError(f"Folder does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ConfigError(f"Not a folder: {resolved}")
    return resolved, kind


def add_location(
    config: dict[str, Any], path_value: str, kind: str
) -> dict[str, Any]:
    resolved, normalized_kind = validate_location(path_value, kind)
    canonical_value = str(resolved)
    for location in config["locations"]:
        existing = canonical_path(expand_path(location["path"]), strict=False)
        if str(existing) == canonical_value and location["kind"] == normalized_kind:
            raise ConfigError("That location is already listed.")

    config["locations"].append(
        {"path": canonical_value, "kind": normalized_kind}
    )
    return config


def remove_location(
    config: dict[str, Any], path_value: str, kind: str
) -> dict[str, Any]:
    target = str(canonical_path(expand_path(path_value), strict=False))
    config["locations"] = [
        location
        for location in config["locations"]
        if not (
            str(
                canonical_path(expand_path(location["path"]), strict=False)
            )
            == target
            and location["kind"] == kind
        )
    ]
    return config


def stable_id(*parts: str) -> str:
    value = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def discover_projects(config: dict[str, Any]) -> list[dict[str, Any]]:
    ignored_names = set(config.get("ignored_names", DEFAULT_IGNORED_NAMES))
    projects: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for location in config["locations"]:
        location_path = canonical_path(
            expand_path(location["path"]), strict=False
        )
        location_id = stable_id(location["kind"], str(location_path))
        if not location_path.is_dir():
            continue

        if location["kind"] == "folder":
            candidates = [location_path]
        else:
            try:
                candidates = sorted(
                    location_path.iterdir(), key=lambda item: item.name.casefold()
                )
            except OSError:
                continue

        for candidate in candidates:
            if location["kind"] == "root" and (
                candidate.name.startswith(".")
                or candidate.name in ignored_names
            ):
                continue
            try:
                if not candidate.is_dir():
                    continue
                resolved = canonical_path(candidate, strict=True)
                canonical_value = str(resolved)
                if canonical_value in seen_paths:
                    continue
                metadata = candidate.stat()
            except OSError:
                continue

            seen_paths.add(canonical_value)
            projects.append(
                {
                    "id": stable_id(canonical_value),
                    "name": candidate.name,
                    "path": canonical_value,
                    "parent": str(resolved.parent),
                    "modified_at": int(metadata.st_mtime),
                    "is_git": (resolved / ".git").exists(),
                    "location_id": location_id,
                }
            )

    projects.sort(key=lambda project: (project["name"].casefold(), project["path"]))
    return projects


def location_state(
    config: dict[str, Any], projects: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for project in projects:
        location_id = project["location_id"]
        counts[location_id] = counts.get(location_id, 0) + 1

    state: list[dict[str, Any]] = []
    for location in config["locations"]:
        path = canonical_path(expand_path(location["path"]), strict=False)
        location_id = stable_id(location["kind"], str(path))
        state.append(
            {
                "id": location_id,
                "name": path.name or str(path),
                "path": str(path),
                "kind": location["kind"],
                "exists": path.is_dir(),
                "project_count": counts.get(location_id, 0),
            }
        )
    return state


def command_status() -> dict[str, Any]:
    return {
        "iterm": Path("/Applications/iTerm.app").is_dir(),
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
    }


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def build_launch_command(path: Path, agent: str, permission: str) -> str:
    quoted_path = shell_quote(str(path))
    if agent == "claude":
        arguments = CLAUDE_PERMISSION_ARGUMENTS.get(permission)
        if arguments is None:
            raise ConfigError("Unsupported Claude permission preset.")
        command = " ".join(("claude", *arguments))
        return f"cd {quoted_path} && {command}"
    if agent == "codex":
        arguments = CODEX_PERMISSION_ARGUMENTS.get(permission)
        if arguments is None:
            raise ConfigError("Unsupported Codex permission preset.")
        return " ".join(("codex", "--cd", quoted_path, *arguments))
    raise ConfigError("Agent must be claude or codex.")


def prepare_launch(
    config: dict[str, Any], payload: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    project_id = payload.get("project_id")
    agent = payload.get("agent")
    permission = payload.get("permission")
    destination = payload.get("destination")
    if not all(
        isinstance(value, str)
        for value in (project_id, agent, permission, destination)
    ):
        raise ConfigError(
            "Project, agent, permission, and destination are required."
        )
    if destination not in LAUNCH_DESTINATIONS:
        raise ConfigError("Destination must be a new tab or new window.")
    if permission == "bypass" and payload.get("confirmed_bypass") is not True:
        raise ConfigError("Full bypass requires explicit confirmation.")

    project = next(
        (
            candidate
            for candidate in discover_projects(config)
            if candidate["id"] == project_id
        ),
        None,
    )
    if project is None:
        raise ConfigError("That project is no longer available.")

    command = build_launch_command(Path(project["path"]), agent, permission)
    return command, destination, project


def run_iterm_launch(command: str, destination: str) -> None:
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-", command, destination],
            input=ITERM_LAUNCH_SCRIPT,
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ItermLaunchError("iTerm2 did not respond in time.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "AppleScript returned an error."
        raise ItermLaunchError(f"Could not open iTerm2: {detail}") from exc


def build_state(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = load_config(config_path)
    projects = discover_projects(config)
    return {
        "projects": projects,
        "locations": location_state(config, projects),
        "status": command_status(),
        "config_path": str(config_path),
    }


class TerminalAgentLauncherHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        config_path: Path,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.config_path = config_path


class RequestHandler(BaseHTTPRequestHandler):
    server: TerminalAgentLauncherHTTPServer

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def _allowed_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]")
        return host in LOOPBACK_HOSTS

    def _allowed_mutation(self) -> bool:
        port = self.server.server_address[1]
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        return (
            self.headers.get("Origin") in allowed_origins
            and (
                self.headers.get("X-Terminal-Agent-Launcher") == "1"
                or self.headers.get("X-Agent-Launchpad") == "1"
            )
        )

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ConfigError("Invalid request length.") from exc
        if content_length <= 0 or content_length > 16_384:
            raise ConfigError("Request body must contain a small JSON object.")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError as exc:
            raise ConfigError("Request body is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ConfigError("Request body must be a JSON object.")
        return payload

    def _serve_static(self, request_path: str) -> None:
        if request_path == "/":
            file_path = WEB_ROOT / "index.html"
        elif request_path.startswith("/assets/"):
            relative_path = unquote(request_path.removeprefix("/"))
            file_path = WEB_ROOT / relative_path
        elif request_path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self._security_headers()
            self.end_headers()
            return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            resolved = file_path.resolve(strict=True)
            resolved.relative_to(WEB_ROOT.resolve(strict=True))
            body = resolved.read_bytes()
        except (OSError, ValueError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type, _ = mimetypes.guess_type(resolved.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._allowed_host():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Localhost requests only.")
            return
        request_path = urlparse(self.path).path
        if request_path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if request_path == "/api/state":
            try:
                self._send_json(build_state(self.server.config_path))
            except ConfigError as exc:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        self._serve_static(request_path)

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path == "/api/locations":
            self._mutate_location("add")
            return
        if request_path == "/api/launch":
            self._launch_session()
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown API route.")

    def do_DELETE(self) -> None:
        self._mutate_location("remove")

    def do_OPTIONS(self) -> None:
        self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "CORS is not enabled.")

    def _mutate_location(self, action: str) -> None:
        if not self._allowed_host() or not self._allowed_mutation():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Request origin was rejected.")
            return
        if urlparse(self.path).path != "/api/locations":
            self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown API route.")
            return
        try:
            payload = self._read_json()
            path_value = payload.get("path")
            kind = payload.get("kind")
            if not isinstance(path_value, str) or not isinstance(kind, str):
                raise ConfigError("Path and kind are required.")
            config = load_config(self.server.config_path)
            if action == "add":
                add_location(config, path_value, kind)
            else:
                remove_location(config, path_value, kind)
            save_config(config, self.server.config_path)
            self._send_json(build_state(self.server.config_path))
        except ConfigError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def _launch_session(self) -> None:
        if not self._allowed_host() or not self._allowed_mutation():
            self._send_error_json(HTTPStatus.FORBIDDEN, "Request origin was rejected.")
            return
        try:
            payload = self._read_json()
            config = load_config(self.server.config_path)
            command, destination, project = prepare_launch(config, payload)
            run_iterm_launch(command, destination)
            self._send_json(
                {
                    "ok": True,
                    "project_id": project["id"],
                    "agent": payload["agent"],
                    "permission": payload["permission"],
                    "destination": destination,
                }
            )
        except ConfigError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except ItermLaunchError as exc:
            self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))


def create_server(
    host: str = "127.0.0.1",
    port: int = 4317,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> TerminalAgentLauncherHTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ConfigError("Terminal Agent Launcher only supports localhost binding.")
    return TerminalAgentLauncherHTTPServer(
        (host, port), RequestHandler, config_path
    )


def serve(
    host: str = "127.0.0.1",
    port: int = 4317,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    server = create_server(host, port, config_path)
    print(
        "Terminal Agent Launcher is running at "
        f"http://{host}:{server.server_address[1]}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
