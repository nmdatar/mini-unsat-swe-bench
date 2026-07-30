"""Dynamic validation primitives for Ruff benchmark candidates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from _common import write_json


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PHASES = ("base", "tests_only", "gold")


class CandidateInputError(ValueError):
    """The generated candidate artifacts are malformed."""


class ValidationInfrastructureError(RuntimeError):
    """The host validation infrastructure is unavailable."""


class CandidatePreparationError(RuntimeError):
    """The candidate's pinned environment could not be prepared."""


@dataclass(frozen=True)
class Candidate:
    task_id: str
    base_commit: str
    subsystem: str
    test_commands: tuple[str, ...]
    test_timeout_seconds: int
    source_dir: Path
    task_json: Path
    tests_patch: Path
    gold_patch: Path


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True)
class PhaseResult:
    phase: str
    repeat: int
    patch_applied: bool
    patch_error: str | None
    commands: tuple[CommandResult, ...]
    infrastructure_error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.infrastructure_error is None
            and self.patch_applied
            and bool(self.commands)
            and all(command.passed for command in self.commands)
        )

    @property
    def any_command_failed(self) -> bool:
        return any(not command.passed for command in self.commands)

    @property
    def any_timeout(self) -> bool:
        return any(command.timed_out for command in self.commands)


class ValidationBackend(Protocol):
    def prepare(self, candidate: Candidate, staged_dir: Path) -> dict[str, Any]:
        """Prepare an immutable environment and return its metadata."""

    def run_phase(
        self,
        candidate: Candidate,
        staged_dir: Path,
        image: str,
        phase: str,
        repeat: int,
    ) -> PhaseResult:
        """Run one clean validation phase."""

    def cleanup(self, image: str) -> None:
        """Remove disposable resources when configured."""


