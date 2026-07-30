#!/usr/bin/env python3
"""Run dynamic-validation batches until exactly enough tasks have validated."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from _common import load_config, read_jsonl, resolve_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--target-valid", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=1)
    return parser.parse_args()


def saved_statuses(results_dir: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for path in results_dir.glob("*.json"):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = result.get("task_id")
        status = result.get("status")
        if isinstance(task_id, str) and isinstance(status, str):
            statuses[task_id] = status
    return statuses


def main() -> int:
    args = parse_args()
    if args.target_valid <= 0 or args.batch_size <= 0 or args.workers <= 0:
        raise SystemExit("target, batch size, and workers must be positive")

    config, root = load_config(args.config)
    results_dir = (
        resolve_path(
            config.get("validation", {}).get(
                "output_dir", ".cache/validation"
            ),
            root,
        )
        / "tasks"
    )
    pool_path = resolve_path(
        config.get("triage", {}).get(
            "dynamic_pool_file", config["paths"]["validation_queue_file"]
        ),
        root,
    )
    pool_ids = {
        str(record["task_id"]) for record in read_jsonl(pool_path)
    }
    validator = Path(__file__).with_name("03_validate_tasks.py")
    status_script = Path(__file__).with_name("00_status.py")

    while True:
        statuses = saved_statuses(results_dir)
        valid = sum(
            status == "validated"
            for task_id, status in statuses.items()
            if task_id in pool_ids
        )
        attempted = sum(task_id in pool_ids for task_id in statuses)
        remaining = len(pool_ids) - attempted
        print(
            f"progress: validated={valid}/{args.target_valid}, "
            f"attempted={attempted}, remaining={remaining}",
            flush=True,
        )
        if valid >= args.target_valid:
            return 0
        if remaining <= 0:
            raise SystemExit(
                f"Pool exhausted with only {valid} validated tasks"
            )

        batch_size = min(
            args.batch_size,
            remaining,
            # At least this many candidates are required. A small overshoot is
            # acceptable because the freezer deterministically takes the top 100.
            max(args.target_valid - valid, args.workers),
        )
        command = [
            sys.executable,
            str(validator),
            "--config",
            args.config,
            "--skip-existing",
            "--limit",
            str(batch_size),
            "--workers",
            str(args.workers),
            "--repeats",
            str(args.repeats),
        ]
        batch_started = time.monotonic()
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode != 0:
            return result.returncode
        print(
            f"batch wall time: {(time.monotonic() - batch_started) / 60:.1f} minutes",
            flush=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(status_script),
                "--config",
                args.config,
                "--target-valid",
                str(args.target_valid),
                "--workers",
                str(args.workers),
            ],
            cwd=root,
            check=False,
        )


if __name__ == "__main__":
    raise SystemExit(main())
