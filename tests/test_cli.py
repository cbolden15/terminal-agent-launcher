from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from terminal_agent_launcher import cli
from terminal_agent_launcher.feedback import FeedbackStore, ReceiptStore, make_route_receipt
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
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.base / "data"),
                "XDG_STATE_HOME": str(self.base / "state"),
            },
        )
        self.environment.start()
        save_config(
            {
                "locations": [{"path": str(self.root), "kind": "root"}],
                "ignored_names": [],
                "aliases": {},
            },
            self.config_path,
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def projects(self) -> list[dict]:
        return [
            {"name": "Payments API", "path": str(self.payments.resolve())},
            {"name": "Payments Worker", "path": str(self.payments_worker.resolve())},
            {"name": "Terminal Agent Launcher", "path": str(self.launcher.resolve())},
            {"name": "list", "path": str(self.list_project.resolve())},
        ]

    def feedback_store(self) -> FeedbackStore:
        return FeedbackStore(self.base / "data" / "terminal-agent-launcher" / "routing" / "feedback.jsonl")

    def receipt_store(self) -> ReceiptStore:
        return ReceiptStore(self.base / "state" / "terminal-agent-launcher" / "routing" / "receipts.jsonl")

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

    def test_cli_route_launch_prints_evidence_before_launching(self) -> None:
        (self.launcher / "README.md").write_text(
            "Terminal Agent Launcher project routing", encoding="utf-8"
        )
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch,
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "route",
                        "change Terminal Agent Launcher project routing",
                        "--agent",
                        "codex",
                    ]
                ),
                0,
            )

        self.assertEqual(launch.call_args.args[0]["path"], str(self.launcher.resolve()))
        self.assertIn(str(self.launcher.resolve()), output.getvalue())
        self.assertIn("matched: launcher, terminal", output.getvalue())

    def test_cli_task_route_launch_prints_evidence_before_launching(self) -> None:
        (self.launcher / "README.md").write_text(
            "Terminal Agent Launcher project routing", encoding="utf-8"
        )
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch,
        ):
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
        self.assertIn(str(self.launcher.resolve()), output.getvalue())
        self.assertIn("matched: launcher, terminal", output.getvalue())

    def test_taught_exact_task_overrides_routing_in_preview_and_launch_without_provider_calls(self) -> None:
        task = "fix payments api authorization"
        store = self.feedback_store()
        store.append_teach(task, self.projects()[2], event_id="teach-launcher")
        output = io.StringIO()

        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=store),
            patch("terminal_agent_launcher.cli.route_task") as route,
            patch("terminal_agent_launcher.cli.run_research") as provider,
            patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch,
        ):
            self.assertEqual(
                cli.main(["--config", str(self.config_path), "route", task]),
                0,
            )
            self.assertEqual(
                cli.main(["--config", str(self.config_path), task, "--agent", "codex"]),
                0,
            )

        self.assertEqual(launch.call_args.args[0]["path"], str(self.launcher.resolve()))
        self.assertIn(str(self.launcher.resolve()), output.getvalue())
        route.assert_not_called()
        provider.assert_not_called()

    def test_similar_untaught_task_uses_the_heuristic_router(self) -> None:
        store = self.feedback_store()
        store.append_teach("fix payments api", self.projects()[2], event_id="teach-launcher")

        with patch("terminal_agent_launcher.cli.route_task", wraps=cli.route_task) as route:
            project, evidence = cli.resolve_task(
                "fix payments api authorization",
                self.projects(),
                routing_roots=(self.root,),
                feedback_store=store,
            )

        self.assertEqual(project["path"], str(self.payments.resolve()))
        self.assertIsNotNone(evidence)
        route.assert_called_once()

    def test_direct_alias_retains_precedence_over_taught_task(self) -> None:
        task = "fix payments api authorization"
        store = self.feedback_store()
        store.append_teach(task, self.projects()[2], event_id="teach-launcher")

        with patch("terminal_agent_launcher.cli.route_task") as route:
            project, evidence = cli.resolve_task(
                task,
                self.projects(),
                {task: str(self.payments.resolve())},
                feedback_store=store,
            )

        self.assertEqual(project["path"], str(self.payments.resolve()))
        self.assertIsNone(evidence)
        route.assert_not_called()

    def test_stale_taught_correction_stops_routing(self) -> None:
        task = "fix payments api authorization"
        store = self.feedback_store()
        store.append_teach(task, self.projects()[2], event_id="teach-launcher")

        with patch("terminal_agent_launcher.cli.route_task") as route:
            with self.assertRaisesRegex(cli.StaleCorrectionError, "stale"):
                cli.resolve_task(
                    task,
                    self.projects()[:2],
                    routing_roots=(self.root,),
                    feedback_store=store,
                )

        route.assert_not_called()

    def test_taught_correction_rejects_an_ambiguous_catalog_target(self) -> None:
        task = "fix payments api authorization"
        store = self.feedback_store()
        store.append_teach(task, self.projects()[2], event_id="teach-launcher")
        duplicate = dict(self.projects()[2])

        with patch("terminal_agent_launcher.cli.route_task") as route:
            with self.assertRaisesRegex(cli.StaleCorrectionError, "ambiguous"):
                cli.resolve_task(
                    task,
                    [*self.projects(), duplicate],
                    routing_roots=(self.root,),
                    feedback_store=store,
                )

        route.assert_not_called()

    def test_cli_records_confident_heuristic_route_before_agent_launch(self) -> None:
        task = "change Terminal Agent Launcher project routing"
        (self.launcher / "README.md").write_text(
            "Terminal Agent Launcher project routing", encoding="utf-8"
        )
        feedback_store = self.feedback_store()
        receipt_store = self.receipt_store()

        def launch_after_receipt(project, agent):
            receipt = receipt_store.receipts()[0]
            self.assertEqual(receipt.task, task)
            self.assertEqual(receipt.selected_project["path"], project["path"])
            self.assertEqual(agent, "codex")
            return 0

        with (
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=feedback_store),
            patch("terminal_agent_launcher.cli.ReceiptStore", return_value=receipt_store),
            patch("terminal_agent_launcher.cli.terminal_identity", return_value="/dev/ttys-test"),
            patch("terminal_agent_launcher.cli.launch_project", side_effect=launch_after_receipt),
        ):
            self.assertEqual(
                cli.main(["--config", str(self.config_path), task, "--agent", "codex"]),
                0,
            )

    def test_cli_teach_task_lists_shows_and_append_only_revokes_correction(self) -> None:
        store = self.feedback_store()
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=store),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "Terminal Agent Launcher",
                        "--task",
                        "Fix the launch shortcut",
                    ]
                ),
                0,
            )

        correction = next(iter(store.active_corrections().values()))
        self.assertEqual(correction.task, "Fix the launch shortcut")
        self.assertEqual(correction.expected_project["path"], str(self.launcher.resolve()))
        self.assertIn("Recorded taught correction", output.getvalue())

        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=store),
        ):
            self.assertEqual(cli.main(["--config", str(self.config_path), "teach", "--list"]), 0)
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "--show",
                        correction.feedback_id,
                    ]
                ),
                0,
            )
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "--revoke",
                        correction.feedback_id,
                        "--yes",
                    ]
                ),
                0,
            )

        self.assertEqual(store.active_corrections(), {})
        self.assertEqual([event["event"] for event in store.events()], ["teach", "revoke"])
        self.assertIn(correction.feedback_id, output.getvalue())
        self.assertIn("Revoked taught correction", output.getvalue())

    def test_cli_teach_receipt_preserves_observed_route_for_noninteractive_callers(self) -> None:
        feedback_store = self.feedback_store()
        receipt_store = self.receipt_store()
        receipt_store.record(
            make_route_receipt(
                "Fix the launch shortcut",
                self.projects()[0],
                {"score": 8.0, "margin": 1.5, "matched_terms": ["launch"]},
                terminal_id="/dev/ttys-other",
                receipt_id="receipt-1",
                created_at=datetime.now(timezone.utc),
            )
        )

        with (
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=feedback_store),
            patch("terminal_agent_launcher.cli.ReceiptStore", return_value=receipt_store),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "Terminal Agent Launcher",
                        "--receipt",
                        "receipt-1",
                    ]
                ),
                0,
            )

        correction = next(iter(feedback_store.active_corrections().values()))
        self.assertEqual(correction.task, "Fix the launch shortcut")
        self.assertEqual(
            correction.observed["selected_project_id"],
            receipt_store.find("receipt-1").selected_project["id"],
        )
        self.assertEqual(correction.observed["score"], 8.0)

    def test_cli_teach_last_confirms_current_terminal_receipt_and_rejects_noninteractive_use(self) -> None:
        feedback_store = self.feedback_store()
        receipt_store = self.receipt_store()
        receipt_store.record(
            make_route_receipt(
                "Fix the launch shortcut",
                self.projects()[0],
                {"score": 8.0, "margin": 1.5, "matched_terms": ["launch"]},
                terminal_id="/dev/ttys-current",
                receipt_id="receipt-current",
                created_at=datetime.now(timezone.utc),
            )
        )
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch("terminal_agent_launcher.cli.terminal_identity", return_value="/dev/ttys-current"),
            patch("terminal_agent_launcher.cli.ReceiptStore", return_value=receipt_store),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "Terminal Agent Launcher",
                        "--last",
                    ]
                ),
                2,
            )
        self.assertIn("requires an interactive terminal", errors.getvalue())

        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=feedback_store),
            patch("terminal_agent_launcher.cli.ReceiptStore", return_value=receipt_store),
            patch("terminal_agent_launcher.cli.terminal_identity", return_value="/dev/ttys-current"),
            patch("terminal_agent_launcher.cli._is_interactive", return_value=True),
            patch("terminal_agent_launcher.cli._confirm", return_value=True),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "Terminal Agent Launcher",
                        "--last",
                    ]
                ),
                0,
            )

        self.assertIn("Task: Fix the launch shortcut", output.getvalue())
        self.assertIn("Receipt ID: receipt-current", output.getvalue())
        self.assertIn("Age:", output.getvalue())
        self.assertEqual(len(feedback_store.active_corrections()), 1)

    def test_cli_teach_revoke_requires_interactive_confirmation_without_yes(self) -> None:
        store = self.feedback_store()
        correction = store.append_teach("Fix the launch shortcut", self.projects()[2])
        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch("terminal_agent_launcher.cli.FeedbackStore", return_value=store),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "teach",
                        "--revoke",
                        correction.feedback_id,
                    ]
                ),
                2,
            )

        self.assertIn("interactive terminal or --yes", errors.getvalue())
        self.assertIn(correction.feedback_id, store.active_corrections())

    def test_cli_routes_nested_non_git_secondbrain_instead_of_sibling_repository(self) -> None:
        blockdaemon = self.root / "work" / "blockdaemon"
        blockdaemon.mkdir(parents=True)
        second_brain = blockdaemon / "SecondBrain"
        second_brain.mkdir()
        (second_brain / ".codex-test-command").write_text(
            "python3 -m unittest", encoding="utf-8"
        )
        (second_brain / "README.md").write_text(
            "SecondBrain capture for Blockdaemon", encoding="utf-8"
        )
        draftdaemon = blockdaemon / "draftdaemon"
        draftdaemon.mkdir()
        (draftdaemon / ".git").mkdir()
        (draftdaemon / "README.md").write_text(
            "A place where I want to work on Blockdaemon drafts", encoding="utf-8"
        )
        task = (
            "I want to work on the secondBrain, which was a project I worked on "
            "for Blockdaemon."
        )

        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.launch_project", return_value=0) as launch,
        ):
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), task, "--agent", "codex"]
                ),
                0,
            )

        self.assertEqual(launch.call_args.args[0]["path"], str(second_brain.resolve()))
        self.assertIn("matched: brain, second, blockdaemon", output.getvalue())

    def test_cli_never_launches_weak_ambiguous_stale_or_unknown_tasks(self) -> None:
        weak = self.root / "weak"
        ambiguous = self.root / "ambiguous"
        weak.mkdir()
        ambiguous.mkdir()
        (weak / "README.md").write_text("raritytoken", encoding="utf-8")
        (self.payments / "README.md").write_text(
            "sharedphrase crossterm", encoding="utf-8"
        )
        (self.payments_worker / "README.md").write_text(
            "sharedphrase crossterm", encoding="utf-8"
        )
        config = load_config(self.config_path)
        config["aliases"]["stale task"] = "/missing"
        save_config(config, self.config_path)

        errors = io.StringIO()
        with (
            redirect_stderr(errors),
            patch("terminal_agent_launcher.cli.launch_project") as launch,
        ):
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "find raritytoken", "--agent", "codex"]
                ),
                2,
            )
            self.assertEqual(
                cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "sharedphrase crossterm",
                        "--agent",
                        "codex",
                    ]
                ),
                2,
            )
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "stale task", "--agent", "codex"]
                ),
                2,
            )
            self.assertEqual(
                cli.main(
                    ["--config", str(self.config_path), "unknown task", "--agent", "codex"]
                ),
                2,
            )

        launch.assert_not_called()
        self.assertIn("Candidates: weak", errors.getvalue())
        self.assertIn("Candidates: Payments API", errors.getvalue())
        self.assertIn("Alias 'stale task' is stale", errors.getvalue())
        self.assertIn("Candidates: none", errors.getvalue())

    def test_help_describes_local_zero_token_task_routing(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                cli.main(["--help"])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("locally without model tokens", output.getvalue())
        self.assertIn("tal route TASK", output.getvalue())
        self.assertIn("tal teach PROJECT --last|--receipt ID|--task TASK", output.getvalue())
        self.assertIn("exact normalized task matches only", output.getvalue())
        self.assertIn("terminal stale correction error", output.getvalue())

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
