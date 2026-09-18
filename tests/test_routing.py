from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_agent_launcher.routing import MAX_METADATA_BYTES, build_profiles, route_task


class RoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "Projects"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def project(self, relative_path: str) -> dict[str, str]:
        path = self.root / relative_path
        path.mkdir(parents=True)
        return {"name": path.name, "path": str(path.resolve())}

    def test_routing_returns_structured_confident_name_evidence(self) -> None:
        billing = self.project("billing-api")
        other = self.project("notes")

        evidence = route_task("fix billing api", [other, billing], [self.root])

        self.assertIs(evidence.project, billing)
        self.assertGreater(evidence.score, 3.0)
        self.assertGreater(evidence.confidence_margin, 1.25)
        self.assertIn("billing", evidence.strongest_terms)

    def test_name_terms_outrank_relative_path_terms(self) -> None:
        name_match = self.project("billing-app")
        path_match = self.project("department/billing/worker")
        unrelated = self.project("weather")

        evidence = route_task(
            "billing", [path_match, name_match, unrelated], [self.root]
        )

        self.assertIs(evidence.project, name_match)
        self.assertGreater(evidence.candidates[0].score, evidence.candidates[1].score)

    def test_metadata_can_route_when_name_and_path_do_not_match(self) -> None:
        configuration = self.project("agent-config")
        (Path(configuration["path"]) / "README.md").write_text(
            "Global Codex Claude instructions live here.", encoding="utf-8"
        )
        other = self.project("weather-service")

        evidence = route_task("global codex instructions", [other, configuration], [self.root])

        self.assertIs(evidence.project, configuration)
        self.assertEqual(evidence.strongest_terms, ("codex", "global", "instructions"))

    def test_fenced_task_example_cannot_tie_or_outrank_its_actual_repository(self) -> None:
        configuration = self.project("agent-config")
        (Path(configuration["path"]) / "README.md").write_text(
            "Global Codex Claude instructions live here.", encoding="utf-8"
        )
        documentation = self.project("routing-docs")
        (Path(documentation["path"]) / "README.md").write_text(
            """How to preview a route:

```sh
tal route "update my global Codex and Claude instructions"
```
""",
            encoding="utf-8",
        )

        evidence = route_task(
            "update my global Codex and Claude instructions",
            [documentation, configuration],
            [self.root],
        )

        self.assertIs(evidence.project, configuration)
        self.assertEqual([candidate.project for candidate in evidence.candidates], [configuration])

    def test_common_work_words_are_rejected(self) -> None:
        evidence = route_task(
            "update change my project", [self.project("one"), self.project("two")], [self.root]
        )

        self.assertIsNone(evidence.project)
        self.assertEqual(evidence.candidates, ())

    def test_equal_scores_are_ambiguous_and_candidates_are_deterministic(self) -> None:
        alpha = self.project("alpha")
        beta = self.project("beta")
        (Path(alpha["path"]) / "README.md").write_text(
            "alphaworkflow", encoding="utf-8"
        )
        (Path(beta["path"]) / "README.md").write_text(
            "betaworkflow", encoding="utf-8"
        )

        first = route_task("alphaworkflow betaworkflow", [beta, alpha], [self.root])
        second = route_task("alphaworkflow betaworkflow", [alpha, beta], [self.root])

        self.assertIsNone(first.project)
        self.assertEqual([candidate.project["name"] for candidate in first.candidates], ["alpha", "beta"])
        self.assertEqual(first.candidates, second.candidates)

    def test_unreadable_metadata_is_ignored(self) -> None:
        project = self.project("private")
        readme = Path(project["path"]) / "README.md"
        readme.write_text("private workflow", encoding="utf-8")

        with patch("pathlib.Path.open", side_effect=OSError("denied")):
            profiles = build_profiles([project], [self.root])

        self.assertEqual(profiles[0].metadata_terms, frozenset())

    def test_metadata_read_is_capped(self) -> None:
        project = self.project("bounded")
        (Path(project["path"]) / "README.md").write_bytes(
            b"visibleterm " + (b"x" * MAX_METADATA_BYTES) + b" hiddenterm"
        )

        profile = build_profiles([project], [self.root])[0]

        self.assertIn("visibleterm", profile.metadata_terms)
        self.assertNotIn("hiddenterm", profile.metadata_terms)

    def test_metadata_symlink_escaping_project_root_is_ignored(self) -> None:
        project = self.project("safe-project")
        outside = self.root.parent / "outside-readme"
        outside.write_text("secretrouterterm", encoding="utf-8")
        (Path(project["path"]) / "README.md").symlink_to(outside)

        profile = build_profiles([project], [self.root])[0]

        self.assertNotIn("secretrouterterm", profile.metadata_terms)


if __name__ == "__main__":
    unittest.main()
