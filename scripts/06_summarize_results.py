#!/usr/bin/env python3
"""Aggregate task-level experiment artifacts into model comparisons."""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from _common import load_config, resolve_path, write_json


DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    return parser.parse_args()


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    index = (len(sorted_values) - 1) * probability
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = index - lower
    return (
        sorted_values[lower] * (1 - fraction)
        + sorted_values[upper] * fraction
    )


def bootstrap_mean_ci(
    values: list[float], samples: int, seed: int
) -> list[float] | None:
    if not values:
        return None
    randomizer = random.Random(seed)
    estimates = sorted(
        statistics.fmean(randomizer.choice(values) for _ in values)
        for _ in range(samples)
    )
    return [
        round(percentile(estimates, 0.025), 4),
        round(percentile(estimates, 0.975), 4),
    ]


def failure_category(record: dict[str, Any]) -> str:
    if not record.get("scorable", False):
        return "infrastructure_error"
    if record.get("fully_resolved"):
        return "resolved"
    if record.get("timed_out") or record.get("hit_wall_time_limit"):
        return "timeout"
    if not record.get("has_submission"):
        if record.get("hit_step_limit"):
            return "step_limit_no_patch"
        if record.get("hit_cost_limit"):
            return "cost_limit_no_patch"
        return "no_patch"
    gate_failures = set(record.get("gate_failures") or [])
    if "patch_not_applied" in gate_failures:
        return "patch_not_applied"
    if "policy_violation" in gate_failures or record.get("policy_violations"):
        return "policy_violation"
    if any(str(value).startswith("gate_check_failed:compile") for value in gate_failures):
        return "compilation_failure"
    core = record.get("group_scores", {}).get("core")
    if core == 0:
        return "core_failure"
    if isinstance(record.get("score"), (int, float)) and record["score"] > 0:
        return "partial_solution"
    return "test_failure"


def patch_metrics(root: Path, record: dict[str, Any]) -> dict[str, int]:
    patch_value = record.get("patch")
    if not isinstance(patch_value, str):
        return {"files": 0, "added_lines": 0, "deleted_lines": 0}
    path = root / patch_value
    if not path.exists():
        return {"files": 0, "added_lines": 0, "deleted_lines": 0}
    patch = path.read_text(encoding="utf-8")
    added = sum(
        line.startswith("+") and not line.startswith("+++")
        for line in patch.splitlines()
    )
    deleted = sum(
        line.startswith("-") and not line.startswith("---")
        for line in patch.splitlines()
    )
    return {
        "files": len({match.group(2) for match in DIFF_PATH_RE.finditer(patch)}),
        "added_lines": added,
        "deleted_lines": deleted,
    }


def cost(record: dict[str, Any]) -> float:
    value = record.get("cost_usd")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    fallback = record.get("model_stats", {}).get("instance_cost", 0.0)
    return (
        float(fallback)
        if isinstance(fallback, (int, float)) and not isinstance(fallback, bool)
        else 0.0
    )


