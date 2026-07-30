#!/usr/bin/env python3
"""Overlap validation and model runs, with live progress and ETA reports."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--run-id", default="final")
    parser.add_argument("--target-valid", type=int, default=100)
    parser.add_argument("--validation-workers", type=int, default=2)
    parser.add_argument("--validation-batch-size", type=int, default=6)
    parser.add_argument("--agent-workers", type=int, default=2)
    parser.add_argument("--agent-batch-size", type=int, default=4)
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument(
        "--skip-evaluation",
        action="store_true",
        help="Stop after model trajectories have been collected",
    )
    return parser.parse_args()


def task_statuses(results_dir: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for path in results_dir.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(result.get("task_id"), str):
            statuses[result["task_id"]] = str(result.get("status"))
    return statuses


def job_complete(path: Path) -> bool:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return result.get("infrastructure_error") is None


def main() -> int:
    args = parse_args()
    config, root = load_config(args.config)
    scripts = Path(__file__).parent
    validation_root = resolve_path(
        config.get("validation", {}).get(
            "output_dir", ".cache/validation"
        ),
        root,
    )
    validation_results = validation_root / "tasks"
    run_root = (
        resolve_path(
            config.get("benchmark", {}).get("output_dir", "results/runs"),
            root,
        )
        / args.run_id
    )
    pool = read_jsonl(
        resolve_path(
            config.get("triage", {}).get(
                "dynamic_pool_file", config["paths"]["validation_queue_file"]
            ),
            root,
        )
    )
    ranked_ids = [str(record["task_id"]) for record in pool]
    import yaml

    model_registry = yaml.safe_load(
        (root / "benchmark" / "models.yaml").read_text(encoding="utf-8")
    )
    model_ids = [
        str(model["id"])
        for model in model_registry["models"]
        if model.get("enabled", True)
    ]
    total_jobs = args.target_valid * len(model_ids)
    pipeline_started = time.monotonic()

    validator = subprocess.Popen(
        [
            sys.executable,
            str(scripts / "03c_validate_until.py"),
            "--config",
            args.config,
            "--target-valid",
            str(args.target_valid),
            "--batch-size",
            str(args.validation_batch_size),
            "--workers",
            str(args.validation_workers),
            "--repeats",
            "1",
        ],
        cwd=root,
    )
    agent: subprocess.Popen[Any] | None = None

    while True:
        statuses = task_statuses(validation_results)
        valid_ids = [
            task_id
            for task_id in ranked_ids
            if statuses.get(task_id) == "validated"
        ][: args.target_valid]
        completed_jobs = sum(
            job_complete(run_root / model_id / task_id / "run.json")
            for task_id in valid_ids
            for model_id in model_ids
        )
        complete_tasks = {
            task_id
            for task_id in valid_ids
            if all(
                job_complete(run_root / model_id / task_id / "run.json")
                for model_id in model_ids
            )
        }
        pending_tasks = [
            task_id for task_id in valid_ids if task_id not in complete_tasks
        ]

        if agent is not None and agent.poll() is not None:
            if agent.returncode not in (0, 2):
                validator.terminate()
                return int(agent.returncode or 1)
            agent = None
        if agent is None and pending_tasks:
            selected = pending_tasks[: args.agent_batch_size]
            command = [
                sys.executable,
                str(scripts / "04_run_benchmark.py"),
                "--config",
                args.config,
                "--run-id",
                args.run_id,
                "--workers",
                str(args.agent_workers),
                "--skip-existing",
            ]
            for task_id in selected:
                command.extend(["--task-id", task_id])
            agent = subprocess.Popen(command, cwd=root)

        elapsed_hours = (time.monotonic() - pipeline_started) / 3600
        jobs_per_hour = completed_jobs / elapsed_hours if elapsed_hours > 0 else 0
        model_eta = (
            (total_jobs - completed_jobs) / jobs_per_hour
            if jobs_per_hour > 0
            else None
        )
        report = {
            "run_id": args.run_id,
            "validated": len(valid_ids),
            "target_valid": args.target_valid,
            "model_jobs_complete": completed_jobs,
            "model_jobs_total": total_jobs,
            "elapsed_hours": round(elapsed_hours, 3),
            "model_jobs_per_hour": round(jobs_per_hour, 2),
            "model_eta_hours": round(model_eta, 2)
            if model_eta is not None
            else None,
            "validator_running": validator.poll() is None,
            "agent_batch_running": agent is not None,
        }
        write_json(validation_root / "pipeline-progress.json", report)
        print(json.dumps(report, sort_keys=True), flush=True)

        validator_done = validator.poll() is not None
        models_done = len(valid_ids) >= args.target_valid and completed_jobs >= total_jobs
        if validator_done and validator.returncode != 0:
            if agent is not None:
                agent.terminate()
            return int(validator.returncode or 1)
        if validator_done and models_done and agent is None:
            break
        time.sleep(args.poll_seconds)

    freeze = subprocess.run(
        [
            sys.executable,
            str(scripts / "03b_freeze_tasks.py"),
            "--config",
            args.config,
        ],
        cwd=root,
        check=False,
    )
    if freeze.returncode != 0 or args.skip_evaluation:
        return freeze.returncode
    evaluation = subprocess.run(
        [
            sys.executable,
            str(scripts / "04b_evaluate_results.py"),
            str(run_root),
            "--config",
            args.config,
            "--workers",
            str(args.validation_workers),
        ],
        cwd=root,
        check=False,
    )
    if evaluation.returncode != 0:
        return evaluation.returncode
    return subprocess.run(
        [
            sys.executable,
            str(scripts / "06_summarize_results.py"),
            str(run_root),
            "--config",
            args.config,
        ],
        cwd=root,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
