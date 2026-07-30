from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from scoring import ScoringInputError, score_evaluator_result  # noqa: E402


def eval_spec(weights: dict[str, float] | None = None) -> dict:
    return {
        "task_id": "ruff__ruff-example",
        "weights": weights
        or {"core": 0.60, "edge": 0.20, "regression": 0.15, "quality": 0.05},
        "core_failure_cap": 0.20,
        "gate_checks": [
            {"id": "compile", "command": "cargo check -p ruff_linter"}
        ],
        "checks": [
            {"id": "core-a", "group": "core", "command": "test core-a"},
            {"id": "core-b", "group": "core", "command": "test core-b"},
            {"id": "edge-a", "group": "edge", "command": "test edge-a"},
            {
                "id": "regression-a",
                "group": "regression",
                "command": "test regression-a",
            },
            {"id": "quality-a", "group": "quality", "command": "cargo fmt --check"},
        ],
    }


def result(statuses: dict[str, str] | None = None, **overrides: object) -> dict:
    statuses = statuses or {
        "compile": "passed",
        "core-a": "passed",
        "core-b": "passed",
        "edge-a": "passed",
        "regression-a": "passed",
        "quality-a": "passed",
    }
    value = {
        "task_id": "ruff__ruff-example",
        "patch_applied": True,
        "evaluator_tampered": False,
        "timed_out": False,
        "infrastructure_failure": False,
        "policy_violations": [],
        "checks": [
            {"id": check_id, "status": status, "runtime_seconds": 0.1}
            for check_id, status in statuses.items()
        ],
    }
    value.update(overrides)
    return value


class ScoringTests(unittest.TestCase):
    def test_fully_passing_solution_scores_one(self) -> None:
        scored = score_evaluator_result(eval_spec(), result())
        self.assertEqual(scored["score"], 1.0)
        self.assertTrue(scored["fully_resolved"])
        self.assertEqual(scored["disposition"], "scored")

    def test_partial_credit_is_by_semantic_check(self) -> None:
        statuses = {
            "compile": "passed",
            "core-a": "passed",
            "core-b": "failed",
            "edge-a": "passed",
            "regression-a": "passed",
            "quality-a": "failed",
        }
        scored = score_evaluator_result(eval_spec(), result(statuses))
        self.assertEqual(scored["score"], 0.65)
        self.assertEqual(scored["group_scores"]["core"], 0.5)
        self.assertFalse(scored["fully_resolved"])

    def test_zero_core_score_is_capped(self) -> None:
        statuses = {
            "compile": "passed",
            "core-a": "failed",
            "core-b": "failed",
            "edge-a": "passed",
            "regression-a": "passed",
            "quality-a": "passed",
        }
        scored = score_evaluator_result(eval_spec(), result(statuses))
        self.assertEqual(scored["raw_score"], 0.4)
        self.assertEqual(scored["score"], 0.2)
        self.assertEqual(scored["disposition"], "core_failure_capped")

    def test_failed_compile_gate_scores_zero(self) -> None:
        statuses = {
            check["id"]: "passed"
            for check in eval_spec()["checks"] + eval_spec()["gate_checks"]
        }
        statuses["compile"] = "failed"
        scored = score_evaluator_result(eval_spec(), result(statuses))
        self.assertEqual(scored["score"], 0.0)
        self.assertIn("gate_check_failed:compile", scored["gate_failures"])

    def test_timeout_and_tampering_score_zero(self) -> None:
        for override in ({"timed_out": True}, {"evaluator_tampered": True}):
            with self.subTest(override=override):
                scored = score_evaluator_result(eval_spec(), result(**override))
                self.assertEqual(scored["score"], 0.0)
                self.assertEqual(scored["disposition"], "gated_failure")

    def test_infrastructure_failure_is_not_a_model_failure(self) -> None:
        scored = score_evaluator_result(
            eval_spec(),
            result(
                infrastructure_failure=True,
                infrastructure_failure_reason="Docker daemon unavailable",
            ),
        )
        self.assertIsNone(scored["score"])
        self.assertFalse(scored["scorable"])
        self.assertEqual(scored["disposition"], "infrastructure_error")

    def test_missing_expected_check_counts_as_failed(self) -> None:
        statuses = {
            "compile": "passed",
            "core-a": "passed",
            "edge-a": "passed",
            "regression-a": "passed",
            "quality-a": "passed",
        }
        scored = score_evaluator_result(eval_spec(), result(statuses))
        self.assertEqual(scored["group_scores"]["core"], 0.5)
        missing = next(check for check in scored["checks"] if check["id"] == "core-b")
        self.assertEqual(missing["status"], "missing")

    def test_edge_weight_can_be_reallocated_to_core(self) -> None:
        spec = eval_spec(
            {"core": 0.80, "edge": 0.0, "regression": 0.15, "quality": 0.05}
        )
        spec["checks"] = [
            check for check in spec["checks"] if check["group"] != "edge"
        ]
        statuses = {
            "compile": "passed",
            "core-a": "passed",
            "core-b": "passed",
            "regression-a": "passed",
            "quality-a": "passed",
        }
        scored = score_evaluator_result(spec, result(statuses))
        self.assertEqual(scored["score"], 1.0)

    def test_invalid_weights_are_rejected(self) -> None:
        spec = eval_spec(
            {"core": 0.60, "edge": 0.20, "regression": 0.15, "quality": 0.15}
        )
        with self.assertRaisesRegex(ScoringInputError, "sum to 1"):
            score_evaluator_result(spec, result())

    def test_unexpected_result_check_is_rejected(self) -> None:
        value = result()
        value["checks"].append({"id": "unknown", "status": "passed"})
        with self.assertRaisesRegex(ScoringInputError, "unexpected check ids"):
            score_evaluator_result(eval_spec(), value)


if __name__ == "__main__":
    unittest.main()