def summarize_records(
    model_id: str,
    records: list[dict[str, Any]],
    *,
    root: Path,
    bootstrap_samples: int,
) -> dict[str, Any]:
    scorable = [record for record in records if record.get("scorable", False)]
    scores = [float(record["score"]) for record in scorable]
    resolved = sum(bool(record.get("fully_resolved")) for record in scorable)
    costs = [cost(record) for record in records]
    durations = [
        float(record.get("duration_seconds", 0.0)) for record in records
    ]
    evaluation_durations = [
        float(record["evaluation_duration_seconds"])
        for record in records
        if isinstance(record.get("evaluation_duration_seconds"), (int, float))
    ]
    steps = [
        float(record.get("steps_used", 0))
        for record in records
        if isinstance(record.get("steps_used"), (int, float))
    ]
    patches = [patch_metrics(root, record) for record in records]
    failure_counts = Counter(failure_category(record) for record in records)
    exit_statuses = Counter(
        str(record.get("exit_status") or "missing") for record in records
    )
    group_values: dict[str, list[float]] = defaultdict(list)
    for record in scorable:
        for group, value in record.get("group_scores", {}).items():
            if isinstance(value, (int, float)):
                group_values[str(group)].append(float(value))

    compile_passed = 0
    compile_observed = 0
    for record in scorable:
        for check in record.get("checks", []):
            if check.get("id") == "compile":
                compile_observed += 1
                compile_passed += check.get("status") == "passed"
                break

    score_buckets = {
        "zero": sum(value == 0 for value in scores),
        "between_0_and_0_5": sum(0 < value < 0.5 for value in scores),
        "between_0_5_and_1": sum(0.5 <= value < 1 for value in scores),
        "one": sum(value == 1 for value in scores),
    }
    total_cost = sum(costs)
    total_score = sum(scores)
    return {
        "model_id": model_id,
        "attempted": len(records),
        "scorable": len(scorable),
        "infrastructure_errors": len(records) - len(scorable),
        "mean_score": rounded(mean(scores)),
        "median_score": rounded(median(scores)),
        "mean_score_95_ci": bootstrap_mean_ci(
            scores, bootstrap_samples, seed=42
        ),
        "resolved": resolved,
        "resolution_rate": rounded(resolved / len(scorable))
        if scorable
        else None,
        "partial_resolution_rate": rounded(
            sum(0 < value < 1 for value in scores) / len(scorable)
        )
        if scorable
        else None,
        "zero_score_rate": rounded(
            sum(value == 0 for value in scores) / len(scorable)
        )
        if scorable
        else None,
        "score_buckets": score_buckets,
        "group_mean_scores": {
            group: rounded(mean(values))
            for group, values in sorted(group_values.items())
        },
        "compile_success_rate": rounded(compile_passed / compile_observed)
        if compile_observed
        else None,
        "total_cost_usd": round(total_cost, 6),
        "mean_cost_usd": rounded(mean(costs), 6),
        "cost_per_resolved_usd": rounded(total_cost / resolved, 6)
        if resolved
        else None,
        "score_per_dollar": rounded(total_score / total_cost, 4)
        if total_cost
        else None,
        "cost_complete_runs": sum(
            bool(record.get("cost_complete")) for record in records
        ),
        "total_duration_seconds": rounded(sum(durations), 3),
        "mean_duration_seconds": rounded(mean(durations), 3),
        "median_duration_seconds": rounded(median(durations), 3),
        "mean_evaluation_duration_seconds": rounded(
            mean(evaluation_durations), 3
        ),
        "median_evaluation_duration_seconds": rounded(
            median(evaluation_durations), 3
        ),
        "mean_steps": rounded(mean(steps), 2),
        "median_steps": rounded(median(steps), 2),
        "step_limit_rate": rounded(
            sum(bool(record.get("hit_step_limit")) for record in records)
            / len(records)
        )
        if records
        else None,
        "timeout_rate": rounded(
            sum(
                bool(record.get("timed_out"))
                or bool(record.get("hit_wall_time_limit"))
                for record in records
            )
            / len(records)
        )
        if records
        else None,
        "submission_rate": rounded(
            sum(bool(record.get("has_submission")) for record in records)
            / len(records)
        )
        if records
        else None,
        "total_prompt_tokens": sum(
            int(record.get("prompt_tokens", 0)) for record in records
        ),
        "total_completion_tokens": sum(
            int(record.get("completion_tokens", 0)) for record in records
        ),
        "total_reasoning_tokens": sum(
            int(record.get("reasoning_tokens", 0)) for record in records
        ),
        "mean_patch_files": rounded(
            mean([float(item["files"]) for item in patches]), 2
        ),
        "mean_patch_added_lines": rounded(
            mean([float(item["added_lines"]) for item in patches]), 2
        ),
        "mean_patch_deleted_lines": rounded(
            mean([float(item["deleted_lines"]) for item in patches]), 2
        ),
        "failure_categories": dict(sorted(failure_counts.items())),
        "exit_statuses": dict(sorted(exit_statuses.items())),
    }


