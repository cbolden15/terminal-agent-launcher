from __future__ import annotations

import json
import plistlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from terminal_agent_launcher import launch_agent
from terminal_agent_launcher.launch_agent import (
    LABEL,
    LEGACY_LABEL,
    install,
    plist_payload,
    uninstall,
    write_plist,
)
from terminal_agent_launcher.server import (
    ConfigError,
    add_location,
    build_launch_command,
    create_server,
    discover_projects,
    load_config,
    prepare_launch,
    remove_location,
    run_iterm_launch,
    save_config,
)


class FolderDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "Projects"
        self.root.mkdir()
        (self.root / "Alpha").mkdir()
        (self.root / "Alpha" / ".git").mkdir()
        (self.root / "beta-2").mkdir()
        (self.root / ".hidden").mkdir()
        (self.root / "node_modules").mkdir()
        (self.root / "notes.txt").write_text("not a project", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self) -> dict:
        return {
            "locations": [{"path": str(self.root), "kind": "root"}],
            "ignored_names": ["node_modules"],
        }

    def test_discovers_visible_directories_and_git_state(self) -> None:
        projects = discover_projects(self.config())

        self.assertEqual([project["name"] for project in projects], ["Alpha", "beta-2"])
        self.assertTrue(projects[0]["is_git"])
        self.assertFalse(projects[1]["is_git"])

    def test_explicit_folder_is_deduplicated_against_root(self) -> None:
        config = self.config()
        config["locations"].append(
            {"path": str(self.root / "Alpha"), "kind": "folder"}
        )

        projects = discover_projects(config)

        self.assertEqual(sum(project["name"] == "Alpha" for project in projects), 1)

    def test_add_and_remove_location(self) -> None:
        config = self.config()
        standalone = self.base / "Standalone"
        standalone.mkdir()

        add_location(config, str(standalone), "folder")
        self.assertEqual(config["locations"][-1]["path"], str(standalone.resolve()))

        with self.assertRaisesRegex(ConfigError, "already listed"):
            add_location(config, str(standalone), "folder")

        remove_location(config, str(standalone), "folder")
        self.assertEqual(len(config["locations"]), 1)

    def test_missing_location_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            add_location(self.config(), str(self.base / "missing"), "folder")

    def test_legacy_config_is_migrated_without_data_loss(self) -> None:
        canonical = self.base / "new" / "config.json"
        legacy = self.base / "old" / "config.json"
        original = self.config()
        original["ignored_names"].append("vendor")
        save_config(original, legacy)

        migrated = load_config(canonical, legacy)

        expected = {**original, "aliases": {}}
        self.assertEqual(migrated, expected)
        self.assertEqual(load_config(canonical), expected)
        self.assertTrue(legacy.exists())

    def test_old_configuration_gets_an_empty_aliases_object(self) -> None:
        path = self.base / "config.json"
        path.write_text(json.dumps(self.config()), encoding="utf-8")

        self.assertEqual(load_config(path)["aliases"], {})

    def test_canonical_config_wins_when_both_configs_exist(self) -> None:
        canonical = self.base / "new" / "config.json"
        legacy = self.base / "old" / "config.json"
        canonical_config = self.config()
        canonical_config["ignored_names"] = ["canonical"]
        legacy_config = self.config()
        legacy_config["ignored_names"] = ["legacy"]
        save_config(canonical_config, canonical)
        save_config(legacy_config, legacy)

        self.assertEqual(
            load_config(canonical, legacy), {**canonical_config, "aliases": {}}
        )

    def test_malformed_legacy_config_is_not_replaced_with_defaults(self) -> None:
        canonical = self.base / "new" / "config.json"
        legacy = self.base / "old" / "config.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{not json", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "Could not read"):
            load_config(canonical, legacy)
        self.assertFalse(canonical.exists())


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "Projects"
        self.root.mkdir()
        (self.root / "Project One").mkdir()
        self.config_path = self.base / "config.json"
        save_config(
            {
                "locations": [{"path": str(self.root), "kind": "root"}],
                "ignored_names": [],
            },
            self.config_path,
        )
        self.server = create_server("127.0.0.1", 0, self.config_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(
        self,
        path: str,
        method: str = "GET",
        payload: dict | None = None,
        origin: str | None = None,
        mutation_header: str = "X-Terminal-Agent-Launcher",
    ):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if payload is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    mutation_header: "1",
                    "Origin": origin or self.origin,
                }
            )
        request = urllib.request.Request(
            f"{self.origin}{path}", data=body, headers=headers, method=method
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_state_and_static_page_are_served(self) -> None:
        with self.request("/api/state") as response:
            payload = json.load(response)
        self.assertEqual(payload["projects"][0]["name"], "Project One")
        self.assertEqual(payload["locations"][0]["project_count"], 1)

        with self.request("/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("Terminal Agent Launcher", html)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_same_origin_location_mutation(self) -> None:
        standalone = self.base / "Standalone"
        standalone.mkdir()
        with self.request(
            "/api/locations",
            method="POST",
            payload={"path": str(standalone), "kind": "folder"},
        ) as response:
            payload = json.load(response)

        self.assertEqual(len(payload["projects"]), 2)
        persisted = load_config(self.config_path)
        self.assertEqual(len(persisted["locations"]), 2)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request(
                "/api/locations",
                method="POST",
                payload={"path": str(self.root), "kind": "folder"},
                origin="https://example.com",
            )
        self.assertEqual(context.exception.code, 403)

    def test_legacy_mutation_header_remains_compatible(self) -> None:
        standalone = self.base / "Legacy Header"
        standalone.mkdir()
        with self.request(
            "/api/locations",
            method="POST",
            payload={"path": str(standalone), "kind": "folder"},
            mutation_header="X-Agent-Launchpad",
        ) as response:
            payload = json.load(response)

        self.assertEqual(len(payload["projects"]), 2)

    def test_non_loopback_server_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "localhost binding"):
            create_server("0.0.0.0", 0, self.config_path)

    def test_launch_endpoint_uses_allowlisted_project_and_options(self) -> None:
        project = discover_projects(load_config(self.config_path))[0]
        with patch("terminal_agent_launcher.server.run_iterm_launch") as launch:
            with self.request(
                "/api/launch",
                method="POST",
                payload={
                    "project_id": project["id"],
                    "agent": "codex",
                    "permission": "workspace",
                    "destination": "tab",
                    "confirmed_bypass": False,
                    "path": "/tmp/browser-supplied-path",
                    "command": "rm -rf /",
                },
            ) as response:
                payload = json.load(response)

        self.assertTrue(payload["ok"])
        launch.assert_called_once_with(
            f"codex --cd '{project['path']}' --sandbox workspace-write "
            "--ask-for-approval on-request",
            "tab",
        )

    def test_launch_endpoint_requires_bypass_confirmation(self) -> None:
        project = discover_projects(load_config(self.config_path))[0]
        with patch("terminal_agent_launcher.server.run_iterm_launch") as launch:
            with self.assertRaises(urllib.error.HTTPError) as context:
                self.request(
                    "/api/launch",
                    method="POST",
                    payload={
                        "project_id": project["id"],
                        "agent": "claude",
                        "permission": "bypass",
                        "destination": "window",
                        "confirmed_bypass": False,
                    },
                )

        self.assertEqual(context.exception.code, 400)
        launch.assert_not_called()


class LaunchCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.project = self.base / "O'Brien Project"
        self.project.mkdir()
        self.config = {
            "locations": [{"path": str(self.project), "kind": "folder"}],
            "ignored_names": [],
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_builds_agent_specific_commands(self) -> None:
        quoted_path = f"'{self.project}'".replace("O'Brien", "O'\\''Brien")
        self.assertEqual(
            build_launch_command(self.project, "claude", "accept-edits"),
            f"cd {quoted_path} && claude --permission-mode acceptEdits",
        )
        self.assertEqual(
            build_launch_command(self.project, "codex", "autonomous"),
            f"codex --cd {quoted_path} --sandbox workspace-write "
            "--ask-for-approval never",
        )

    def test_prepare_launch_requires_a_known_project_id(self) -> None:
        with self.assertRaisesRegex(ConfigError, "no longer available"):
            prepare_launch(
                self.config,
                {
                    "project_id": "not-a-project",
                    "path": "/tmp/injected",
                    "agent": "codex",
                    "permission": "workspace",
                    "destination": "tab",
                },
            )

    def test_prepare_launch_rejects_unsupported_permission(self) -> None:
        project = discover_projects(self.config)[0]
        with self.assertRaisesRegex(ConfigError, "Unsupported Claude"):
            prepare_launch(
                self.config,
                {
                    "project_id": project["id"],
                    "agent": "claude",
                    "permission": "invented-mode",
                    "destination": "window",
                },
            )

    def test_iterm_launch_uses_script_arguments_not_interpolation(self) -> None:
        with patch("terminal_agent_launcher.server.subprocess.run") as subprocess_run:
            run_iterm_launch("command with 'quotes'", "window")

        arguments, keywords = subprocess_run.call_args
        self.assertEqual(
            arguments[0],
            [
                "/usr/bin/osascript",
                "-",
                "command with 'quotes'",
                "window",
            ],
        )
        self.assertIn("write text launchCommand", keywords["input"])
        self.assertTrue(keywords["check"])


class LaunchAgentTests(unittest.TestCase):
    def test_plist_runs_local_server_from_project(self) -> None:
        payload = plist_payload("/usr/bin/python3")

        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(payload["ProgramArguments"][0], "/usr/bin/python3")
        self.assertIn("terminal_agent_launcher", payload["ProgramArguments"])
        self.assertNotIn("--host", payload["ProgramArguments"])
        self.assertEqual(Path(payload["WorkingDirectory"]), launch_agent.PROJECT_ROOT)

    def test_plist_writer_produces_valid_plist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.plist"
            write_plist(plist_payload("/usr/bin/python3"), path)

            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual(payload["Label"], LABEL)

    def test_install_removes_the_legacy_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            plist_path = base / f"{LABEL}.plist"
            legacy_path = base / f"{LEGACY_LABEL}.plist"
            legacy_path.write_text("legacy", encoding="utf-8")
            with (
                patch.object(launch_agent, "PLIST_PATH", plist_path),
                patch.object(launch_agent, "LEGACY_PLIST_PATH", legacy_path),
                patch.object(launch_agent, "STATE_DIR", base / "logs"),
                patch.object(launch_agent, "run_launchctl") as run_launchctl,
                patch.object(launch_agent, "write_plist"),
                patch.object(launch_agent, "wait_until_ready", return_value=True),
            ):
                installed_path = install(open_browser=False)

            self.assertEqual(installed_path, plist_path)
            self.assertFalse(legacy_path.exists())
            bootout_targets = [
                call.args[1]
                for call in run_launchctl.call_args_list
                if call.args[0] == "bootout"
            ]
            self.assertIn(f"{launch_agent.launchctl_target()}/{LABEL}", bootout_targets)
            self.assertIn(
                f"{launch_agent.launchctl_target()}/{LEGACY_LABEL}",
                bootout_targets,
            )

    def test_uninstall_removes_new_and_legacy_plists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            plist_path = base / f"{LABEL}.plist"
            legacy_path = base / f"{LEGACY_LABEL}.plist"
            plist_path.touch()
            legacy_path.touch()
            with (
                patch.object(launch_agent, "PLIST_PATH", plist_path),
                patch.object(launch_agent, "LEGACY_PLIST_PATH", legacy_path),
                patch.object(launch_agent, "run_launchctl"),
            ):
                removed = uninstall()

            self.assertTrue(removed)
            self.assertFalse(plist_path.exists())
            self.assertFalse(legacy_path.exists())

    def test_legacy_python_module_exports_the_server(self) -> None:
        from agent_launchpad import server as legacy_server
        from terminal_agent_launcher import server as canonical_server

        self.assertIs(legacy_server.create_server, canonical_server.create_server)


if __name__ == "__main__":
    unittest.main()
