#!/usr/bin/env python3
"""Freeze exactly 100 validated tasks and a deterministic reserve list."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--target", type=int)
    parser.add_argument("--reserves", type=int)
    parser.add_argument(
        "--provisional",
        action="store_true",
        help="Freeze the ranked static pool before dynamic validation (development only)",
    )
    return parser.parse_args()


def validation_statuses(directory: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for path in sorted((directory / "tasks").glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        statuses[str(result.get("task_id"))] = str(result.get("status"))
    return statuses


def frozen_record(
    record: dict[str, Any], rank: int, status: str, split: str
) -> dict[str, Any]:
    return {
        "task_id": record["task_id"],
        "base_commit": record["base_commit"],
        "subsystem": record["subsystem"],
        "priority_score": record["priority_score"],
        "test_commands": record["suggested_test_commands"],
        "validation_status": status,
        "selection_rank": rank,
        "split": split,
    }


def main() -> int:
    args = parse_args()
    config, root = load_config(args.config)
    triage = config.get("triage", {})
    paths = config["paths"]
    target = args.target or int(triage.get("target_tasks", 100))
    reserves = (
        args.reserves
        if args.reserves is not None
        else int(triage.get("reserve_tasks", 0))
    )
    if target != 100:
        raise SystemExit(
            "The assessment requires exactly 100 final tasks; use --target 100"
        )
    if reserves < 0:
        raise SystemExit("--reserves must not be negative")

    pool = read_jsonl(
        resolve_path(
            triage.get("dynamic_pool_file", paths["validation_queue_file"]),
            root,
        )
    )
    validation_dir = resolve_path(
        config.get("validation", {}).get("output_dir", ".cache/validation"),
        root,
    )
    statuses = validation_statuses(validation_dir)
    if args.provisional:
        eligible = [
            (record, statuses.get(str(record["task_id"]), "not_run"))
            for record in pool
            if statuses.get(str(record["task_id"])) != "rejected"
        ]
    else:
        eligible = [
            (record, "validated")
            for record in pool
            if statuses.get(str(record["task_id"])) == "validated"
        ]
    required = target + reserves
    if len(eligible) < required:
        raise SystemExit(
            f"Only {len(eligible)} eligible tasks are available; need {required}. "
            "Continue dynamic validation or request fewer reserves."
        )

    primary = [
        frozen_record(record, rank, status, "benchmark")
        for rank, (record, status) in enumerate(eligible[:target], 1)
    ]
    reserve_records = [
        frozen_record(record, rank, status, "reserve")
        for rank, (record, status) in enumerate(
            eligible[target : target + reserves], 1
        )
    ]
    index_path = resolve_path(paths["benchmark_index_file"], root)
    reserves_path = resolve_path(paths["reserve_index_file"], root)
    write_jsonl(index_path, primary)
    write_jsonl(reserves_path, reserve_records)
    print(
        json.dumps(
            {
                "benchmark_tasks": len(primary),
                "reserves": len(reserve_records),
                "provisional": args.provisional,
                "index": str(index_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
