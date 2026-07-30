#!/usr/bin/env python3
"""Rank generated candidates for fast dynamic validation.

The triage is read-only with respect to ``tasks/``. It inspects patch structure,
estimates whether hidden tests are executable before the gold patch, proposes a
focused Rust test command, and writes an auditable ranked pool under
``.cache/triage``.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _common import load_config, read_jsonl, resolve_path, write_json, write_jsonl


DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
TEST_ATTRIBUTE_RE = re.compile(r"^\+\s*#\[(?:test|test_case)(?:\]|\()", re.M)
TEST_FUNCTION_RE = re.compile(
    r"^\+\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
RULE_CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{1,6}\d{3,4})(?![A-Z0-9])")


@dataclass
class PatchFile:
    path: str
    new_file: bool = False
    added_lines: list[str] = field(default_factory=list)


def parse_patch(text: str) -> list[PatchFile]:
    files: list[PatchFile] = []
    current: PatchFile | None = None
    for line in text.splitlines():
        header = DIFF_HEADER_RE.match(line)
        if header:
            current = PatchFile(path=header.group(2))
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("new file mode "):
            current.new_file = True
        elif line.startswith("+") and not line.startswith("+++"):
            current.added_lines.append(line[1:])
    return files


def is_direct_rust_test(path: str) -> bool:
    return path.endswith(".rs") and (
        "/tests/" in path
        or path.endswith("_test.rs")
        or path.endswith("/tests.rs")
        or "/test/" in path
    )


def is_snapshot(path: str) -> bool:
    return path.endswith(".snap")


def is_fixture(path: str) -> bool:
    return any(marker in path for marker in ("/fixture/", "/fixtures/", "/resources/test/"))


def added_test_functions(files: list[PatchFile]) -> list[str]:
    names: list[str] = []
    for patch_file in files:
        if not patch_file.path.endswith(".rs"):
            continue
        added = "\n".join(f"+{line}" for line in patch_file.added_lines)
        if TEST_ATTRIBUTE_RE.search(added):
            names.extend(TEST_FUNCTION_RE.findall(added))
    return sorted(set(names))


def contains_test_registration(files: list[PatchFile]) -> bool:
    for patch_file in files:
        if not patch_file.path.endswith(".rs"):
            continue
        added = "\n".join(f"+{line}" for line in patch_file.added_lines)
        if TEST_ATTRIBUTE_RE.search(added):
            return True
        if any(
            "#[test_case" in line or "test_case(" in line
            for line in patch_file.added_lines
        ):
            return True
    return False


def linter_module(paths: list[str]) -> str | None:
    for path in paths:
        match = re.search(r"crates/ruff_linter/src/rules/([^/]+)/", path)
        if match:
            return match.group(1)
        match = re.search(
            r"crates/ruff_linter/resources/test/fixtures/([^/]+)/",
            path,
        )
        if match:
            return match.group(1)
    return None


def suggest_commands(
    subsystem: str,
    test_files: list[PatchFile],
    function_names: list[str],
) -> list[str]:
    paths = [patch_file.path for patch_file in test_files]
    filters = function_names[:3]
    commands: list[str] = []

    if any(path.startswith("crates/ruff_server/tests/e2e/") for path in paths):
        server_files = [
            patch_file
            for patch_file in test_files
            if patch_file.path.startswith("crates/ruff_server/tests/e2e/")
        ]
        server_functions = added_test_functions(server_files)
        modules = sorted(
            {
                Path(path).stem
                for path in paths
                if path.startswith("crates/ruff_server/tests/e2e/")
                and path.endswith(".rs")
                and Path(path).stem != "main"
            }
        )
        test_filter = (
            server_functions[0]
            if server_functions
            else (f"{modules[0]}::" if modules else "")
        )
        command = "cargo test -p ruff_server --test e2e"
        commands.append(f"{command} {test_filter}".strip())

    if any(path.startswith("crates/ruff/tests/cli/") for path in paths):
        cli_files = [
            patch_file
            for patch_file in test_files
            if patch_file.path.startswith("crates/ruff/tests/cli/")
        ]
        cli_functions = added_test_functions(cli_files)
        modules = sorted(
            {
                (
                    Path(path).stem
                    if path.endswith(".rs")
                    else re.search(r"(?:^|/)cli__([^_]+)__", path).group(1)
                )
                for path in paths
                if path.startswith("crates/ruff/tests/cli/")
                and (
                    (path.endswith(".rs") and Path(path).stem != "main")
                    or re.search(r"(?:^|/)cli__([^_]+)__", path)
                )
            }
        )
        test_filter = (
            cli_functions[0]
            if cli_functions
            else (f"{modules[0]}::" if modules else "")
        )
        command = "cargo test -p ruff --test cli"
        commands.append(f"{command} {test_filter}".strip())

    if any(path == "crates/ruff/tests/integration_test.rs" for path in paths):
        test_filter = filters[0] if filters else ""
        commands.append(
            f"cargo test -p ruff --test integration_test {test_filter}".strip()
        )

    direct_ruff_tests = sorted(
        {
            Path(path).stem
            for path in paths
            if re.fullmatch(r"crates/ruff/tests/[^/]+\.rs", path)
            and Path(path).stem != "integration_test"
        }
    )
    for test_target in direct_ruff_tests:
        target_functions = added_test_functions(
            [
                patch_file
                for patch_file in test_files
                if patch_file.path == f"crates/ruff/tests/{test_target}.rs"
            ]
        )
        test_filter = target_functions[0] if target_functions else ""
        commands.append(
            f"cargo test -p ruff --test {test_target} {test_filter}".strip()
        )

    if any(path.startswith("crates/ruff_python_formatter/") for path in paths):
        formatter_functions = added_test_functions(
            [
                patch_file
                for patch_file in test_files
                if patch_file.path.startswith("crates/ruff_python_formatter/")
            ]
        )
        test_filter = formatter_functions[0] if formatter_functions else ""
        commands.append(
            f"cargo test -p ruff_python_formatter {test_filter}".strip()
        )

    if any(path.startswith("crates/ruff_python_parser/") for path in paths):
        parser_functions = added_test_functions(
            [
                patch_file
                for patch_file in test_files
                if patch_file.path.startswith("crates/ruff_python_parser/")
            ]
        )
        test_filter = parser_functions[0] if parser_functions else ""
        commands.append(
            f"cargo test -p ruff_python_parser {test_filter}".strip()
        )

    if any(path.startswith("crates/ruff_workspace/") for path in paths):
        workspace_functions = added_test_functions(
            [
                patch_file
                for patch_file in test_files
                if patch_file.path.startswith("crates/ruff_workspace/")
            ]
        )
        test_filter = workspace_functions[0] if workspace_functions else ""
        commands.append(f"cargo test -p ruff_workspace {test_filter}".strip())

    if any(path.startswith("crates/ruff_linter/") for path in paths):
        linter_files = [
            patch_file
            for patch_file in test_files
            if patch_file.path.startswith("crates/ruff_linter/")
        ]
        linter_functions = added_test_functions(linter_files)
        module = linter_module(paths)
        rule_codes = sorted(
            {
                match.group(1)
                for path in paths
                for match in RULE_CODE_RE.finditer(Path(path).name)
            }
        )
        test_filter = (
            linter_functions[0]
            if linter_functions
            else (module or (rule_codes[0] if rule_codes else ""))
        )
        if test_filter or not commands:
            commands.append(f"cargo test -p ruff_linter {test_filter}".strip())

    if commands:
        return list(dict.fromkeys(commands))

    fallback = {
        "formatter": "cargo test -p ruff_python_formatter",
        "parser": "cargo test -p ruff_python_parser",
        "configuration": "cargo test -p ruff_workspace",
        "language-server": "cargo test -p ruff_server",
        "linter": "cargo test -p ruff_linter",
    }.get(subsystem, "cargo test --workspace")
    return [fallback]


def triage_candidate(record: dict[str, Any], tasks_dir: Path) -> dict[str, Any]:
    task_id = str(record["task_id"])
    task_dir = tasks_dir / task_id
    reasons: list[str] = []
    warnings: list[str] = []
    try:
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        tests_text = (task_dir / "tests.patch").read_text(encoding="utf-8")
        gold_text = (task_dir / "gold.patch").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "task_id": task_id,
            "priority_score": 0,
            "static_status": "reject",
            "reasons": [f"artifact_read_failure:{exc}"],
            "warnings": [],
        }

    test_files = parse_patch(tests_text)
    gold_files = parse_patch(gold_text)
    if not test_files:
        reasons.append("empty_or_unparseable_tests_patch")
    if not gold_files:
        reasons.append("empty_or_unparseable_gold_patch")

    test_paths = [patch_file.path for patch_file in test_files]
    gold_paths = [patch_file.path for patch_file in gold_files]
    overlaps = sorted(set(test_paths) & set(gold_paths))
    if overlaps:
        reasons.append("gold_and_tests_touch_same_file")

    direct_rust_files = [
        patch_file for patch_file in test_files if is_direct_rust_test(patch_file.path)
    ]
    test_functions = added_test_functions(test_files)
    snapshots = [patch_file for patch_file in test_files if is_snapshot(patch_file.path)]
    fixtures = [patch_file for patch_file in test_files if is_fixture(patch_file.path)]
    formatter_expectations = [
        patch_file for patch_file in test_files if patch_file.path.endswith(".expect")
    ]
    existing_behavior_artifacts = [
        patch_file
        for patch_file in snapshots + fixtures + formatter_expectations
        if not patch_file.new_file
    ]
    all_behavior_artifacts_new = bool(
        snapshots or fixtures or formatter_expectations
    ) and not existing_behavior_artifacts
    test_registration_in_gold = contains_test_registration(gold_files)

    if test_registration_in_gold and not direct_rust_files:
        reasons.append("test_registration_left_in_gold")
    if (
        all_behavior_artifacts_new
        and not direct_rust_files
        and task.get("subsystem") != "formatter"
    ):
        reasons.append("only_new_unregistered_test_artifacts")
    if not direct_rust_files and not snapshots and not fixtures and not formatter_expectations:
        reasons.append("no_recognized_executable_test_signal")

    suggested = suggest_commands(
        str(task.get("subsystem", record.get("subsystem", "unknown"))),
        test_files,
        test_functions,
    )
    current_commands = task.get("test_commands", [])
    if current_commands != suggested:
        warnings.append("preliminary_test_command_can_be_focused")
    if suggested == ["cargo test --workspace"]:
        warnings.append("workspace_wide_test_command")

    score = 0
    if direct_rust_files:
        score += 45
    if test_functions:
        score += 15
    if existing_behavior_artifacts:
        score += 30
    if formatter_expectations:
        score += 30
    if snapshots:
        score += 10
    if fixtures:
        score += 10
    subsystem = str(task.get("subsystem", record.get("subsystem", "unknown")))
    score += {
        "linter": 20,
        "formatter": 20,
        "parser": 15,
        "configuration": 10,
        "other-core": 5,
        "language-server": -10,
        "notebooks": -15,
    }.get(subsystem, 0)
    if len(gold_files) <= 3:
        score += 10
    elif len(gold_files) > 10:
        score -= 15
    if test_registration_in_gold:
        score -= 50
    if all_behavior_artifacts_new and not direct_rust_files:
        score -= 35
    if suggested == ["cargo test --workspace"]:
        score -= 35
    score = max(0, score)

    return {
        "task_id": task_id,
        "base_commit": task.get("base_commit"),
        "subsystem": subsystem,
        "priority_score": score,
        "static_status": "reject" if reasons else "eligible",
        "reasons": sorted(set(reasons)),
        "warnings": sorted(set(warnings)),
        "suggested_test_commands": suggested,
        "current_test_commands": current_commands,
        "signals": {
            "direct_rust_test_files": len(direct_rust_files),
            "added_test_functions": test_functions,
            "snapshots": len(snapshots),
            "fixtures": len(fixtures),
            "formatter_expectations": len(formatter_expectations),
            "existing_behavior_artifacts": len(existing_behavior_artifacts),
            "all_behavior_artifacts_new": all_behavior_artifacts_new,
            "test_registration_in_gold": test_registration_in_gold,
            "gold_files": len(gold_files),
            "test_files": len(test_files),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output-dir", default=".cache/triage")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, config_dir = load_config(args.config)
    tasks_dir = resolve_path(config["paths"]["tasks_dir"], config_dir)
    candidates = read_jsonl(
        resolve_path(config["paths"]["candidates_file"], config_dir)
    )
    if args.limit is not None:
        candidates = candidates[: args.limit]
    results = [triage_candidate(record, tasks_dir) for record in candidates]
    ranked = sorted(
        results,
        key=lambda item: (
            item["static_status"] != "eligible",
            -item["priority_score"],
            item["task_id"],
        ),
    )

    output_dir = resolve_path(args.output_dir, config_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "ranked.jsonl", ranked)
    eligible = [item for item in ranked if item["static_status"] == "eligible"]
    write_jsonl(output_dir / "eligible.jsonl", eligible)
    write_jsonl(
        output_dir / "dynamic_pool.jsonl",
        eligible[: min(180, len(eligible))],
    )

    by_subsystem: dict[str, int] = {}
    rejection_reasons: dict[str, int] = {}
    for item in eligible:
        subsystem = item["subsystem"]
        by_subsystem[subsystem] = by_subsystem.get(subsystem, 0) + 1
    for item in ranked:
        for reason in item["reasons"]:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    summary = {
        "candidates": len(ranked),
        "eligible": len(eligible),
        "rejected": len(ranked) - len(eligible),
        "dynamic_pool": min(180, len(eligible)),
        "eligible_by_subsystem": dict(sorted(by_subsystem.items())),
        "rejection_reasons": dict(
            sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))
        ),
    }
    write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
