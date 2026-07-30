from __future__ import annotations

import json
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from experiment import trajectory_metrics  # noqa: E402


evaluator = import_module("04b_evaluate_results")
summarizer = import_module("06_summarize_results")


class TrajectoryMetricsTests(unittest.TestCase):
    def test_normalizes_steps_tokens_cost_and_provider_identity(self) -> None:
        trajectory = {
            "info": {
                "model_stats": {
                    "api_calls": 2,
                    "instance_cost": 0.125,
                }
            },
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "bash"}}],
                    "extra": {
                        "response": {
                            "provider": "ProviderA",
                            "model": "model/version-a",
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 20,
                                "total_tokens": 120,
                                "cost": 0.05,
                                "prompt_tokens_details": {"cached_tokens": 10},
                                "completion_tokens_details": {
                                    "reasoning_tokens": 4
                                },
                            },
                        }
                    },
                },
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "bash"}}],
                    "extra": {
                        "response": {
                            "provider": "ProviderB",
                            "model": "model/version-a",
                            "usage": {
                                "prompt_tokens": 200,
                                "completion_tokens": 30,
                                "total_tokens": 230,
                                "cost": 0.075,
                            },
                        }
                    },
                },
            ],
        }
        metrics = trajectory_metrics(trajectory)
        self.assertEqual(metrics["steps_used"], 2)
        self.assertEqual(metrics["prompt_tokens"], 300)
        self.assertEqual(metrics["completion_tokens"], 50)
        self.assertEqual(metrics["total_tokens"], 350)
        self.assertEqual(metrics["cached_tokens"], 10)
        self.assertEqual(metrics["reasoning_tokens"], 4)
        self.assertEqual(metrics["cost_usd"], 0.125)
        self.assertTrue(metrics["cost_complete"])
        self.assertEqual(metrics["providers"], ["ProviderA", "ProviderB"])
        self.assertEqual(metrics["provider_models"], ["model/version-a"])


class EvaluatorPropagationTests(unittest.TestCase):
    def test_run_infrastructure_failure_remains_unscorable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_id = "ruff__ruff-example"
            task_dir = root / "tasks" / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "task.json").write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "base_commit": "a" * 40,
                        "test_commands": ["cargo test -p ruff_linter"],
                    }
                ),
                encoding="utf-8",
            )
            (task_dir / "tests.patch").write_text(
                "diff --git a/tests/a.rs b/tests/a.rs\n",
                encoding="utf-8",
            )
            destination = root / "results" / "model" / task_id
            destination.mkdir(parents=True)
            patch = destination / "patch.diff"
            patch.write_text("", encoding="utf-8")
            run_path = destination / "run.json"
            run_path.write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "model_id": "model",
                        "patch": str(patch.relative_to(root)),
                        "infrastructure_error": "provider unavailable",
                    }
                ),
                encoding="utf-8",
            )

            result = evaluator.evaluate_one(
                root=root,
                run_record_path=run_path,
                tasks_dir=root / "tasks",
                commands_by_id={task_id: ["cargo test -p ruff_linter"]},
                timeout=1,
            )
            self.assertFalse(result["scorable"])
            self.assertIsNone(result["score"])
            self.assertEqual(result["disposition"], "infrastructure_error")
            checks = json.loads(
                (destination / "checks.json").read_text(encoding="utf-8")
            )
            self.assertTrue(checks["infrastructure_failure"])


class SummaryTests(unittest.TestCase):
    def test_failure_categories_distinguish_limits_and_compile_failures(self) -> None:
        self.assertEqual(
            summarizer.failure_category(
                {"scorable": False, "infrastructure_error": "down"}
            ),
            "infrastructure_error",
        )
        self.assertEqual(
            summarizer.failure_category(
                {
                    "scorable": True,
                    "fully_resolved": False,
                    "has_submission": False,
                    "hit_step_limit": True,
                }
            ),
            "step_limit_no_patch",
        )
        self.assertEqual(
            summarizer.failure_category(
                {
                    "scorable": True,
                    "fully_resolved": False,
                    "has_submission": True,
                    "gate_failures": ["gate_check_failed:compile"],
                }
            ),
            "compilation_failure",
        )

    def test_summary_reports_cost_runtime_steps_and_score_buckets(self) -> None:
        records = [
            {
                "task_id": "one",
                "model_id": "model",
                "scorable": True,
                "score": 1.0,
                "fully_resolved": True,
                "cost_usd": 0.2,
                "cost_complete": True,
                "duration_seconds": 10,
                "evaluation_duration_seconds": 2,
                "steps_used": 5,
                "hit_step_limit": False,
                "timed_out": False,
                "has_submission": True,
                "group_scores": {"core": 1.0},
                "checks": [{"id": "compile", "status": "passed"}],
            },
            {
                "task_id": "two",
                "model_id": "model",
                "scorable": True,
                "score": 0.2,
                "fully_resolved": False,
                "cost_usd": 0.1,
                "cost_complete": True,
                "duration_seconds": 20,
                "evaluation_duration_seconds": 4,
                "steps_used": 10,
                "hit_step_limit": True,
                "timed_out": False,
                "has_submission": True,
                "group_scores": {"core": 0.0},
                "checks": [{"id": "compile", "status": "passed"}],
            },
        ]
        summary = summarizer.summarize_records(
            "model",
            records,
            root=Path("/tmp"),
            bootstrap_samples=100,
        )
        self.assertEqual(summary["mean_score"], 0.6)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["score_buckets"]["one"], 1)
        self.assertEqual(summary["score_buckets"]["between_0_and_0_5"], 1)
        self.assertEqual(summary["median_duration_seconds"], 15.0)
        self.assertEqual(summary["mean_steps"], 7.5)
        self.assertAlmostEqual(summary["total_cost_usd"], 0.3)
        self.assertEqual(summary["failure_categories"]["core_failure"], 1)


if __name__ == "__main__":
    unittest.main()
