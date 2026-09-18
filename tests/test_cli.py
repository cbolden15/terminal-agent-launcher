from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from terminal_agent_launcher import cli
from terminal_agent_launcher.server import ConfigError, load_config, save_config


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.root = self.base / "Projects"
        self.root.mkdir()
        self.payments = self.root / "Payments API"
        self.payments.mkdir()
        self.payments_worker = self.root / "Payments Worker"
        self.payments_worker.mkdir()
        self.launcher = self.root / "Terminal Agent Launcher"
        self.launcher.mkdir()
        self.list_project = self.root / "list"
        self.list_project.mkdir()
        self.config_path = self.base / "config.json"
        save_config(
            {
                "locations": [{"path": str(self.root), "kind": "root"}],
                "ignored_names": [],
                "aliases": {},
            },
            self.config_path,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def projects(self) -> list[dict]:
        return [
            {"name": "Payments API", "path": str(self.payments.resolve())},
            {"name": "Payments Worker", "path": str(self.payments_worker.resolve())},
            {"name": "Terminal Agent Launcher", "path": str(self.launcher.resolve())},
            {"name": "list", "path": str(self.list_project.resolve())},
        ]

    def test_resolver_precedence_is_alias_path_name_then_partial(self) -> None:
        projects = self.projects()
        aliases = {"Payments API": projects[1]["path"]}

        self.assertEqual(
            cli.resolve_project("Payments API", projects, aliases), projects[1]
        )
        self.assertEqual(
            cli.resolve_project(projects[0]["path"], projects, aliases), projects[0]
        )
        self.assertEqual(
            cli.resolve_project("Terminal Agent Launcher", projects, aliases), projects[2]
        )
        self.assertEqual(cli.resolve_project("launcher", projects), projects[2])

    def test_resolver_reports_ambiguous_unknown_and_stale_aliases(self) -> None:
        projects = self.projects()

        with self.assertRaisesRegex(cli.SelectorError, "Payments API .*Payments Worker"):
            cli.resolve_project("payments", projects)
        with self.assertRaisesRegex(cli.SelectorError, "Available: .*Payments API"):
            cli.resolve_project("missing", projects)
        with self.assertRaisesRegex(cli.SelectorError, "stale.*Payments API"):
            cli.resolve_project("pay", projects, {"pay": "/missing"})

    def test_aliases_are_persisted_and_validated(self) -> None:
        config = load_config(self.config_path)
        project = self.projects()[0]
        cli.add_alias(config, "payments", project)
        save_config(config, self.config_path)

        self.assertEqual(load_config(self.config_path)["aliases"], {"payments": project["path"]})
        with self.assertRaisesRegex(ConfigError, "already exists"):
            cli.add_alias(config, "PAYMENTS", project)
        with self.assertRaisesRegex(ConfigError, "cannot be empty"):
            cli.add_alias(config, "  ", project)
        cli.remove_alias(config, "payments")
        self.assertEqual(config["aliases"], {})
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            cli.remove_alias(config, "payments")

    def test_detect_agent_only_accepts_detected_supported_commands(self) -> None:
        executable = str(Path(__file__).resolve())
        with patch("terminal_agent_launcher.cli.shutil.which", return_value=executable):
            self.assertEqual(cli.detect_agent("codex"), executable)
        with patch("terminal_agent_launcher.cli.shutil.which", return_value=None):
            with self.assertRaisesRegex(cli.AgentNotFoundError, "not found"):
                cli.detect_agent("claude")
        with self.assertRaisesRegex(ConfigError, "claude or codex"):
            cli.detect_agent("other")

    def test_detect_agent_resolves_relative_path_results_before_launch(self) -> None:
        executable = self.base / "bin" / "codex"
        executable.parent.mkdir()
        executable.touch()
        relative_executable = os.path.relpath(executable, Path.cwd())

        with patch(
            "terminal_agent_launcher.cli.shutil.which", return_value=relative_executable
        ):
            self.assertEqual(cli.detect_agent("codex"), str(executable.resolve()))

    def test_foreground_launch_uses_literal_argument_vector_and_project_cwd(self) -> None:
        completed = Mock(returncode=23)
        project = self.projects()[0]
        with (
            patch("terminal_agent_launcher.cli.detect_agent", return_value="/bin/codex"),
            patch("terminal_agent_launcher.cli.subprocess.run", return_value=completed) as run,
        ):
            self.assertEqual(cli.launch_project(project, "codex"), 23)

        run.assert_called_once_with(["/bin/codex"], cwd=project["path"], check=False)

    def test_cli_alias_commands_and_launch_exit_status(self) -> None:
        project = self.projects()[0]
        self.assertEqual(
            cli.main(["--config", str(self.config_path), "alias", "add", "pay", project["path"]]),
            0,
        )
        self.assertEqual(load_config(self.config_path)["aliases"]["pay"], project["path"])

        with patch("terminal_agent_launcher.cli.launch_project", return_value=19) as launch:
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "pay", "--agent", "codex"]),
                19,
            )
        launched_project, launched_agent = launch.call_args.args
        self.assertEqual(launched_project["path"], project["path"])
        self.assertEqual(launched_agent, "codex")

        self.assertEqual(
            cli.main(["--config", str(self.config_path), "alias", "remove", "pay"]),
            0,
        )
        self.assertEqual(load_config(self.config_path)["aliases"], {})

    def test_cli_lists_discovered_projects(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--config", str(self.config_path), "list"]), 0)

        self.assertIn(f"Payments API\t{self.payments.resolve()}", output.getvalue())
        self.assertIn(
            f"Terminal Agent Launcher\t{self.launcher.resolve()}", output.getvalue()
        )

    def test_cli_catalogs_nested_repositories_for_listing_aliases_and_selectors(self) -> None:
        nested = self.root / "oss" / "agent-launchpad"
        nested.mkdir(parents=True)
        (nested / ".git").mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--config", str(self.config_path), "list"]), 0)
        self.assertIn(f"agent-launchpad\t{nested.resolve()}", output.getvalue())
        self.assertNotIn(f"oss\t{self.root / 'oss'}", output.getvalue())

        self.assertEqual(
            cli.main(
                [
                    "--config",
                    str(self.config_path),
                    "alias",
                    "add",
                    "launcher",
                    str(nested),
                ]
            ),
            0,
        )
        with patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch:
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "agent-launchpad", "--agent", "codex"]
                ),
                0,
            )
        self.assertEqual(launch.call_args.args[0]["path"], str(nested.resolve()))

    def test_cli_explicit_agent_disambiguates_list_project_and_alias(self) -> None:
        with patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch:
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "list", "--agent", "codex"]),
                0,
            )
        self.assertEqual(launch.call_args.args[0]["path"], str(self.list_project.resolve()))

        config = load_config(self.config_path)
        config["aliases"]["list"] = str(self.payments.resolve())
        save_config(config, self.config_path)
        with patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch:
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "list", "--agent", "codex"]),
                0,
            )
        self.assertEqual(launch.call_args.args[0]["path"], str(self.payments.resolve()))

    def test_cli_rejects_missing_agent_and_unknown_selector_without_launching(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(cli.main(["--config", str(self.config_path), "Payments API"]), 2)
            self.assertEqual(cli.main(["--config", str(self.config_path), "missing", "--agent", "codex"]), 2)
            with patch("terminal_agent_launcher.cli.shutil.which", return_value=None):
                self.assertEqual(
                    cli.main(
                        ["--config", str(self.config_path), "Payments API", "--agent", "codex"]
                    ),
                    2,
                )

        self.assertIn("requires --agent", errors.getvalue())
        self.assertIn("No project matches", errors.getvalue())
        self.assertIn("codex was not found", errors.getvalue())

    def test_cli_never_launches_unknown_ambiguous_or_stale_selectors(self) -> None:
        config = load_config(self.config_path)
        config["aliases"]["stale"] = "/missing"
        save_config(config, self.config_path)

        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch("terminal_agent_launcher.cli.launch_project") as launch,
        ):
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "missing", "--agent", "codex"]),
                2,
            )
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "payments", "--agent", "codex"]),
                2,
            )
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "stale", "--agent", "codex"]),
                2,
            )

        launch.assert_not_called()
        self.assertIn("No project matches", errors.getvalue())
        self.assertIn("Ambiguous project", errors.getvalue())
        self.assertIn("Alias 'stale' is stale", errors.getvalue())

    def test_cli_routes_unresolved_task_text_but_stale_alias_remains_terminal(self) -> None:
        (self.launcher / "README.md").write_text(
            "Terminal Agent Launcher project routing", encoding="utf-8"
        )
        config = load_config(self.config_path)
        config["aliases"]["stale alias"] = "/missing"
        save_config(config, self.config_path)

        with patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch:
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "change Terminal Agent Launcher project routing",
                        "--agent",
                        "codex",
                    ]
                ),
                0,
            )
        self.assertEqual(launch.call_args.args[0]["path"], str(self.launcher.resolve()))

        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "stale alias", "--agent", "codex"]
                ),
                2,
            )
        self.assertIn("Alias 'stale alias' is stale", errors.getvalue())

    def test_cli_route_previews_evidence_without_launching(self) -> None:
        (self.launcher / "README.md").write_text(
            "Terminal Agent Launcher project routing", encoding="utf-8"
        )
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.launch_project") as launch,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "route",
                        "change Terminal Agent Launcher project routing",
                    ]
                ),
                0,
            )
        launch.assert_not_called()
        self.assertIn(str(self.launcher.resolve()), output.getvalue())
        self.assertIn("matched: launcher, terminal", output.getvalue())

    def test_cli_reports_launch_oserror_without_traceback(self) -> None:
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch("terminal_agent_launcher.cli.detect_agent", return_value="/bin/codex"),
            patch(
                "terminal_agent_launcher.cli.subprocess.run",
                side_effect=OSError("permission denied"),
            ),
        ):
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "Payments API", "--agent", "codex"]
                ),
                2,
            )

        self.assertIn("tal: Could not start codex: permission denied", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
