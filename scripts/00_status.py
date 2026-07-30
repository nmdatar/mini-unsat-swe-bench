#!/usr/bin/env python3
"""Report validation throughput and an evidence-based completion ETA."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--target-valid", type=int, default=100)
    parser.add_argument("--workers", type=int, default=2)
    return parser.parse_args()


def command_seconds(result: dict[str, Any]) -> float:
    return sum(
        float(command.get("duration_seconds", 0.0))
        for phase in result.get("phase_results", [])
        for command in phase.get("commands", [])
    )


def main() -> int:
    args = parse_args()
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
    pool = read_jsonl(
        resolve_path(
            config.get("triage", {}).get(
                "dynamic_pool_file", config["paths"]["validation_queue_file"]
            ),
            root,
        )
    )
    pool_ids = {str(record["task_id"]) for record in pool}
    results: list[dict[str, Any]] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if result.get("task_id") in pool_ids:
            results.append(result)

    validated = sum(result.get("status") == "validated" for result in results)
    rejected = sum(result.get("status") == "rejected" for result in results)
    infrastructure = sum(
        result.get("status") == "infrastructure_error" for result in results
    )
    attempted = validated + rejected
    acceptance_rate = validated / attempted if attempted else None
    durations = [
        command_seconds(result)
        for result in results
        if command_seconds(result) > 0
    ]
    mean_seconds = statistics.fmean(durations) if durations else None
    remaining_valid = max(0, args.target_valid - validated)
    if acceptance_rate and mean_seconds:
        expected_candidates = math.ceil(remaining_valid / acceptance_rate)
        eta_hours = expected_candidates * mean_seconds / args.workers / 3600
    else:
        expected_candidates = None
        eta_hours = None

    report = {
        "target_valid": args.target_valid,
        "pool_size": len(pool_ids),
        "attempted_scorable": attempted,
        "validated": validated,
        "rejected": rejected,
        "infrastructure_errors": infrastructure,
        "remaining_pool": len(pool_ids) - len(results),
        "acceptance_rate": round(acceptance_rate, 4)
        if acceptance_rate is not None
        else None,
        "mean_candidate_test_seconds": round(mean_seconds, 1)
        if mean_seconds is not None
        else None,
        "workers": args.workers,
        "expected_more_candidates": expected_candidates,
        "estimated_validation_hours": round(eta_hours, 2)
        if eta_hours is not None
        else None,
        "note": (
            "ETA uses completed test-command time and excludes image-build "
            "variance; allow a 20-30% margin."
        ),
    }
    write_json(
        resolve_path(
            config.get("validation", {}).get(
                "output_dir", ".cache/validation"
            ),
            root,
        )
        / "progress.json",
        report,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
