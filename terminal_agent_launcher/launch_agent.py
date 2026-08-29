from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


LABEL = "io.github.cbolden15.terminal-agent-launcher"
LEGACY_LABEL = "com.calebbolden.agent-launchpad"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
LEGACY_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LEGACY_LABEL}.plist"
STATE_DIR = Path.home() / "Library" / "Logs" / "TerminalAgentLauncher"
DEFAULT_URL = "http://127.0.0.1:4317"


def plist_payload(python_executable: str | None = None) -> dict[str, Any]:
    executable = python_executable or sys.executable
    return {
        "Label": LABEL,
        "ProgramArguments": [
            executable,
            "-m",
            "terminal_agent_launcher",
            "serve",
            "--port",
            "4317",
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 5,
        "StandardOutPath": str(STATE_DIR / "server.log"),
        "StandardErrorPath": str(STATE_DIR / "server.error.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
    }


def write_plist(payload: dict[str, Any], path: Path = PLIST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f"{LABEL}-",
        suffix=".plist.tmp",
        delete=False,
    ) as handle:
        plistlib.dump(payload, handle, sort_keys=True)
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def launchctl_target() -> str:
    return f"gui/{os.getuid()}"


def run_launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def remove_registration(label: str, plist_path: Path) -> bool:
    existed = plist_path.exists()
    run_launchctl(
        "bootout", f"{launchctl_target()}/{label}", check=False
    )
    plist_path.unlink(missing_ok=True)
    return existed


def wait_until_ready(url: str = DEFAULT_URL, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.15)
    return False


def install(open_browser: bool = True) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("The LaunchAgent installer requires macOS.")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    remove_registration(LABEL, PLIST_PATH)
    remove_registration(LEGACY_LABEL, LEGACY_PLIST_PATH)
    write_plist(plist_payload())
    run_launchctl("bootstrap", launchctl_target(), str(PLIST_PATH))
    run_launchctl("kickstart", "-k", f"{launchctl_target()}/{LABEL}")

    ready = wait_until_ready()
    if open_browser:
        webbrowser.open(DEFAULT_URL)
    if not ready:
        raise RuntimeError(
            f"LaunchAgent started but {DEFAULT_URL} did not become ready. "
            f"Check {STATE_DIR / 'server.error.log'}."
        )
    return PLIST_PATH


def uninstall() -> bool:
    if sys.platform != "darwin":
        raise RuntimeError("The LaunchAgent installer requires macOS.")
    removed = remove_registration(LABEL, PLIST_PATH)
    removed_legacy = remove_registration(LEGACY_LABEL, LEGACY_PLIST_PATH)
    return removed or removed_legacy
