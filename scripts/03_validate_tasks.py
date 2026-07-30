#!/usr/bin/env python3
"""Dynamically validate generated Ruff candidates in isolated containers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from _common import load_config, read_jsonl, resolve_path, write_jsonl
from validation import (
    CandidateInputError,
    CandidatePreparationError,
    DockerBackend,
    ValidationInfrastructureError,
    read_candidate,
    stage_candidate,
    validate_candidate,
    write_validation_result,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="Validate this task ID; repeat the option to select multiple tasks",
    )
    parser.add_argument("--subsystem", action="append", dest="subsystems")
    parser.add_argument(
        "--pool-file",
        help="Use this ranked JSONL pool instead of triage.dynamic_pool_file",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip candidates that already have a saved dynamic-validation result",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and display candidate inputs without starting Docker",
    )
    parser.add_argument(
        "--rebuild-images",
        action="store_true",
        help="Rebuild images even if a correctly labeled image already exists",
    )
    parser.add_argument(
        "--precompile",
        action="store_true",
        help="Precompile core Ruff tests while building each image",
    )
    return parser.parse_args()


def select_candidate_ids(
    records: list[dict[str, Any]],
    *,
    task_ids: list[str] | None,
    subsystems: list[str] | None,
    limit: int | None,
) -> list[str]:
    available = {str(record["task_id"]): record for record in records}
    if task_ids:
        missing = sorted(set(task_ids) - set(available))
        if missing:
            raise CandidateInputError(
                f"task IDs are not present in the selected candidate pool: {missing}"
            )
        selected = [task_id for task_id in task_ids if task_id in available]
    else:
        selected = [
            str(record["task_id"])
            for record in records
            if not subsystems or record.get("subsystem") in set(subsystems)
        ]
    selected = list(dict.fromkeys(selected))
    if limit is not None:
        if limit <= 0:
            raise CandidateInputError("--limit must be positive")
        selected = selected[:limit]
    if not selected:
        raise CandidateInputError("no candidates matched the selection")
    return selected


def acquire_validator_lock(output_root: Path) -> TextIO:
    """Prevent concurrent validator processes from claiming the same tasks."""

    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "validator.lock"
    lock = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.seek(0)
        owner = lock.read().strip() or "unknown"
        lock.close()
        raise CandidateInputError(
            "another validation process is already running "
            f"(owner PID: {owner}); wait for it to finish instead of "
            "starting an overlapping --skip-existing run"
        ) from exc
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def main() -> int:
    args = parse_args()
    config, config_dir = load_config(args.config)
    paths = config["paths"]
    validation_config = config.get("validation", {})
    tasks_dir = resolve_path(paths["tasks_dir"], config_dir)
    output_root = resolve_path(
        validation_config.get("output_dir", ".cache/validation"),
        config_dir,
    )
    task_results_dir = output_root / "tasks"
    dynamic_pool = resolve_path(
        args.pool_file
        or config.get("triage", {}).get(
            "dynamic_pool_file",
            paths["validation_queue_file"],
        ),
        config_dir,
    )
    records = read_jsonl(dynamic_pool)
    if not records:
        raise SystemExit(
            f"No dynamic-validation pool found at {dynamic_pool}; "
            "run scripts/02b_triage_candidates.py first"
        )
    if args.skip_existing:
        completed_ids = {
            result_path.stem for result_path in task_results_dir.glob("*.json")
        }
        records = [
            record
            for record in records
            if str(record.get("task_id")) not in completed_ids
        ]
    task_ids = select_candidate_ids(
        records,
        task_ids=args.task_ids,
        subsystems=args.subsystems,
        limit=args.limit,
    )

    records_by_id = {str(record["task_id"]): record for record in records}
    candidates = []
    for task_id in task_ids:
        candidate = read_candidate(tasks_dir / task_id)
        suggested = records_by_id[task_id].get("suggested_test_commands")
        if isinstance(suggested, list) and suggested and all(
            isinstance(command, str) and command.strip() for command in suggested
        ):
            candidate = replace(candidate, test_commands=tuple(suggested))
        candidates.append(candidate)
    dry_run_records = [
        {
            "task_id": candidate.task_id,
            "base_commit": candidate.base_commit,
            "subsystem": candidate.subsystem,
            "test_commands": list(candidate.test_commands),
            "test_timeout_seconds": candidate.test_timeout_seconds,
            "source_dir": str(candidate.source_dir),
        }
        for candidate in candidates
    ]
    if args.dry_run:
        print(json.dumps(dry_run_records, indent=2, sort_keys=True))
        return 0

    # Keep the handle alive until main exits. The OS releases the advisory
    # lock automatically after normal completion, failure, or interruption.
    try:
        validator_lock = acquire_validator_lock(output_root)
    except CandidateInputError as exc:
        raise SystemExit(str(exc)) from exc
    staging_root = output_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    task_results_dir.mkdir(parents=True, exist_ok=True)

    backend = DockerBackend(
        dockerfile=config_dir / "environment" / "Dockerfile",
        context=config_dir / "environment",
        image_prefix=str(
            validation_config.get(
                "image_prefix",
                "mini-unsat-ruff-validation",
            )
        ),
        build_timeout_seconds=int(
            validation_config.get("build_timeout_seconds", 3600)
        ),
        container_start_timeout_seconds=int(
            validation_config.get("container_start_timeout_seconds", 60)
        ),
        command_timeout_seconds=int(
            validation_config.get("command_timeout_seconds", 900)
        ),
        cpus=validation_config.get("cpus", 4),
        memory=str(validation_config.get("memory", "8g")),
        precompile=bool(args.precompile or validation_config.get("precompile", False)),
        rebuild_images=bool(
            args.rebuild_images or validation_config.get("rebuild_images", False)
        ),
        keep_images=bool(validation_config.get("keep_images", True)),
        output_tail_characters=int(
            validation_config.get("output_tail_characters", 12000)
        ),
    )
    try:
        docker_version = backend.ensure_available()
    except ValidationInfrastructureError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Using Docker {docker_version}")

    repeats = (
        args.repeats
        if args.repeats is not None
        else int(validation_config.get("repeats", 3))
    )
    if repeats <= 0:
        raise SystemExit("--repeats must be positive")

    workers = (
        args.workers
        if args.workers is not None
        else int(validation_config.get("workers", 1))
    )
    if workers <= 0:
        raise SystemExit("--workers must be positive")

    def validate_one(candidate: Any) -> dict[str, Any]:
        print(f"[{candidate.task_id}] staging candidate artifacts")
        staged, artifact_hashes = stage_candidate(candidate, staging_root)
        # Staging intentionally copies the original artifacts unchanged. Keep
        # the triage command override in memory for validation.
        staged = replace(staged, test_commands=candidate.test_commands)
        try:
            result = validate_candidate(
                staged,
                staged.source_dir,
                backend,
                repeats,
                artifact_hashes,
            )
        except CandidatePreparationError as exc:
            result = {
                "task_id": candidate.task_id,
                "base_commit": candidate.base_commit,
                "subsystem": candidate.subsystem,
                "status": "rejected",
                "reasons": ["environment_preparation_failure"],
                "artifact_hashes": artifact_hashes,
                "environment": {},
                "phase_results": [],
                "infrastructure_errors": [],
                "preparation_error": str(exc),
            }
        except ValidationInfrastructureError as exc:
            result = {
                "task_id": candidate.task_id,
                "base_commit": candidate.base_commit,
                "subsystem": candidate.subsystem,
                "status": "infrastructure_error",
                "reasons": [],
                "artifact_hashes": artifact_hashes,
                "environment": {},
                "phase_results": [],
                "infrastructure_errors": [str(exc)],
            }
        write_validation_result(task_results_dir / f"{candidate.task_id}.json", result)
        print(
            f"[{candidate.task_id}] {result['status']}"
            + (f": {', '.join(result['reasons'])}" if result["reasons"] else "")
        )
        return {
            "task_id": result["task_id"],
            "base_commit": result["base_commit"],
            "subsystem": result["subsystem"],
            "status": result["status"],
            "reasons": result["reasons"],
        }

    if workers == 1:
        summaries = [validate_one(candidate) for candidate in candidates]
    else:
        print(f"Validating with {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            summaries = list(executor.map(validate_one, candidates))

    all_summaries: list[dict[str, Any]] = []
    for result_path in sorted(task_results_dir.glob("*.json")):
        try:
            saved_result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read validation result {result_path}: {exc}") from exc
        all_summaries.append(
            {
                "task_id": saved_result["task_id"],
                "base_commit": saved_result["base_commit"],
                "subsystem": saved_result["subsystem"],
                "status": saved_result["status"],
                "reasons": saved_result["reasons"],
            }
        )
    write_jsonl(output_root / "summary.jsonl", all_summaries)
    counts: dict[str, int] = {}
    for summary in summaries:
        counts[summary["status"]] = counts.get(summary["status"], 0) + 1
    print(json.dumps({"output": str(output_root), "counts": counts}, sort_keys=True))
    validator_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
