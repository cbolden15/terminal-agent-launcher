"""Fixed subprocess evaluator for routing research candidates."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a safe relative path: {value!r}")
    return path


def _write_project(case_root: Path, project: Mapping[str, Any]) -> dict[str, str]:
    relative = _safe_relative_path(str(project["path"]), "project path")
    project_root = case_root.joinpath(*relative.parts)
    project_root.mkdir(parents=True, exist_ok=True)

    metadata = project.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("project metadata must be an object")
    for name, content in metadata.items():
        metadata_path = _safe_relative_path(str(name), "metadata path")
        target = project_root.joinpath(*metadata_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")

    return {"name": project_root.name, "path": str(project_root.resolve())}


def _load_router(router_path: Path) -> Any:
    module_name = "_terminal_agent_launcher_candidate_routing"
    spec = importlib.util.spec_from_file_location(module_name, router_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load candidate router from {router_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    if not callable(getattr(module, "route_task", None)):
        raise RuntimeError("Candidate router does not define callable route_task")
    return module


def evaluate(router_path: Path, cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    router = _load_router(router_path)
    outcomes: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="tal-routing-eval-") as temporary:
        evaluation_root = Path(temporary)
        for index, case in enumerate(cases):
            case_id = str(case["id"])
            group = str(case["group"])
            task = str(case["task"])
            case_root = evaluation_root / f"case-{index:04d}"
            case_root.mkdir()
            canonical_case_root = case_root.resolve()
            projects = [_write_project(case_root, project) for project in case["projects"]]

            expected_relative = case.get("expected")
            expected_path = None
            if expected_relative is not None:
                relative = _safe_relative_path(str(expected_relative), "expected path")
                expected_path = str(canonical_case_root.joinpath(*relative.parts))

            try:
                evidence = router.route_task(task, projects, [case_root])
                selected = getattr(evidence, "project", None)
                actual_path = selected.get("path") if isinstance(selected, dict) else None
                if expected_path is None and actual_path is None:
                    status = "correct_abstain"
                elif expected_path is not None and actual_path == expected_path:
                    status = "correct_route"
                elif actual_path is None:
                    status = "missed_route"
                else:
                    status = "wrong_confident"
                outcomes.append(
                    {
                        "id": case_id,
                        "group": group,
                        "status": status,
                        "expected": expected_relative,
                        "selected": (
                            str(Path(actual_path).relative_to(canonical_case_root))
                            if actual_path is not None
                            else None
                        ),
                        "score": float(getattr(evidence, "score", 0.0)),
                        "margin": float(getattr(evidence, "confidence_margin", 0.0)),
                    }
                )
            except Exception as exc:  # Candidate failures are evaluator data.
                outcomes.append(
                    {
                        "id": case_id,
                        "group": group,
                        "status": "error",
                        "expected": expected_relative,
                        "selected": None,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )

    return {"outcomes": outcomes}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate one routing.py candidate.")
    parser.add_argument("--router", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = evaluate(args.router.resolve(strict=True), request["cases"])
        args.output.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        return 0
    except Exception as exc:
        args.output.write_text(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"[:1000]}),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
