#!/usr/bin/env python3
"""Evaluate submitted patches in Docker and emit scores in [0, 1]."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json
from experiment import (
    ExperimentInfrastructureError,
    cleanup_errors,
    ensure_task_image,
    remove_image,
    remove_image_containers,
    remove_volume,
    utc_now,
)
from scoring import score_evaluator_result


DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
PACKAGE_RE = re.compile(r"(?:^|\s)-p\s+([A-Za-z0-9_-]+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Run directory created by 04_run_benchmark.py")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument(
        "--keep-resources",
        action="store_true",
        help="Keep task images and Cargo volumes after evaluation",
    )
    return parser.parse_args()


def command_result(
    container: str, command: str, timeout: int
) -> tuple[str, float, str | None, bool]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["docker", "exec", container, "bash", "-c", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "error", time.monotonic() - started, "command timed out", True
    output = result.stdout[-12000:]
    status = "passed" if result.returncode == 0 else "failed"
    return status, time.monotonic() - started, None if status == "passed" else output, False


def package_from_command(command: str) -> str:
    match = PACKAGE_RE.search(command)
    return match.group(1) if match else "ruff"


def make_spec(task_id: str, target_command: str) -> dict[str, Any]:
    package = package_from_command(target_command)
    return {
        "task_id": task_id,
        "weights": {
            "core": 0.80,
            "edge": 0.0,
            "regression": 0.15,
            "quality": 0.05,
        },
        "core_failure_cap": 0.20,
        "gate_checks": [
            {
                "id": "compile",
                "command": f"cargo check --locked -p {package}",
                "description": "Affected crate compiles",
            }
        ],
        "checks": [
            {
                "id": "hidden-target",
                "group": "core",
                "command": target_command,
                "description": "PR-derived hidden behavior tests pass",
            },
            {
                "id": "regression-compile",
                "group": "regression",
                "command": f"cargo test --locked -p {package} --no-run",
                "description": "Affected crate's regression tests compile",
            },
            {
                "id": "format",
                "group": "quality",
                "command": "cargo fmt --all -- --check",
                "description": "Rust formatting is clean",
            },
        ],
    }


def patch_paths(patch: str) -> set[str]:
    return {match.group(2) for match in DIFF_PATH_RE.finditer(patch)}


def evaluate_one(
    *,
    root: Path,
    run_record_path: Path,
    tasks_dir: Path,
    commands_by_id: dict[str, list[str]],
    timeout: int,
) -> dict[str, Any]:
    evaluation_started = time.monotonic()
    evaluation_started_at = utc_now()
    run = json.loads(run_record_path.read_text(encoding="utf-8"))
    task_id = str(run["task_id"])
    destination = run_record_path.parent
    patch_file = root / str(run["patch"])
    task_dir = tasks_dir / task_id
    tests_patch = task_dir / "tests.patch"
    target_commands = commands_by_id.get(task_id)
    if not target_commands:
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        target_commands = list(task["test_commands"])
    target_command = " && ".join(target_commands)
    spec = make_spec(task_id, target_command)
    write_json(destination / "eval.json", spec)

    patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""
    hidden_paths = patch_paths(tests_patch.read_text(encoding="utf-8"))
    submitted_paths = patch_paths(patch)
    violations = [
        f"modified_evaluator_owned_path:{path}"
        for path in sorted(hidden_paths & submitted_paths)
    ]
    result: dict[str, Any] = {
        "task_id": task_id,
        "patch_applied": False,
        "evaluator_tampered": bool(violations),
        "timed_out": False,
        "infrastructure_failure": False,
        "infrastructure_failure_reason": None,
        "policy_violations": violations,
        "checks": [],
    }
    if run.get("infrastructure_error"):
        result["infrastructure_failure"] = True
        result["infrastructure_failure_reason"] = str(run["infrastructure_error"])
        write_json(destination / "checks.json", result)
        score = score_evaluator_result(spec, result)
        score["evaluation_started_at"] = evaluation_started_at
        score["evaluation_finished_at"] = utc_now()
        score["evaluation_duration_seconds"] = round(
            time.monotonic() - evaluation_started, 3
        )
        write_json(destination / "score.json", score)
        return {**run, **score}

    if not patch.strip():
        write_json(destination / "checks.json", result)
        score = score_evaluator_result(spec, result)
        score["evaluation_started_at"] = evaluation_started_at
        score["evaluation_finished_at"] = utc_now()
        score["evaluation_duration_seconds"] = round(
            time.monotonic() - evaluation_started, 3
        )
        write_json(destination / "score.json", score)
        return {**run, **score}

    container = f"mini-unsat-eval-{uuid.uuid4().hex[:12]}"
    volume = run.get("cargo_target_volume")
    docker_command = [
        "docker",
        "run",
        "--detach",
        "--name",
        container,
        "--network",
        "none",
        "--cpus",
        "4",
        "--memory",
        "8g",
    ]
    if isinstance(volume, str) and volume:
        docker_command.extend(
            ["--mount", f"type=volume,src={volume},dst=/testbed/target"]
        )
    docker_command.extend(
        [
            "--mount",
            f"type=bind,src={destination.resolve()},dst=/results,readonly",
            "--mount",
            f"type=bind,src={task_dir.resolve()},dst=/benchmark,readonly",
            str(run["image"]),
            "sleep",
            "infinity",
        ]
    )
    start = subprocess.run(
        docker_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if start.returncode != 0:
        result["infrastructure_failure"] = True
        result["infrastructure_failure_reason"] = start.stderr[-12000:]
    else:
        try:
            applied = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "bash",
                    "-c",
                    (
                        "git apply --check /results/patch.diff "
                        "&& git apply /results/patch.diff "
                        "&& git apply --check /benchmark/tests.patch "
                        "&& git apply /benchmark/tests.patch"
                    ),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
            result["patch_applied"] = applied.returncode == 0
            if applied.returncode != 0:
                result["patch_apply_error"] = applied.stdout[-12000:]
            else:
                for check in spec["gate_checks"] + spec["checks"]:
                    status, runtime, failure, timed_out = command_result(
                        container, check["command"], timeout
                    )
                    result["checks"].append(
                        {
                            "id": check["id"],
                            "status": status,
                            "runtime_seconds": round(runtime, 3),
                            "failure_reason": failure,
                        }
                    )
                    result["timed_out"] = result["timed_out"] or timed_out
                    if check["id"] == "compile" and status != "passed":
                        break
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
        finally:
            subprocess.run(
                ["docker", "rm", "--force", container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )

    write_json(destination / "checks.json", result)
    score = score_evaluator_result(spec, result)
    score["evaluation_started_at"] = evaluation_started_at
    score["evaluation_finished_at"] = utc_now()
    score["evaluation_duration_seconds"] = round(
        time.monotonic() - evaluation_started, 3
    )
    write_json(destination / "score.json", score)
    return {**run, **score}


def evaluate_task_runs(
    *,
    root: Path,
    run_paths: list[Path],
    tasks_dir: Path,
    commands_by_id: dict[str, list[str]],
    timeout: int,
    dockerfile: Path,
    context: Path,
    build_timeout_seconds: int,
    keep_resources: bool,
) -> list[dict[str, Any]]:
    first_run = json.loads(run_paths[0].read_text(encoding="utf-8"))
    task_id = str(first_run["task_id"])
    task = json.loads(
        (tasks_dir / task_id / "task.json").read_text(encoding="utf-8")
    )
    image = str(first_run["image"])
    volumes: list[str] = []
    needs_image = False
    for path in run_paths:
        run = json.loads(path.read_text(encoding="utf-8"))
        volume = run.get("cargo_target_volume")
        if isinstance(volume, str) and volume:
            volumes.append(volume)
        patch_path = root / str(run["patch"])
        needs_image = needs_image or (
            not run.get("infrastructure_error")
            and patch_path.exists()
            and bool(patch_path.read_text(encoding="utf-8").strip())
        )

    preparation_error: str | None = None
    if needs_image:
        try:
            ensure_task_image(
                image=image,
                task_id=task_id,
                base_commit=str(task["base_commit"]),
                dockerfile=dockerfile,
                context=context,
                build_timeout_seconds=build_timeout_seconds,
                precompile=bool(first_run.get("image_precompiled", False)),
            )
        except ExperimentInfrastructureError as exc:
            preparation_error = str(exc)

    results: list[dict[str, Any]] = []
    try:
        for path in run_paths:
            if preparation_error:
                run = json.loads(path.read_text(encoding="utf-8"))
                destination = path.parent
                spec = make_spec(
                    task_id,
                    " && ".join(
                        commands_by_id.get(task_id)
                        or json.loads(
                            (tasks_dir / task_id / "task.json").read_text(
                                encoding="utf-8"
                            )
                        )["test_commands"]
                    ),
                )
                write_json(destination / "eval.json", spec)
                checks = {
                    "task_id": task_id,
                    "patch_applied": False,
                    "evaluator_tampered": False,
                    "timed_out": False,
                    "infrastructure_failure": True,
                    "infrastructure_failure_reason": preparation_error,
                    "policy_violations": [],
                    "checks": [],
                }
                write_json(destination / "checks.json", checks)
                score = score_evaluator_result(spec, checks)
                write_json(destination / "score.json", score)
                results.append({**run, **score})
            else:
                results.append(
                    evaluate_one(
                        root=root,
                        run_record_path=path,
                        tasks_dir=tasks_dir,
                        commands_by_id=commands_by_id,
                        timeout=timeout,
                    )
                )
    finally:
        if not keep_resources:
            cleanup = cleanup_errors(
                [remove_image_containers(image)]
                + [remove_volume(volume) for volume in set(volumes)]
            )
            image_error = remove_image(image)
            if image_error:
                cleanup.append(image_error)
            cleaned_at = utc_now()
            for path, result in zip(run_paths, results, strict=False):
                cleanup_record = {
                    "cleanup_attempted": True,
                    "resources_cleaned": not cleanup,
                    "resources_retained": False,
                    "cleanup_errors": cleanup,
                    "cleaned_at": cleaned_at,
                }
                result.update(cleanup_record)
                run = json.loads(path.read_text(encoding="utf-8"))
                run.update(cleanup_record)
                write_json(path, run)
                score_path = path.parent / "score.json"
                if score_path.exists():
                    score = json.loads(score_path.read_text(encoding="utf-8"))
                    score.update(cleanup_record)
                    write_json(score_path, score)
        else:
            for path, result in zip(run_paths, results, strict=False):
                retention_record = {
                    "cleanup_attempted": False,
                    "resources_cleaned": False,
                    "resources_retained": True,
                    "cleanup_errors": [],
                }
                result.update(retention_record)
                run = json.loads(path.read_text(encoding="utf-8"))
                run.update(retention_record)
                write_json(path, run)
                score_path = path.parent / "score.json"
                if score_path.exists():
                    score = json.loads(score_path.read_text(encoding="utf-8"))
                    score.update(retention_record)
                    write_json(score_path, score)
    return results


def main() -> int:
    args = parse_args()
    config, root = load_config(args.config)
    run_dir = resolve_path(args.run_dir, root)
    tasks_dir = resolve_path(config["paths"]["tasks_dir"], root)
    pool = read_jsonl(
        resolve_path(
            config.get("triage", {}).get(
                "dynamic_pool_file", config["paths"]["validation_queue_file"]
            ),
            root,
        )
    )
    commands_by_id = {
        str(record["task_id"]): list(record.get("suggested_test_commands", []))
        for record in pool
    }
    run_paths = sorted(run_dir.glob("*/*/run.json"))
    if args.task_ids:
        selected = set(args.task_ids)
        run_paths = [
            path
            for path in run_paths
            if json.loads(path.read_text(encoding="utf-8")).get("task_id")
            in selected
        ]
    if not run_paths:
        raise SystemExit(f"No run.json files found under {run_dir}")
    workers = args.workers or int(config.get("benchmark", {}).get("workers", 4))
    grouped_paths: dict[str, list[Path]] = {}
    for path in run_paths:
        run = json.loads(path.read_text(encoding="utf-8"))
        grouped_paths.setdefault(str(run["task_id"]), []).append(path)
    validation_config = config.get("validation", {})
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                evaluate_task_runs,
                root=root,
                run_paths=paths,
                tasks_dir=tasks_dir,
                commands_by_id=commands_by_id,
                timeout=args.timeout,
                dockerfile=root / "environment" / "Dockerfile",
                context=root / "environment",
                build_timeout_seconds=int(
                    validation_config.get("build_timeout_seconds", 3600)
                ),
                keep_resources=args.keep_resources,
            )
            for paths in grouped_paths.values()
        ]
        results = []
        for future in concurrent.futures.as_completed(futures):
            task_results = future.result()
            results.extend(task_results)
            for result in task_results:
                print(
                    f"[{result['model_id']}/{result['task_id']}] "
                    f"score={result['score']} resolved={result['fully_resolved']}"
                )
    scores_path = run_dir / "scores.json"
    existing: list[dict[str, Any]] = []
    if scores_path.exists():
        try:
            loaded = json.loads(scores_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = []
    merged = {
        (str(item["model_id"]), str(item["task_id"])): item
        for item in existing + results
    }
    write_json(
        scores_path,
        sorted(
            merged.values(),
            key=lambda item: (item["model_id"], item["task_id"]),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