def pairwise_comparisons(
    grouped: dict[str, list[dict[str, Any]]],
    samples: int,
) -> list[dict[str, Any]]:
    by_model_task = {
        model: {
            str(record["task_id"]): float(record["score"])
            for record in records
            if record.get("scorable", False)
        }
        for model, records in grouped.items()
    }
    models = sorted(by_model_task)
    comparisons: list[dict[str, Any]] = []
    for index, model_a in enumerate(models):
        for model_b in models[index + 1 :]:
            common = sorted(
                set(by_model_task[model_a]) & set(by_model_task[model_b])
            )
            differences = [
                by_model_task[model_a][task] - by_model_task[model_b][task]
                for task in common
            ]
            comparisons.append(
                {
                    "model_a": model_a,
                    "model_b": model_b,
                    "paired_tasks": len(common),
                    "mean_score_difference_a_minus_b": rounded(
                        mean(differences)
                    ),
                    "difference_95_ci": bootstrap_mean_ci(
                        differences, samples, seed=42
                    ),
                    "model_a_wins": sum(value > 0 for value in differences),
                    "ties": sum(value == 0 for value in differences),
                    "model_b_wins": sum(value < 0 for value in differences),
                }
            )
    return comparisons


def main() -> int:
    args = parse_args()
    _, root = load_config(args.config)
    run_dir = resolve_path(args.run_dir, root)
    scores_path = run_dir / "scores.json"
    records = json.loads(scores_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit(f"{scores_path} must contain a list")
    if args.bootstrap_samples <= 0:
        raise SystemExit("--bootstrap-samples must be positive")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["model_id"])].append(record)
    summary = [
        summarize_records(
            model_id,
            model_records,
            root=root,
            bootstrap_samples=args.bootstrap_samples,
        )
        for model_id, model_records in sorted(grouped.items())
    ]
    write_json(run_dir / "summary.json", summary)

    subsystem_summary: list[dict[str, Any]] = []
    for model_id, model_records in sorted(grouped.items()):
        by_subsystem: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in model_records:
            by_subsystem[str(record.get("subsystem") or "unknown")].append(
                record
            )
        for subsystem, subsystem_records in sorted(by_subsystem.items()):
            scorable = [
                record
                for record in subsystem_records
                if record.get("scorable", False)
            ]
            values = [float(record["score"]) for record in scorable]
            resolved = sum(
                bool(record.get("fully_resolved")) for record in scorable
            )
            subsystem_summary.append(
                {
                    "model_id": model_id,
                    "subsystem": subsystem,
                    "attempted": len(subsystem_records),
                    "scorable": len(scorable),
                    "mean_score": rounded(mean(values)),
                    "resolved": resolved,
                    "resolution_rate": rounded(resolved / len(scorable))
                    if scorable
                    else None,
                }
            )
    write_json(run_dir / "subsystem-summary.json", subsystem_summary)
    write_json(
        run_dir / "pairwise-comparisons.json",
        pairwise_comparisons(grouped, args.bootstrap_samples),
    )

    header = (
        "| Model | Scorable | Mean score (95% CI) | Resolved | Partial | "
        "Median time | Mean steps | Cost | Cost/resolved |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = [header]
    for record in summary:
        ci = record["mean_score_95_ci"]
        mean_with_ci = (
            f"{record['mean_score']:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"
            if record["mean_score"] is not None and ci
            else "n/a"
        )
        rate = (
            f"{100 * record['resolution_rate']:.1f}%"
            if record["resolution_rate"] is not None
            else "n/a"
        )
        partial = (
            f"{100 * record['partial_resolution_rate']:.1f}%"
            if record["partial_resolution_rate"] is not None
            else "n/a"
        )
        median_time = (
            f"{record['median_duration_seconds']:.1f}s"
            if record["median_duration_seconds"] is not None
            else "n/a"
        )
        mean_steps = (
            f"{record['mean_steps']:.1f}"
            if record["mean_steps"] is not None
            else "n/a"
        )
        cost_per_resolved = (
            f"${record['cost_per_resolved_usd']:.3f}"
            if record["cost_per_resolved_usd"] is not None
            else "n/a"
        )
        rows.append(
            f"| {record['model_id']} | {record['scorable']} | "
            f"{mean_with_ci} | {record['resolved']} ({rate}) | {partial} | "
            f"{median_time} | {mean_steps} | "
            f"${record['total_cost_usd']:.3f} | {cost_per_resolved} |"
        )
    table = "\n".join(rows) + "\n"
    (run_dir / "summary.md").write_text(table, encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
