#!/usr/bin/env python3
"""Score one completed evaluator run from an eval spec and check results.

This CLI is the deterministic scoring boundary. A later Docker evaluator will
run the commands declared in ``eval.json`` and write the check-results JSON
consumed here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _common import load_config, write_json
from scoring import ScoringInputError, score_evaluator_result


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScoringInputError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScoringInputError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScoringInputError(f"{label} must contain a JSON object: {path}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-spec", type=Path, required=True)
    parser.add_argument("--check-results", type=Path, required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the score record to this JSON file instead of stdout",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, _ = load_config(args.config)
    scoring_config = config.get("scoring", {})
    eval_spec = read_json(args.eval_spec, "eval spec")
    check_results = read_json(args.check_results, "check results")

    try:
        score = score_evaluator_result(
            eval_spec,
            check_results,
            default_weights=scoring_config.get("default_weights"),
            default_core_failure_cap=float(
                scoring_config.get("core_failure_cap", 0.20)
            ),
            weight_tolerance=float(scoring_config.get("weight_tolerance", 1e-6)),
        )
    except ScoringInputError as exc:
        raise SystemExit(f"Scoring input error: {exc}") from exc

    if args.output:
        write_json(args.output, score)
    else:
        print(json.dumps(score, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
