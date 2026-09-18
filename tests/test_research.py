from __future__ import annotations

import difflib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from terminal_agent_launcher import cli
from terminal_agent_launcher.research import (
    EvaluationSnapshot,
    GroupMetrics,
    PatchProposal,
    ProviderResult,
    CodexProvider,
    ResearchError,
    ResearchRunResult,
    apply_proposal,
    build_prompt,
    evaluate_candidate,
    load_cases,
    run_research,
    should_promote,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeImprovingProvider:
    name = "codex"

    def propose(self, worktree, prompt, output_directory, experiment, timeout_seconds):
        self.prompt = prompt
        router = worktree / "terminal_agent_launcher" / "routing.py"
        before = router.read_text(encoding="utf-8")
        after = before.replace("BROKEN = True", "BROKEN = False")
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="a/terminal_agent_launcher/routing.py",
                tofile="b/terminal_agent_launcher/routing.py",
            )
        )
        return ProviderResult(PatchProposal("enable exact task routing", diff), "proposed")


class ResearchTestCase(unittest.TestCase):
    def test_public_corpus_has_no_confident_wrong_routes_or_errors(self) -> None:
        cases = load_cases(ROOT)
        snapshot = evaluate_candidate(ROOT / "terminal_agent_launcher" / "routing.py", cases)

        self.assertEqual(snapshot.total.wrong_confident, 0)
        self.assertEqual(snapshot.total.errors, 0)
        self.assertGreater(snapshot.total.correct_routes, 0)

    def test_confident_wrong_route_is_never_promoted(self) -> None:
        current = self.snapshot(correct_routes=1, missed_routes=1)
        candidate = self.snapshot(correct_routes=2, wrong_confident=1)

        promoted, reason = should_promote(current, candidate)

        self.assertFalse(promoted)
        self.assertIn("confident wrong", reason)

    def test_holdout_regression_blocks_a_training_improvement(self) -> None:
        current = self.snapshot(train_routes=1, holdout_routes=1)
        candidate = self.snapshot(train_routes=2, holdout_routes=0, missed_routes=1)

        promoted, reason = should_promote(current, candidate)

        self.assertFalse(promoted)
        self.assertIn("holdout", reason)

    def test_patch_allowlist_rejects_any_other_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary)
            patch_path = worktree / "proposal.patch"
            proposal = PatchProposal(
                "edit docs",
                "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n",
            )

            with self.assertRaisesRegex(ResearchError, "disallowed paths"):
                apply_proposal(worktree, proposal, patch_path)

    def test_private_task_text_is_not_put_in_the_provider_prompt(self) -> None:
        cases = [
            {
                "id": "train-visible",
                "group": "train",
                "task": "visible routing failure",
                "expected": "visible",
                "projects": [{"path": "visible"}],
            },
            {
                "id": "private-secret",
                "group": "private",
                "task": "secret customer acquisition task",
                "expected": "secret",
                "projects": [{"path": "secret"}],
            },
        ]
        snapshot = EvaluationSnapshot(
            {
                "train": GroupMetrics(total=1, missed_routes=1),
                "holdout": GroupMetrics(),
                "private": GroupMetrics(total=1, missed_routes=1),
            },
            (
                {"id": "train-visible", "group": "train", "status": "missed_route"},
                {"id": "private-secret", "group": "private", "status": "missed_route"},
            ),
            100,
            0.01,
        )

        prompt = build_prompt(cases, snapshot, 1, ())

        self.assertIn("visible routing failure", prompt)
        self.assertNotIn("secret customer acquisition task", prompt)
        self.assertNotIn("private-secret", prompt)

    def test_codex_provider_uses_read_only_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            output = root / "output"
            (worktree / "research").mkdir(parents=True)
            output.mkdir()
            schema = worktree / "research" / "proposal.schema.json"
            schema.write_text("{}", encoding="utf-8")
            provider = CodexProvider(executable=sys.executable)

            def fake_run(command, **_kwargs):
                response = Path(command[command.index("--output-last-message") + 1])
                response.write_text(
                    json.dumps({"description": "one change", "patch": "patch text"}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0)

            with patch("terminal_agent_launcher.research.subprocess.run", side_effect=fake_run) as run:
                result = provider.propose(worktree, "prompt", output, 1, 30)

            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertEqual(command[command.index("--output-schema") + 1], str(schema))
            self.assertIn("--ephemeral", command)
            self.assertEqual(result.status, "proposed")
            self.assertEqual(result.proposal.description, "one change")

    def test_bounded_run_keeps_an_improvement_on_a_candidate_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            output = root / "state"
            self.make_research_repository(repository)
            provider = FakeImprovingProvider()

            result = run_research(
                repository=repository,
                provider_name="codex",
                max_experiments=1,
                max_minutes=1,
                output_root=output,
                provider=provider,
                progress=lambda _message: None,
            )

            self.assertEqual(result.experiments_run, 1)
            self.assertEqual(result.kept_experiments, 1)
            self.assertTrue(result.report_path.is_file())
            self.assertTrue(result.results_path.is_file())
            self.assertIn("Visible training failures", provider.prompt)
            branch_source = self.git(
                repository,
                "show",
                f"{result.branch}:terminal_agent_launcher/routing.py",
            ).stdout
            self.assertIn("BROKEN = False", branch_source)
            self.assertEqual(
                self.git(repository, "worktree", "list", "--porcelain").stdout.count("worktree "),
                1,
            )

    def test_cli_research_does_not_load_launcher_configuration(self) -> None:
        baseline = self.snapshot(correct_routes=1)
        result = ResearchRunResult(
            branch="autoresearch/routing-test",
            report_path=Path("/tmp/routing-report.md"),
            results_path=Path("/tmp/routing-results.jsonl"),
            experiments_run=1,
            kept_experiments=1,
            baseline=baseline,
            best=baseline,
        )
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("terminal_agent_launcher.cli.run_research", return_value=result) as research,
            patch("terminal_agent_launcher.cli.load_config") as load_config,
        ):
            status = cli.main(
                [
                    "research",
                    "--repo",
                    "/tmp/source",
                    "--max-experiments",
                    "3",
                    "--max-minutes",
                    "5",
                ]
            )

        self.assertEqual(status, 0)
        load_config.assert_not_called()
        research.assert_called_once()
        self.assertEqual(research.call_args.kwargs["max_experiments"], 3)
        self.assertIn("Candidate branch: autoresearch/routing-test", output.getvalue())

    @staticmethod
    def snapshot(
        correct_routes=0,
        missed_routes=0,
        wrong_confident=0,
        train_routes=None,
        holdout_routes=0,
    ):
        train = correct_routes if train_routes is None else train_routes
        groups = {
            "train": GroupMetrics(
                total=train + missed_routes + wrong_confident,
                correct_routes=train,
                missed_routes=missed_routes,
                wrong_confident=wrong_confident,
            ),
            "holdout": GroupMetrics(total=holdout_routes, correct_routes=holdout_routes),
            "private": GroupMetrics(),
        }
        return EvaluationSnapshot(groups, (), 100, 0.01)

    def make_research_repository(self, repository: Path) -> None:
        (repository / "terminal_agent_launcher").mkdir(parents=True)
        (repository / "research").mkdir()
        (repository / "terminal_agent_launcher" / "routing.py").write_text(
            """BROKEN = True

class Evidence:
    def __init__(self, project):
        self.project = project
        self.score = 10.0 if project else 0.0
        self.confidence_margin = self.score
        self.strongest_terms = (\"alpha\",) if project else ()

def route_task(task, projects, roots=()):
    selected = None
    if not BROKEN:
        selected = next((project for project in projects if project[\"name\"] == \"alpha\"), None)
    return Evidence(selected)
""",
            encoding="utf-8",
        )
        case = {
            "id": "train-alpha",
            "split": "train",
            "task": "work on alpha",
            "expected": "alpha",
            "projects": [{"path": "alpha", "metadata": {"README.md": "Alpha."}}],
        }
        (repository / "research" / "routing_cases.public.jsonl").write_text(
            json.dumps(case) + "\n", encoding="utf-8"
        )
        (repository / "research" / "proposal.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "patch": {"type": "string"},
                    },
                    "required": ["description", "patch"],
                }
            ),
            encoding="utf-8",
        )
        (repository / ".codex-test-command").write_text(
            "python3 -c pass\n", encoding="utf-8"
        )
        self.git(repository, "init")
        self.git(repository, "add", ".")
        self.git(
            repository,
            "-c",
            "user.name=Research Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "baseline",
        )

    @staticmethod
    def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            text=True,
            capture_output=True,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
