#!/usr/bin/env python3
"""Run mini-swe-agent over validated Ruff tasks through OpenRouter."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json
from experiment import (
    ExperimentInfrastructureError,
    cleanup_errors,
    ensure_task_image,
    remove_image,
    remove_volume,
    sha256_file,
    trajectory_metrics,
    utc_now,
)


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--models", default="benchmark/models.yaml")
    parser.add_argument("--run-id")
    parser.add_argument("--model", action="append", dest="model_ids")
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--step-limit", type=int)
    parser.add_argument("--cost-limit", type=float)
    parser.add_argument("--wall-time-limit-seconds", type=int)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Resume a run without repeating completed model/task jobs",
    )
    parser.add_argument("--allow-unvalidated", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-evaluation",
        action="store_true",
        help="Collect patches without immediately evaluating them",
    )
    parser.add_argument(
        "--keep-resources",
        action="store_true",
        help="Keep task images and Cargo volumes after each task",
    )
    parser.add_argument("--evaluation-timeout", type=int, default=900)
    return parser.parse_args()


def load_dotenv(path: Path, environment: dict[str, str]) -> None:
    """Load simple KEY=VALUE entries without logging credential values."""

    if not path.exists():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}:{line_number}: invalid variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        environment.setdefault(key, value)


def image_tag(prefix: str, task_id: str) -> str:
    return f"{prefix}:{task_id.lower().replace('_', '-')}"


def completed_job(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return result.get("infrastructure_error") is None


def completed_score(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(result.get("scorable", False))


def load_models(path: Path, root: Path) -> list[dict[str, Any]]:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        raise ValueError(f"{path} must define a models list")
    models: list[dict[str, Any]] = []
    for record in value["models"]:
        if not isinstance(record, dict) or not record.get("enabled", True):
            continue
        model_id = str(record.get("id", ""))
        if not SAFE_ID_RE.fullmatch(model_id):
            raise ValueError(f"unsafe model id: {model_id!r}")
        model_config = resolve_path(str(record["config"]), root)
        model_values = yaml.safe_load(model_config.read_text(encoding="utf-8"))
        configured_name = None
        if isinstance(model_values, dict) and isinstance(model_values.get("model"), dict):
            configured_name = model_values["model"].get("model_name")
        models.append(
            {
                **record,
                "id": model_id,
                "config_path": model_config,
                "configured_model_name": configured_name,
            }
        )
    return models


def validated_ids(validation_dir: Path) -> set[str]:
    result: set[str] = set()
    for record in read_jsonl(validation_dir / "summary.jsonl"):
        if record.get("status") == "validated":
            result.add(str(record["task_id"]))
    return result


def select_tasks(
    *,
    tasks_dir: Path,
    pool: list[dict[str, Any]],
    explicit_ids: list[str] | None,
    valid_ids: set[str],
    allow_unvalidated: bool,
    limit: int,
) -> list[dict[str, Any]]:
    by_id = {str(record["task_id"]): record for record in pool}
    ids = explicit_ids or [str(record["task_id"]) for record in pool]
    missing = [task_id for task_id in ids if task_id not in by_id]
    if missing:
        raise ValueError(f"tasks not present in dynamic pool: {missing}")
    if not allow_unvalidated:
        ids = [task_id for task_id in ids if task_id in valid_ids]
    selected: list[dict[str, Any]] = []
    for task_id in ids[:limit]:
        task = json.loads(
            (tasks_dir / task_id / "task.json").read_text(encoding="utf-8")
        )
        selected.append({**by_id[task_id], "prompt": task["prompt"]})
    return selected


def run_one(
    *,
    root: Path,
    mini: Path,
    shared_config: Path,
    output_root: Path,
    task: dict[str, Any],
    model: dict[str, Any],
    image: str,
    docker_run_args: list[str],
    run_id: str,
    environment: dict[str, str],
    image_metadata: dict[str, Any],
    limits: dict[str, Any],
    reproducibility: dict[str, Any],
) -> dict[str, Any]:
    import yaml

    task_id = str(task["task_id"])
    model_id = str(model["id"])
    destination = output_root / model_id / task_id
    destination.mkdir(parents=True, exist_ok=True)
    trajectory = destination / "trajectory.json"
    log_path = destination / "runner.log"
    patch_path = destination / "patch.diff"
    result_path = destination / "run.json"
    overlay_path = destination / "environment.yaml"
    volume = (
        f"mini-unsat-run-{run_id}-{model_id}-{task_id}"
        .lower()
        .replace("_", "-")
        .replace(".", "-")
    )
    overlay = {
        "agent": {
            "step_limit": limits["step_limit"],
            "cost_limit": limits["cost_limit"],
            "wall_time_limit_seconds": limits["wall_time_limit_seconds"],
        },
        "environment": {
            "image": image,
            "run_args": docker_run_args
            + ["--mount", f"type=volume,src={volume},dst=/testbed/target"],
        }
    }
    overlay_path.write_text(
        yaml.safe_dump(overlay, sort_keys=False),
        encoding="utf-8",
    )

    started_at = utc_now()
    started = time.monotonic()
    command = [
        str(mini),
        "--yolo",
        "--exit-immediately",
        "--config",
        str(shared_config),
        "--config",
        str(model["config_path"]),
        "--config",
        str(overlay_path),
        "--task",
        str(task["prompt"]),
        "--output",
        str(trajectory),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(limits["wall_time_limit_seconds"]) + 300,
            check=False,
        )
        output = completed.stdout
        return_code = completed.returncode
        infrastructure_error = None
    except subprocess.TimeoutExpired as exc:
        output = (
            exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        output += "\nrunner exceeded wall-time grace period"
        return_code = None
        infrastructure_error = "mini-swe-agent runner timeout"
    except OSError as exc:
        output = str(exc)
        return_code = None
        infrastructure_error = str(exc)
    log_path.write_text(output, encoding="utf-8")

    submission = ""
    info: dict[str, Any] = {}
    metrics: dict[str, Any] = {
        "steps_used": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "provider_response_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
        "cost_complete": False,
        "providers": [],
        "provider_models": [],
    }
    if trajectory.exists():
        try:
            trajectory_data = json.loads(trajectory.read_text(encoding="utf-8"))
            info = trajectory_data.get("info", {})
            if not isinstance(info, dict):
                raise AttributeError("trajectory info must be an object")
            metrics = trajectory_metrics(trajectory_data)
            submission = info.get("submission") or ""
            exit_status = str(info.get("exit_status") or "")
            if exit_status in {
                "OpenRouterAPIError",
                "OpenRouterAuthenticationError",
                "OpenRouterRateLimitError",
            }:
                infrastructure_error = f"provider failure: {exit_status}"
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            infrastructure_error = f"invalid trajectory: {exc}"
    elif return_code != 0 and infrastructure_error is None:
        infrastructure_error = (
            f"mini-swe-agent exited {return_code} before writing a trajectory"
        )
    patch_path.write_text(submission, encoding="utf-8")
    result = {
        "task_id": task_id,
        "model_id": model_id,
        "image": image,
        "image_id": image_metadata.get("image_id"),
        "image_reused": image_metadata.get("image_reused"),
        "image_prepare_seconds": image_metadata.get("image_prepare_seconds"),
        "cargo_target_volume": volume,
        "return_code": return_code,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_status": info.get("exit_status"),
        "model_stats": info.get("model_stats", {}),
        **metrics,
        "step_limit": limits["step_limit"],
        "cost_limit": limits["cost_limit"],
        "wall_time_limit_seconds": limits["wall_time_limit_seconds"],
        "hit_step_limit": metrics["steps_used"] >= int(limits["step_limit"]),
        "hit_cost_limit": metrics["cost_usd"] >= float(limits["cost_limit"]),
        "hit_wall_time_limit": (
            time.monotonic() - started
            >= 0.98 * float(limits["wall_time_limit_seconds"])
        ),
        "timed_out": info.get("exit_status")
        in {"Timeout", "WallTimeExceeded", "RunnerTimeout"},
        "has_submission": bool(submission.strip()),
        "infrastructure_error": infrastructure_error,
        "trajectory": str(trajectory.relative_to(root)),
        "patch": str(patch_path.relative_to(root)),
        "configured_model_name": model.get("configured_model_name"),
        "subsystem": task.get("subsystem"),
        "priority_score": task.get("priority_score"),
        "base_commit": task.get("base_commit"),
        "reproducibility": reproducibility,
    }
    write_json(result_path, result)
    return result


def main() -> int:
    args = parse_args()
    config, root = load_config(args.config)
    benchmark_config = config.get("benchmark", {})
    validation_config = config.get("validation", {})
    paths = config["paths"]

    environment = dict(os.environ)
    load_dotenv(root / ".env.local", environment)
    environment["MSWEA_GLOBAL_CONFIG_DIR"] = str(root / ".cache" / "mini-swe-agent")
    environment["MSWEA_SILENT_STARTUP"] = "1"
    environment["MSWEA_CONFIGURED"] = "1"

    models_path = resolve_path(args.models, root)
    models = load_models(models_path, root)
    if args.model_ids:
        wanted = set(args.model_ids)
        models = [model for model in models if model["id"] in wanted]
        missing_models = wanted - {model["id"] for model in models}
        if missing_models:
            raise SystemExit(f"Unknown or disabled models: {sorted(missing_models)}")
    for model in models:
        key_name = str(model.get("api_key_env", "OPENROUTER_API_KEY"))
        if not args.dry_run and not environment.get(key_name):
            raise SystemExit(
                f"{key_name} is not set; add it to .env.local or export it"
            )

    frozen_index = resolve_path(
        paths.get("benchmark_index_file", "tasks/index.jsonl"), root
    )
    pool_path = (
        frozen_index
        if frozen_index.exists() and not args.task_ids
        else resolve_path(
            config.get("triage", {}).get(
                "dynamic_pool_file", paths["validation_queue_file"]
            ),
            root,
        )
    )
    pool = read_jsonl(pool_path)
    validation_dir = resolve_path(
        validation_config.get("output_dir", ".cache/validation"), root
    )
    limit = args.limit or int(benchmark_config.get("task_limit", 100))
    tasks = select_tasks(
        tasks_dir=resolve_path(paths["tasks_dir"], root),
        pool=pool,
        explicit_ids=args.task_ids,
        valid_ids=validated_ids(validation_dir),
        allow_unvalidated=args.allow_unvalidated,
        limit=limit,
    )
    if not tasks:
        raise SystemExit(
            "No validated tasks selected. Run scripts/03_validate_tasks.py first."
        )

    prefix = str(
        validation_config.get("image_prefix", "mini-unsat-ruff-validation")
    )
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    if not SAFE_ID_RE.fullmatch(run_id):
        raise SystemExit("--run-id may contain only letters, digits, dot, dash, underscore")
    output_root = (
        resolve_path(benchmark_config.get("output_dir", "results/runs"), root)
        / run_id
    )
    shared_config = root / "benchmark" / "mini.yaml"
    import yaml

    shared_values = yaml.safe_load(shared_config.read_text(encoding="utf-8"))
    docker_run_args = list(shared_values["environment"]["run_args"])
    agent_values = shared_values["agent"]
    limits = {
        "step_limit": int(
            args.step_limit
            or benchmark_config.get("step_limit", agent_values["step_limit"])
        ),
        "cost_limit": float(
            args.cost_limit
            if args.cost_limit is not None
            else benchmark_config.get("cost_limit", agent_values["cost_limit"])
        ),
        "wall_time_limit_seconds": int(
            args.wall_time_limit_seconds
            or benchmark_config.get(
                "wall_time_limit_seconds",
                agent_values["wall_time_limit_seconds"],
            )
        ),
    }
    if (
        limits["step_limit"] <= 0
        or limits["cost_limit"] <= 0
        or limits["wall_time_limit_seconds"] <= 0
    ):
        raise SystemExit("step, cost, and wall-time limits must be positive")
    reproducibility_base = {
        "mini_version": "2.4.5",
        "shared_config_sha256": sha256_file(shared_config),
        "models_registry_sha256": sha256_file(models_path),
        "task_index_sha256": sha256_file(pool_path),
        "dockerfile_sha256": sha256_file(root / "environment" / "Dockerfile"),
        "setup_script_sha256": sha256_file(root / "environment" / "setup.sh"),
    }
    pending_pairs = [
        (task, model)
        for task in tasks
        for model in models
        if not (
            args.skip_existing
            and completed_job(
                output_root / model["id"] / task["task_id"] / "run.json"
            )
            and (
                args.skip_evaluation
                or completed_score(
                    output_root / model["id"] / task["task_id"] / "score.json"
                )
            )
        )
    ]
    existing_manifest: dict[str, Any] = {}
    manifest_path = output_root / "manifest.json"
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            existing_manifest = {}
    manifest_tasks = sorted(
        set(existing_manifest.get("tasks", []))
        | {str(task["task_id"]) for task in tasks}
    )
    manifest_models = sorted(
        set(existing_manifest.get("models", []))
        | {str(model["id"]) for model in models}
    )
    manifest = {
        "run_id": run_id,
        "created_at": existing_manifest.get(
            "created_at", dt.datetime.now(dt.timezone.utc).isoformat()
        ),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tasks": manifest_tasks,
        "models": manifest_models,
        "job_count": len(manifest_tasks) * len(manifest_models),
        "pending_job_count": len(pending_pairs),
        "mini_version": "2.4.5",
        "integrated_evaluation": not args.skip_evaluation,
        "limits": limits,
        "reproducibility": reproducibility_base,
        "model_configs": {
            str(model["id"]): {
                "configured_model_name": model.get("configured_model_name"),
                "sha256": sha256_file(Path(model["config_path"])),
            }
            for model in models
        },
    }
    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, manifest)
    if not pending_pairs:
        print("All selected model/task jobs already exist")
        return 0

    mini = root / ".venv" / "bin" / "mini"
    if not mini.exists():
        raise SystemExit("mini-swe-agent is not installed at .venv/bin/mini")

    workers = args.workers or int(benchmark_config.get("workers", 4))
    runs_path = output_root / "runs.json"
    existing_results: list[dict[str, Any]] = []
    if runs_path.exists():
        try:
            loaded_results = json.loads(runs_path.read_text(encoding="utf-8"))
            if isinstance(loaded_results, list):
                existing_results = loaded_results
        except (OSError, json.JSONDecodeError):
            pass
    tasks_dir = resolve_path(paths["tasks_dir"], root)
    dynamic_pool = read_jsonl(
        resolve_path(
            config.get("triage", {}).get(
                "dynamic_pool_file", paths["validation_queue_file"]
            ),
            root,
        )
    )
    commands_by_id = {
        str(record["task_id"]): list(record.get("suggested_test_commands", []))
        for record in dynamic_pool
    }
    evaluator = importlib.import_module("04b_evaluate_results")
    pending_keys = {
        (str(task["task_id"]), str(model["id"]))
        for task, model in pending_pairs
    }

    def infrastructure_result(
        task: dict[str, Any],
        model: dict[str, Any],
        image: str,
        error: str,
    ) -> dict[str, Any]:
        task_id = str(task["task_id"])
        model_id = str(model["id"])
        destination = output_root / model_id / task_id
        destination.mkdir(parents=True, exist_ok=True)
        patch_path = destination / "patch.diff"
        trajectory_path = destination / "trajectory.json"
        patch_path.write_text("", encoding="utf-8")
        result = {
            "task_id": task_id,
            "model_id": model_id,
            "configured_model_name": model.get("configured_model_name"),
            "subsystem": task.get("subsystem"),
            "priority_score": task.get("priority_score"),
            "base_commit": task.get("base_commit"),
            "image": image,
            "image_id": None,
            "image_reused": False,
            "image_prepare_seconds": None,
            "cargo_target_volume": (
                f"mini-unsat-run-{run_id}-{model_id}-{task_id}"
                .lower()
                .replace("_", "-")
                .replace(".", "-")
            ),
            "return_code": None,
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "duration_seconds": 0.0,
            "exit_status": "InfrastructureError",
            "model_stats": {},
            "steps_used": 0,
            "assistant_turns": 0,
            "tool_calls": 0,
            "provider_response_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "cost_usd": 0.0,
            "cost_complete": False,
            "providers": [],
            "provider_models": [],
            **limits,
            "hit_step_limit": False,
            "hit_cost_limit": False,
            "hit_wall_time_limit": False,
            "timed_out": False,
            "has_submission": False,
            "infrastructure_error": error,
            "trajectory": str(trajectory_path.relative_to(root)),
            "patch": str(patch_path.relative_to(root)),
            "reproducibility": {
                **reproducibility_base,
                "model_config_sha256": sha256_file(Path(model["config_path"])),
            },
        }
        write_json(destination / "run.json", result)
        return result

    def process_task(task: dict[str, Any]) -> list[dict[str, Any]]:
        task_id = str(task["task_id"])
        image = image_tag(prefix, task_id)
        models_to_run = [
            model
            for model in models
            if (task_id, str(model["id"])) in pending_keys
            and not (
                args.skip_existing
                and completed_job(
                    output_root / model["id"] / task_id / "run.json"
                )
            )
        ]
        try:
            image_metadata = ensure_task_image(
                image=image,
                task_id=task_id,
                base_commit=str(task["base_commit"]),
                dockerfile=root / "environment" / "Dockerfile",
                context=root / "environment",
                build_timeout_seconds=int(
                    validation_config.get("build_timeout_seconds", 3600)
                ),
                precompile=False,
            )
        except ExperimentInfrastructureError as exc:
            task_results = [
                infrastructure_result(task, model, image, str(exc))
                for model in models_to_run
            ]
        else:
            task_results = []
            for model in models_to_run:
                result = run_one(
                    root=root,
                    mini=mini,
                    shared_config=shared_config,
                    output_root=output_root,
                    task=task,
                    model=model,
                    image=image,
                    docker_run_args=docker_run_args,
                    run_id=run_id,
                    environment=environment,
                    image_metadata=image_metadata,
                    limits=limits,
                    reproducibility={
                        **reproducibility_base,
                        "model_config_sha256": sha256_file(
                            Path(model["config_path"])
                        ),
                    },
                )
                task_results.append(result)
                print(
                    f"[{model['id']}/{task_id}] exit={result['exit_status']} "
                    f"patch={result['has_submission']} "
                    f"steps={result['steps_used']} "
                    f"cost=${result['cost_usd']:.4f} "
                    f"seconds={result['duration_seconds']}"
                )

        run_paths = [
            output_root / model["id"] / task_id / "run.json"
            for model in models
            if (
                (task_id, str(model["id"])) in pending_keys
                and (output_root / model["id"] / task_id / "run.json").exists()
            )
        ]
        if not args.skip_evaluation and run_paths:
            evaluated = evaluator.evaluate_task_runs(
                root=root,
                run_paths=run_paths,
                tasks_dir=tasks_dir,
                commands_by_id=commands_by_id,
                timeout=args.evaluation_timeout,
                dockerfile=root / "environment" / "Dockerfile",
                context=root / "environment",
                build_timeout_seconds=int(
                    validation_config.get("build_timeout_seconds", 3600)
                ),
                keep_resources=args.keep_resources,
            )
            for result in evaluated:
                print(
                    f"[{result['model_id']}/{task_id}] "
                    f"score={result['score']} resolved={result['fully_resolved']}"
                )
        elif not args.keep_resources:
            volumes = [
                str(result["cargo_target_volume"])
                for result in task_results
                if result.get("cargo_target_volume")
            ]
            cleanup = cleanup_errors(remove_volume(volume) for volume in volumes)
            image_error = remove_image(image)
            if image_error:
                cleanup.append(image_error)
            if cleanup:
                for result in task_results:
                    result.setdefault("cleanup_errors", []).extend(cleanup)
        return task_results

    results: list[dict[str, Any]] = []
    pending_task_ids = {str(task["task_id"]) for task, _model in pending_pairs}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_tasks = {
            executor.submit(process_task, task): str(task["task_id"])
            for task in tasks
            if str(task["task_id"]) in pending_task_ids
        }
        for future in concurrent.futures.as_completed(future_tasks):
            results.extend(future.result())
    merged_results = {
        (str(item["model_id"]), str(item["task_id"])): item
        for item in existing_results + results
    }
    write_json(
        runs_path,
        sorted(
            merged_results.values(),
            key=lambda item: (item["model_id"], item["task_id"]),
        ),
    )
    if not args.skip_evaluation:
        score_records: list[dict[str, Any]] = []
        for path in sorted(output_root.glob("*/*/score.json")):
            score = json.loads(path.read_text(encoding="utf-8"))
            run = json.loads((path.parent / "run.json").read_text(encoding="utf-8"))
            score_records.append({**run, **score})
        write_json(
            output_root / "scores.json",
            sorted(
                score_records,
                key=lambda item: (item["model_id"], item["task_id"]),
            ),
        )
    return 0 if all(result["infrastructure_error"] is None for result in results) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
