from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validation import (  # noqa: E402
    Candidate,
    CandidateInputError,
    CommandResult,
    DockerBackend,
    PhaseResult,
    classify_validation,
    read_candidate,
    stage_candidate,
)


def command(exit_code: int = 0, *, timed_out: bool = False) -> CommandResult:
    return CommandResult(
        command="cargo test",
        exit_code=None if timed_out else exit_code,
        timed_out=timed_out,
        duration_seconds=0.1,
        stdout_tail="",
        stderr_tail="",
    )


def phase(name: str, repeat: int, passes: bool) -> PhaseResult:
    return PhaseResult(
        phase=name,
        repeat=repeat,
        patch_applied=True,
        patch_error=None,
        commands=(command(0 if passes else 1),),
    )


def candidate(source_dir: Path) -> Candidate:
    return Candidate(
        task_id="ruff__ruff-example",
        base_commit="a" * 40,
        subsystem="linter",
        test_commands=("cargo test -p ruff_linter",),
        test_timeout_seconds=900,
        source_dir=source_dir,
        task_json=source_dir / "task.json",
        tests_patch=source_dir / "tests.patch",
        gold_patch=source_dir / "gold.patch",
    )


class ValidationClassificationTests(unittest.TestCase):
    def test_expected_matrix_validates(self) -> None:
        item = candidate(Path("/tmp/source"))
        results = []
        for repeat in range(1, 4):
            results.extend(
                [
                    phase("base", repeat, True),
                    phase("tests_only", repeat, False),
                    phase("gold", repeat, True),
                ]
            )
        classified = classify_validation(item, {"image": "test"}, results, {})
        self.assertEqual(classified["status"], "validated")
        self.assertEqual(classified["reasons"], [])

    def test_tests_that_pass_before_gold_are_rejected(self) -> None:
        item = candidate(Path("/tmp/source"))
        results = [
            phase("base", 1, True),
            phase("tests_only", 1, True),
            phase("gold", 1, True),
        ]
        classified = classify_validation(item, {}, results, {})
        self.assertEqual(classified["status"], "rejected")
        self.assertIn(
            "hidden_tests_do_not_consistently_fail",
            classified["reasons"],
        )

    def test_gold_failure_and_timeout_are_rejected(self) -> None:
        item = candidate(Path("/tmp/source"))
        results = [
            phase("base", 1, True),
            phase("tests_only", 1, False),
            PhaseResult(
                phase="gold",
                repeat=1,
                patch_applied=True,
                patch_error=None,
                commands=(command(timed_out=True),),
            ),
        ]
        classified = classify_validation(item, {}, results, {})
        self.assertIn("gold_does_not_consistently_pass", classified["reasons"])
        self.assertIn("gold_timeout", classified["reasons"])

    def test_infrastructure_errors_are_not_candidate_rejections(self) -> None:
        item = candidate(Path("/tmp/source"))
        results = [
            PhaseResult(
                phase=name,
                repeat=1,
                patch_applied=False,
                patch_error=None,
                commands=(),
                infrastructure_error="Docker unavailable",
            )
            for name in ("base", "tests_only", "gold")
        ]
        classified = classify_validation(item, {}, results, {})
        self.assertEqual(classified["status"], "infrastructure_error")


class CandidateArtifactTests(unittest.TestCase):
    def write_candidate(self, root: Path, *, task_id: str = "ruff__ruff-example") -> Path:
        task_dir = root / "ruff__ruff-example"
        task_dir.mkdir()
        (task_dir / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "base_commit": "a" * 40,
                    "subsystem": "linter",
                    "test_commands": ["cargo test -p ruff_linter"],
                    "timeouts": {"test_seconds": 900},
                }
            ),
            encoding="utf-8",
        )
        (task_dir / "tests.patch").write_text("test patch\n", encoding="utf-8")
        (task_dir / "gold.patch").write_text("gold patch\n", encoding="utf-8")
        return task_dir

    def test_candidate_is_read_and_staged_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir = self.write_candidate(root)
            original = read_candidate(task_dir)
            staged, hashes = stage_candidate(original, root / "staging")
            self.assertNotEqual(staged.source_dir, original.source_dir)
            self.assertEqual(staged.task_id, original.task_id)
            self.assertEqual(
                set(hashes),
                {"task.json", "tests.patch", "gold.patch"},
            )

    def test_mismatched_directory_and_task_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_dir = self.write_candidate(Path(temporary), task_id="wrong")
            with self.assertRaisesRegex(CandidateInputError, "does not match"):
                read_candidate(task_dir)


class DockerCacheTests(unittest.TestCase):
    def test_phase_mounts_shared_target_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = candidate(root)
            backend = DockerBackend(
                dockerfile=root / "Dockerfile",
                context=root,
                image_prefix="test",
                build_timeout_seconds=1,
                container_start_timeout_seconds=1,
                command_timeout_seconds=1,
                cpus=1,
                memory="1g",
                precompile=False,
                rebuild_images=False,
                keep_images=True,
                output_tail_characters=100,
                reporter=lambda _message: None,
            )
            backend._target_volumes["test:image"] = "target-volume"
            invocations: list[list[str]] = []

            def fake_run(command_line: list[str], *, timeout: int) -> CommandResult:
                del timeout
                invocations.append(command_line)
                return CommandResult(
                    command=" ".join(command_line),
                    exit_code=0,
                    timed_out=False,
                    duration_seconds=0,
                    stdout_tail="",
                    stderr_tail="",
                )

            backend._run = fake_run  # type: ignore[method-assign]
            result = backend.run_phase(item, root, "test:image", "base", 1)
            self.assertTrue(result.passed)
            docker_run = invocations[0]
            self.assertIn(
                "type=volume,src=target-volume,dst=/testbed/target",
                docker_run,
            )


if __name__ == "__main__":
    unittest.main()