def read_candidate(task_dir: Path) -> Candidate:
    """Read and validate one candidate directory without modifying it."""

    task_json = task_dir / "task.json"
    tests_patch = task_dir / "tests.patch"
    gold_patch = task_dir / "gold.patch"
    try:
        task = json.loads(task_json.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CandidateInputError(f"missing task.json in {task_dir}") from exc
    except json.JSONDecodeError as exc:
        raise CandidateInputError(f"invalid JSON in {task_json}: {exc}") from exc
    if not isinstance(task, dict):
        raise CandidateInputError(f"{task_json} must contain an object")

    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise CandidateInputError(f"{task_json}: task_id must be a non-empty string")
    if task_id != task_dir.name:
        raise CandidateInputError(
            f"{task_json}: task_id {task_id!r} does not match directory {task_dir.name!r}"
        )

    base_commit = task.get("base_commit")
    if not isinstance(base_commit, str) or not FULL_SHA_RE.fullmatch(base_commit):
        raise CandidateInputError(
            f"{task_json}: base_commit must be a full lowercase commit SHA"
        )

    subsystem = task.get("subsystem")
    if not isinstance(subsystem, str) or not subsystem:
        raise CandidateInputError(f"{task_json}: subsystem must be a non-empty string")

    commands = task.get("test_commands")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(command, str) and command.strip() for command in commands)
    ):
        raise CandidateInputError(
            f"{task_json}: test_commands must be a non-empty list of commands"
        )
    timeouts = task.get("timeouts")
    if not isinstance(timeouts, dict):
        raise CandidateInputError(f"{task_json}: timeouts must be an object")
    test_timeout = timeouts.get("test_seconds")
    if isinstance(test_timeout, bool) or not isinstance(test_timeout, int) or test_timeout <= 0:
        raise CandidateInputError(
            f"{task_json}: timeouts.test_seconds must be a positive integer"
        )

    for patch in (tests_patch, gold_patch):
        try:
            if not patch.read_bytes().strip():
                raise CandidateInputError(f"patch is empty: {patch}")
        except FileNotFoundError as exc:
            raise CandidateInputError(f"missing patch: {patch}") from exc

    return Candidate(
        task_id=task_id,
        base_commit=base_commit,
        subsystem=subsystem,
        test_commands=tuple(commands),
        test_timeout_seconds=test_timeout,
        source_dir=task_dir,
        task_json=task_json,
        tests_patch=tests_patch,
        gold_patch=gold_patch,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_candidate(candidate: Candidate, staging_root: Path) -> tuple[Candidate, dict[str, str]]:
    """Copy a stable candidate snapshot outside ``tasks/`` for validation."""

    staged_dir = staging_root / candidate.task_id
    staged_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "task.json": candidate.task_json,
        "tests.patch": candidate.tests_patch,
        "gold.patch": candidate.gold_patch,
    }
    hashes: dict[str, str] = {}
    for name, source in artifacts.items():
        destination = staged_dir / name
        shutil.copyfile(source, destination)

    # Dynamic triage may replace a broad package test with an equivalent,
    # focused command. Persist the effective command only in the evaluator's
    # staging copy; the generated source artifact remains immutable.
    staged_task = json.loads((staged_dir / "task.json").read_text(encoding="utf-8"))
    staged_task["test_commands"] = list(candidate.test_commands)
    (staged_dir / "task.json").write_text(
        json.dumps(staged_task, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for name in artifacts:
        hashes[name] = sha256_file(staged_dir / name)

    staged = read_candidate(staged_dir)
    return staged, hashes


def classify_validation(
    candidate: Candidate,
    environment: dict[str, Any],
    phase_results: list[PhaseResult],
    artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    """Classify repeated base/tests-only/gold outcomes."""

    reasons: list[str] = []
    infrastructure_errors = sorted(
        {
            result.infrastructure_error
            for result in phase_results
            if result.infrastructure_error
        }
    )
    by_phase = {
        phase: [result for result in phase_results if result.phase == phase]
        for phase in PHASES
    }

    expected_repeats = len(by_phase["base"])
    if expected_repeats == 0 or any(
        len(by_phase[phase]) != expected_repeats for phase in PHASES
    ):
        reasons.append("incomplete_phase_matrix")

    for result in phase_results:
        if result.any_timeout:
            reasons.append(f"{result.phase}_timeout")
        if not result.patch_applied:
            reasons.append(f"{result.phase}_patch_apply_failure")

    base_outcomes = [result.passed for result in by_phase["base"]]
    tests_outcomes = [result.passed for result in by_phase["tests_only"]]
    gold_outcomes = [result.passed for result in by_phase["gold"]]

    if base_outcomes and not all(base_outcomes):
        reasons.append("base_regression_failure")
    if tests_outcomes and any(tests_outcomes):
        reasons.append("hidden_tests_do_not_consistently_fail")
    if gold_outcomes and not all(gold_outcomes):
        reasons.append("gold_does_not_consistently_pass")

    for phase, outcomes in (
        ("base", base_outcomes),
        ("tests_only", tests_outcomes),
        ("gold", gold_outcomes),
    ):
        if outcomes and len(set(outcomes)) > 1:
            reasons.append(f"{phase}_nondeterministic")

    if infrastructure_errors:
        status = "infrastructure_error"
    elif reasons:
        status = "rejected"
    else:
        status = "validated"

    return {
        "task_id": candidate.task_id,
        "base_commit": candidate.base_commit,
        "subsystem": candidate.subsystem,
        "status": status,
        "reasons": sorted(set(reasons)),
        "artifact_hashes": artifact_hashes,
        "environment": environment,
        "phase_results": [
            {
                **asdict(result),
                "passed": result.passed,
                "any_command_failed": result.any_command_failed,
                "any_timeout": result.any_timeout,
            }
            for result in phase_results
        ],
        "infrastructure_errors": infrastructure_errors,
    }


def validate_candidate(
    candidate: Candidate,
    staged_dir: Path,
    backend: ValidationBackend,
    repeats: int,
    artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    """Prepare and dynamically validate one staged candidate."""

    if repeats <= 0:
        raise ValueError("repeats must be positive")
    environment = backend.prepare(candidate, staged_dir)
    image = environment["image"]
    results: list[PhaseResult] = []
    try:
        for repeat in range(1, repeats + 1):
            for phase in PHASES:
                results.append(
                    backend.run_phase(
                        candidate,
                        staged_dir,
                        image,
                        phase,
                        repeat,
                    )
                )
    finally:
        backend.cleanup(image)
    return classify_validation(candidate, environment, results, artifact_hashes)


def _tail(value: str, characters: int) -> str:
    if len(value) <= characters:
        return value
    return value[-characters:]


class DockerBackend:
    """Validate candidates in clean, network-disabled Docker containers."""

    def __init__(
        self,
        *,
        dockerfile: Path,
        context: Path,
        image_prefix: str,
        build_timeout_seconds: int,
        container_start_timeout_seconds: int,
        command_timeout_seconds: int,
        cpus: int | float,
        memory: str,
        precompile: bool,
        rebuild_images: bool,
        keep_images: bool,
        output_tail_characters: int,
        reporter: Callable[[str], None] = print,
    ) -> None:
        self.dockerfile = dockerfile.resolve()
        self.context = context.resolve()
        self.image_prefix = image_prefix
        self.build_timeout_seconds = build_timeout_seconds
        self.container_start_timeout_seconds = container_start_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.cpus = cpus
        self.memory = memory
        self.precompile = precompile
        self.rebuild_images = rebuild_images
        self.keep_images = keep_images
        self.output_tail_characters = output_tail_characters
        self.reporter = reporter
        self._target_volumes: dict[str, str] = {}

    @staticmethod
    def _cache_lane() -> tuple[str, str]:
        """Return a stable cache lane for the current executor worker.

        The validation wrapper starts one validator process at a time, and
        ThreadPoolExecutor uses stable ``..._0``, ``..._1`` worker suffixes.
        Reusing those named volumes across batches avoids recompiling every
        third-party dependency for every nearby Ruff commit.
        """

        thread_name = threading.current_thread().name
        match = re.search(r"_(\d+)$", thread_name)
        lane = match.group(1) if match else "0"
        return lane, f"mini-unsat-target-lane-{lane}"

    def ensure_available(self) -> str:
        result = self._run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            timeout=30,
        )
        if not result.passed:
            detail = result.stderr_tail or result.stdout_tail or "unknown Docker error"
            raise ValidationInfrastructureError(f"Docker is unavailable: {detail}")
        return result.stdout_tail.strip()

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
    ) -> CommandResult:
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
            return CommandResult(
                command=" ".join(command),
                exit_code=None,
                timed_out=True,
                duration_seconds=time.monotonic() - started,
                stdout_tail=_tail(stdout, self.output_tail_characters),
                stderr_tail=_tail(stderr, self.output_tail_characters),
            )
        return CommandResult(
            command=" ".join(command),
            exit_code=result.returncode,
            timed_out=False,
            duration_seconds=time.monotonic() - started,
            stdout_tail=_tail(result.stdout, self.output_tail_characters),
            stderr_tail=_tail(result.stderr, self.output_tail_characters),
        )

    def _image_tag(self, candidate: Candidate) -> str:
        suffix = candidate.task_id.lower().replace("_", "-")
        return f"{self.image_prefix}:{suffix}"

    def _inspect_label(self, image: str, label: str) -> str | None:
        result = self._run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{label}" }}}}',
                image,
            ],
            timeout=30,
        )
        return result.stdout_tail.strip() if result.passed else None

    def prepare(self, candidate: Candidate, staged_dir: Path) -> dict[str, Any]:
        del staged_dir
        image = self._image_tag(candidate)
        existing_commit = self._inspect_label(image, "benchmark.base_commit")
        existing_precompile = self._inspect_label(image, "benchmark.precompile")
        expected_precompile = "true" if self.precompile else "false"
        if (
            self.rebuild_images
            or existing_commit != candidate.base_commit
            or existing_precompile != expected_precompile
        ):
            self.reporter(f"[{candidate.task_id}] building {image}")
            build = self._run(
                [
                    "docker",
                    "build",
                    "--file",
                    str(self.dockerfile),
                    "--build-arg",
                    f"RUFF_COMMIT={candidate.base_commit}",
                    "--build-arg",
                    f"PRECOMPILE={'true' if self.precompile else 'false'}",
                    "--label",
                    f"benchmark.task_id={candidate.task_id}",
                    "--label",
                    f"benchmark.base_commit={candidate.base_commit}",
                    "--label",
                    f"benchmark.precompile={expected_precompile}",
                    "--tag",
                    image,
                    str(self.context),
                ],
                timeout=self.build_timeout_seconds,
            )
            if not build.passed:
                detail = build.stderr_tail or build.stdout_tail
                if build.timed_out:
                    raise CandidatePreparationError(
                        f"image build timed out after {self.build_timeout_seconds}s"
                    )
                raise CandidatePreparationError(f"image build failed: {detail}")
        else:
            self.reporter(f"[{candidate.task_id}] reusing {image}")

        smoke = self._run(
            ["docker", "run", "--rm", "--network", "none", image, "smoke-test"],
            timeout=self.container_start_timeout_seconds,
        )
        if not smoke.passed:
            raise CandidatePreparationError(
                f"environment smoke test failed: {smoke.stderr_tail or smoke.stdout_tail}"
            )
        image_id = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            timeout=30,
        )
        if not image_id.passed:
            raise ValidationInfrastructureError(
                f"could not inspect image {image}: {image_id.stderr_tail}"
            )
        lane, volume = self._cache_lane()
        created = self._run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"benchmark.cache_lane={lane}",
                volume,
            ],
            timeout=30,
        )
        if not created.passed:
            raise CandidatePreparationError(
                "could not create the per-candidate Cargo target volume: "
                f"{created.stderr_tail or created.stdout_tail}"
            )
        self._target_volumes[image] = volume
        return {
            "backend": "docker",
            "image": image,
            "image_id": image_id.stdout_tail.strip(),
            "base_commit": candidate.base_commit,
            "precompile": self.precompile,
            "cargo_target_volume": volume,
            "cargo_cache_strategy": "persistent_worker_lane",
            "cargo_cache_lane": lane,
        }

    @staticmethod
    def _looks_like_docker_error(result: CommandResult) -> bool:
        combined = f"{result.stdout_tail}\n{result.stderr_tail}".lower()
        return any(
            marker in combined
            for marker in (
                "error response from daemon",
                "cannot connect to the docker daemon",
                "no such container",
                "docker daemon is not running",
            )
        )

    def run_phase(
        self,
        candidate: Candidate,
        staged_dir: Path,
        image: str,
        phase: str,
        repeat: int,
    ) -> PhaseResult:
        if phase not in PHASES:
            raise ValueError(f"unknown phase: {phase}")
        container = (
            f"mini-unsat-validate-{candidate.task_id.lower().replace('_', '-')}-"
            f"{phase}-{repeat}-{uuid.uuid4().hex[:8]}"
        )
        start = self._run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--cpus",
                str(self.cpus),
                "--memory",
                self.memory,
                "--mount",
                (
                    "type=volume,"
                    f"src={self._target_volumes[image]},"
                    "dst=/testbed/target"
                ),
                "--mount",
                f"type=bind,src={staged_dir.resolve()},dst=/benchmark,readonly",
                image,
                "sleep",
                "infinity",
            ],
            timeout=self.container_start_timeout_seconds,
        )
        if not start.passed:
            return PhaseResult(
                phase=phase,
                repeat=repeat,
                patch_applied=False,
                patch_error=None,
                commands=(),
                infrastructure_error=start.stderr_tail or start.stdout_tail,
            )

        patch_applied = True
        patch_error: str | None = None
        commands: list[CommandResult] = []
        infrastructure_error: str | None = None
        try:
            patch_script = {
                # A lane can contain artifacts from a nearby commit whose
                # source mtimes differ. Touching workspace inputs forces Cargo
                # to re-check/rebuild Ruff crates while retaining compiled
                # third-party dependencies. This preserves correctness and is
                # substantially faster than an empty target directory.
                "base": (
                    "find crates -type f -name '*.rs' -exec touch {} + "
                    "&& touch Cargo.toml Cargo.lock"
                ),
                "tests_only": (
                    "git apply --check /benchmark/tests.patch "
                    "&& git apply /benchmark/tests.patch"
                ),
                "gold": (
                    "git apply --check /benchmark/tests.patch "
                    "&& git apply /benchmark/tests.patch "
                    "&& git apply --check /benchmark/gold.patch "
                    "&& git apply /benchmark/gold.patch"
                ),
            }[phase]
            patch_result = self._run(
                ["docker", "exec", container, "bash", "-c", patch_script],
                timeout=min(candidate.test_timeout_seconds, self.command_timeout_seconds),
            )
            if not patch_result.passed:
                patch_applied = False
                patch_error = patch_result.stderr_tail or patch_result.stdout_tail
                if self._looks_like_docker_error(patch_result):
                    infrastructure_error = patch_error
            else:
                timeout = min(
                    candidate.test_timeout_seconds,
                    self.command_timeout_seconds,
                )
                for test_command in candidate.test_commands:
                    command_result = self._run(
                        ["docker", "exec", container, "bash", "-c", test_command],
                        timeout=timeout,
                    )
                    commands.append(command_result)
                    if self._looks_like_docker_error(command_result):
                        infrastructure_error = (
                            command_result.stderr_tail or command_result.stdout_tail
                        )
                        break
        finally:
            self._run(["docker", "rm", "--force", container], timeout=30)

        return PhaseResult(
            phase=phase,
            repeat=repeat,
            patch_applied=patch_applied,
            patch_error=patch_error,
            commands=tuple(commands),
            infrastructure_error=infrastructure_error,
        )

    def cleanup(self, image: str) -> None:
        # The target volume belongs to a persistent worker cache lane, not to
        # this candidate. It is intentionally retained for the next task.
        self._target_volumes.pop(image, None)
        if not self.keep_images:
            self._run(["docker", "image", "rm", image], timeout=60)


def write_validation_result(path: Path, result: dict[str, Any]) -> None:
    write_json(path, result)
