"""Deterministic scoring for validated coding-agent task results.

This module intentionally does not run Docker or tests. The validation and
evaluation stages produce an evaluator result containing check statuses; this
module validates that result against a task's evaluator-only ``eval.json`` and
maps it to a score in [0, 1].
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


GROUPS = ("core", "edge", "regression", "quality")
PASSING_STATUS = "passed"
VALID_STATUSES = frozenset({"passed", "failed", "error", "skipped"})
DEFAULT_WEIGHTS = {
    "core": 0.60,
    "edge": 0.20,
    "regression": 0.15,
    "quality": 0.05,
}


class ScoringInputError(ValueError):
    """Raised when an eval specification or evaluator result is malformed."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScoringInputError(f"{label} must be an object")
    return value


def _records(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ScoringInputError(f"{label} must be a list")
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        records.append(_mapping(item, f"{label}[{index}]"))
    return records


def _identifier(record: Mapping[str, Any], label: str) -> str:
    value = record.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ScoringInputError(f"{label}.id must be a non-empty string")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoringInputError(f"{label} must be a number")
    return float(value)


def _boolean(record: Mapping[str, Any], key: str, default: bool) -> bool:
    value = record.get(key, default)
    if not isinstance(value, bool):
        raise ScoringInputError(f"evaluator_result.{key} must be a boolean")
    return value


def validate_eval_spec(
    eval_spec: Mapping[str, Any],
    *,
    default_weights: Mapping[str, float] | None = None,
    default_core_failure_cap: float = 0.20,
    weight_tolerance: float = 1e-6,
) -> dict[str, Any]:
    """Validate and normalize an evaluator-only task specification."""

    spec = _mapping(eval_spec, "eval_spec")
    task_id = spec.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ScoringInputError("eval_spec.task_id must be a non-empty string")

    supplied_weights = _mapping(
        spec.get("weights", default_weights or DEFAULT_WEIGHTS),
        "eval_spec.weights",
    )
    unknown_groups = set(supplied_weights) - set(GROUPS)
    if unknown_groups:
        raise ScoringInputError(
            f"eval_spec.weights contains unknown groups: {sorted(unknown_groups)}"
        )

    weights: dict[str, float] = {}
    for group in GROUPS:
        if group not in supplied_weights:
            raise ScoringInputError(f"eval_spec.weights is missing {group!r}")
        weight = _number(supplied_weights[group], f"eval_spec.weights.{group}")
        if not 0 <= weight <= 1:
            raise ScoringInputError(
                f"eval_spec.weights.{group} must be between 0 and 1"
            )
        weights[group] = weight
    if abs(sum(weights.values()) - 1.0) > weight_tolerance:
        raise ScoringInputError("eval_spec weights must sum to 1")

    cap = _number(
        spec.get("core_failure_cap", default_core_failure_cap),
        "eval_spec.core_failure_cap",
    )
    if not 0 <= cap <= 1:
        raise ScoringInputError("eval_spec.core_failure_cap must be between 0 and 1")

    checks = _records(spec.get("checks"), "eval_spec.checks")
    gate_checks = _records(spec.get("gate_checks", []), "eval_spec.gate_checks")
    seen_ids: set[str] = set()
    normalized_checks: list[dict[str, Any]] = []
    normalized_gates: list[dict[str, Any]] = []
    group_counts = {group: 0 for group in GROUPS}

    for index, check in enumerate(checks):
        label = f"eval_spec.checks[{index}]"
        check_id = _identifier(check, label)
        if check_id in seen_ids:
            raise ScoringInputError(f"duplicate check id: {check_id}")
        seen_ids.add(check_id)
        group = check.get("group")
        if group not in GROUPS:
            raise ScoringInputError(f"{label}.group must be one of {GROUPS}")
        command = check.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ScoringInputError(f"{label}.command must be a non-empty string")
        group_counts[group] += 1
        normalized_checks.append(
            {
                "id": check_id,
                "group": group,
                "command": command,
                "description": check.get("description"),
            }
        )

    if group_counts["core"] == 0:
        raise ScoringInputError("eval_spec must define at least one core check")
    for group, weight in weights.items():
        if weight > 0 and group_counts[group] == 0:
            raise ScoringInputError(
                f"weighted group {group!r} must define at least one check"
            )

    for index, check in enumerate(gate_checks):
        label = f"eval_spec.gate_checks[{index}]"
        check_id = _identifier(check, label)
        if check_id in seen_ids:
            raise ScoringInputError(f"duplicate check id: {check_id}")
        seen_ids.add(check_id)
        command = check.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ScoringInputError(f"{label}.command must be a non-empty string")
        normalized_gates.append(
            {
                "id": check_id,
                "command": command,
                "description": check.get("description"),
            }
        )

    if not normalized_gates:
        raise ScoringInputError(
            "eval_spec must define at least one gate check for compilation"
        )

    return {
        "task_id": task_id,
        "weights": weights,
        "core_failure_cap": cap,
        "checks": normalized_checks,
        "gate_checks": normalized_gates,
        "group_counts": group_counts,
    }


def _normalize_result_checks(
    evaluator_result: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    checks = _records(evaluator_result.get("checks", []), "evaluator_result.checks")
    normalized: dict[str, dict[str, Any]] = {}
    for index, check in enumerate(checks):
        label = f"evaluator_result.checks[{index}]"
        check_id = _identifier(check, label)
        if check_id in normalized:
            raise ScoringInputError(f"duplicate evaluator result check id: {check_id}")
        status = check.get("status")
        if status not in VALID_STATUSES:
            raise ScoringInputError(
                f"{label}.status must be one of {sorted(VALID_STATUSES)}"
            )
        runtime = check.get("runtime_seconds")
        if runtime is not None:
            runtime = _number(runtime, f"{label}.runtime_seconds")
            if runtime < 0:
                raise ScoringInputError(
                    f"{label}.runtime_seconds must not be negative"
                )
        failure_reason = check.get("failure_reason")
        if failure_reason is not None and not isinstance(failure_reason, str):
            raise ScoringInputError(f"{label}.failure_reason must be a string or null")
        normalized[check_id] = {
            "status": status,
            "runtime_seconds": runtime,
            "failure_reason": failure_reason,
        }
    return normalized


def score_evaluator_result(
    eval_spec: Mapping[str, Any],
    evaluator_result: Mapping[str, Any],
    *,
    default_weights: Mapping[str, float] | None = None,
    default_core_failure_cap: float = 0.20,
    weight_tolerance: float = 1e-6,
) -> dict[str, Any]:
    """Score one evaluator result and return a machine-readable breakdown."""

    spec = validate_eval_spec(
        eval_spec,
        default_weights=default_weights,
        default_core_failure_cap=default_core_failure_cap,
        weight_tolerance=weight_tolerance,
    )
    result = _mapping(evaluator_result, "evaluator_result")
    result_task_id = result.get("task_id")
    if result_task_id != spec["task_id"]:
        raise ScoringInputError(
            "evaluator_result.task_id does not match eval_spec.task_id"
        )

    infrastructure_failure = _boolean(result, "infrastructure_failure", False)
    if infrastructure_failure:
        reason = result.get("infrastructure_failure_reason")
        if reason is not None and not isinstance(reason, str):
            raise ScoringInputError(
                "evaluator_result.infrastructure_failure_reason must be a string or null"
            )
        return {
            "task_id": spec["task_id"],
            "score": None,
            "scorable": False,
            "fully_resolved": False,
            "disposition": "infrastructure_error",
            "gate_failures": [],
            "group_scores": {},
            "checks": [],
            "infrastructure_failure_reason": reason,
        }

    result_checks = _normalize_result_checks(result)
    expected_ids = {
        check["id"] for check in spec["checks"] + spec["gate_checks"]
    }
    unexpected_ids = sorted(set(result_checks) - expected_ids)
    if unexpected_ids:
        raise ScoringInputError(
            f"evaluator_result contains unexpected check ids: {unexpected_ids}"
        )

    gate_failures: list[str] = []
    if not _boolean(result, "patch_applied", True):
        gate_failures.append("patch_not_applied")
    if _boolean(result, "evaluator_tampered", False):
        gate_failures.append("evaluator_tampered")
    if _boolean(result, "timed_out", False):
        gate_failures.append("task_timeout")

    policy_violations = result.get("policy_violations", [])
    if not isinstance(policy_violations, list) or not all(
        isinstance(value, str) for value in policy_violations
    ):
        raise ScoringInputError(
            "evaluator_result.policy_violations must be a list of strings"
        )
    if policy_violations:
        gate_failures.append("policy_violation")

    breakdown: list[dict[str, Any]] = []
    for gate in spec["gate_checks"]:
        observed = result_checks.get(gate["id"])
        status = observed["status"] if observed else "missing"
        if status != PASSING_STATUS:
            gate_failures.append(f"gate_check_failed:{gate['id']}")
        breakdown.append(
            {
                **gate,
                "kind": "gate",
                "group": None,
                "effective_weight": 0.0,
                "status": status,
                "runtime_seconds": observed["runtime_seconds"] if observed else None,
                "failure_reason": (
                    observed["failure_reason"] if observed else "result missing"
                ),
            }
        )

    group_passes = {group: 0 for group in GROUPS}
    for check in spec["checks"]:
        observed = result_checks.get(check["id"])
        status = observed["status"] if observed else "missing"
        if status == PASSING_STATUS:
            group_passes[check["group"]] += 1
        count = spec["group_counts"][check["group"]]
        breakdown.append(
            {
                **check,
                "kind": "scored",
                "effective_weight": spec["weights"][check["group"]] / count,
                "status": status,
                "runtime_seconds": observed["runtime_seconds"] if observed else None,
                "failure_reason": (
                    observed["failure_reason"] if observed else "result missing"
                ),
            }
        )

    group_scores = {
        group: (
            group_passes[group] / spec["group_counts"][group]
            if spec["group_counts"][group]
            else 0.0
        )
        for group in GROUPS
    }
    raw_score = sum(
        spec["weights"][group] * group_scores[group] for group in GROUPS
    )

    if gate_failures:
        score = 0.0
        disposition = "gated_failure"
    elif group_scores["core"] == 0:
        score = min(raw_score, spec["core_failure_cap"])
        disposition = "core_failure_capped"
    else:
        score = raw_score
        disposition = "scored"

    score = round(max(0.0, min(1.0, score)), 12)
    raw_score = round(max(0.0, min(1.0, raw_score)), 12)
    return {
        "task_id": spec["task_id"],
        "score": score,
        "raw_score": raw_score,
        "scorable": True,
        "fully_resolved": score == 1.0,
        "disposition": disposition,
        "gate_failures": sorted(set(gate_failures)),
        "group_scores": group_scores,
        "group_weights": spec["weights"],
        "checks": breakdown,
        "policy_violations": policy_violations,
    }
