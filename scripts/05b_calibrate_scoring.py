#!/usr/bin/env python3
"""Calibrate the Docker evaluator with empty, broken, and gold patches."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--run-id", default="scorer-calibration")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--keep-resources",
        action="store_true",
        help="Keep task images and Cargo volumes after calibration",
    )
    return parser.parse_args()


def calibration_run(
    *,
    root: Path,
    run_root: Path,
    task_id: str,
    variant: str,
    image: str,
    patch: str,
) -> Path:
    destination = run_root / f"calibration-{variant}" / task_id
    destination.mkdir(parents=True, exist_ok=True)
    patch_path = destination / "patch.diff"
    patch_path.write_text(patch, encoding="utf-8")
    volume = (
        f"mini-unsat-calibration-{variant}-{task_id}"
        .lower()
        .replace("_", "-")
        .replace(".", "-")
    )
    run = {
        "task_id": task_id,
        "model_id": f"calibration-{variant}",
        "image": image,
        "cargo_target_volume": volume,
        "patch": str(patch_path.relative_to(root)),
        "has_submission": bool(patch.strip()),
        "infrastructure_error": None,
        "duration_seconds": 0.0,
        "steps_used": 0,
        "cost_usd": 0.0,
        "cost_complete": True,
        "subsystem": "calibration",
        "exit_status": "Calibration",
    }
    run_path = destination / "run.json"
    write_json(run_path, run)
    return run_path


def main() -> int:
    args = parse_args()
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    config, root = load_config(args.config)
    paths = config["paths"]
    validation_config = config.get("validation", {})
    tasks_dir = resolve_path(paths["tasks_dir"], root)
    index = read_jsonl(
        resolve_path(paths.get("benchmark_index_file", "tasks/index.jsonl"), root)
    )
    available = {str(record["task_id"]): record for record in index}
    selected_ids = args.task_ids or [
        str(record["task_id"]) for record in index[: args.limit]
    ]
    missing = sorted(set(selected_ids) - set(available))
    if missing:
        raise SystemExit(f"Tasks are not in the frozen index: {missing}")
    selected_ids = selected_ids[: args.limit]

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
    run_root = (
        resolve_path(config.get("benchmark", {}).get("output_dir", "results/runs"), root)
        / args.run_id
    )
    evaluator = importlib.import_module("04b_evaluate_results")
    all_results: list[dict[str, Any]] = []
    prefix = str(
        validation_config.get("image_prefix", "mini-unsat-ruff-validation")
    )
    for task_id in selected_ids:
        task_dir = tasks_dir / task_id
        gold = (task_dir / "gold.patch").read_text(encoding="utf-8")
        image = f"{prefix}:{task_id.lower().replace('_', '-')}"
        paths_for_task = [
            calibration_run(
                root=root,
                run_root=run_root,
                task_id=task_id,
                variant="empty",
                image=image,
                patch="",
            ),
            calibration_run(
                root=root,
                run_root=run_root,
                task_id=task_id,
                variant="broken",
                image=image,
                patch="this is not a patch\n",
            ),
            calibration_run(
                root=root,
                run_root=run_root,
                task_id=task_id,
                variant="gold",
                image=image,
                patch=gold,
            ),
        ]
        results = evaluator.evaluate_task_runs(
            root=root,
            run_paths=paths_for_task,
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
        all_results.extend(results)
        by_variant = {
            str(result["model_id"]).removeprefix("calibration-"): result
            for result in results
        }
        expected = {"empty": 0.0, "broken": 0.0, "gold": 1.0}
        for variant, score in expected.items():
            observed = by_variant[variant].get("score")
            if observed != score:
                raise SystemExit(
                    f"{task_id}/{variant}: expected score {score}, observed {observed}"
                )
        print(f"[{task_id}] empty=0 broken=0 gold=1")

    write_json(
        run_root / "calibration-results.json",
        sorted(
            all_results,
            key=lambda item: (item["task_id"], item["model_id"]),
        ),
    )
    write_json(
        run_root / "calibration-summary.json",
        {
            "tasks": selected_ids,
            "task_count": len(selected_ids),
            "variants": ["empty", "broken", "gold"],
            "passed": True,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
