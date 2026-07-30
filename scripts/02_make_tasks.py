#!/usr/bin/env python3
"""Create statically filtered Ruff task candidates from fetched PR metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from _common import (
    atomic_write_text,
    load_config,
    read_jsonl,
    resolve_path,
    sha256_text,
    write_json,
    write_jsonl,
)


REMOVED_SECTIONS = {
    "approach",
    "benchmark",
    "checklist",
    "implementation",
    "implementation details",
    "likely cause",
    "proposed fix",
    "solution",
    "test",
    "test plan",
    "testing",
    "version",
}
URL_RE = re.compile(r"https://github\.com/astral-sh/ruff/(?:pull|commit)/[^\s)]+", re.I)
PR_RE = re.compile(r"(?i)\b(?:PR|pull request)\s*#?\d+\b")
SOURCE_REFERENCE_LINE_RE = re.compile(
    r"(?im)^\s*(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:[\w.-]+/[\w.-]+)?#\d+\.?\s*$"
)
NUMBER_REFERENCE_RE = re.compile(r"(?<![\w/])#\d+\b")
COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.I)


def git(repository: Path, *args: str, capture: bool = False) -> str:
    command = ["git", f"--git-dir={repository}", *args]
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def ensure_repository(config: dict[str, Any], config_dir: Path) -> Path:
    repository = resolve_path(config["paths"]["repository_cache"], config_dir)
    if not repository.exists():
        repository.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--filter=blob:none",
                config["repository"]["clone_url"],
                str(repository),
            ],
            check=True,
        )
    git(repository, "fetch", "--prune", "origin")
    return repository


def ensure_commit(repository: Path, sha: str, fallback_ref: str | None = None) -> None:
    exists = subprocess.run(
        ["git", f"--git-dir={repository}", "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode:
        git(repository, "fetch", "origin", fallback_ref or sha)
        verified = subprocess.run(
            ["git", f"--git-dir={repository}", "cat-file", "-e", f"{sha}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if verified.returncode:
            raise RuntimeError(f"Fetched {fallback_ref or sha}, but commit {sha} is unavailable")


def path_is_test(path: str, candidate_filter: dict[str, Any]) -> bool:
    pure_path = PurePosixPath(path.lower())
    markers = {marker.lower() for marker in candidate_filter["test_path_markers"]}
    if any(part in markers or PurePosixPath(part).stem in markers for part in pure_path.parts):
        return True
    if pure_path.stem.endswith(("_test", "_tests")):
        return True
    return pure_path.suffix in {suffix.lower() for suffix in candidate_filter["test_extensions"]}


def path_is_production(path: str, candidate_filter: dict[str, Any]) -> bool:
    pure_path = PurePosixPath(path)
    if path_is_test(path, candidate_filter):
        return False
    if pure_path.suffix.lower() in {
        suffix.lower() for suffix in candidate_filter["production_extensions"]
    }:
        return True
    return pure_path.name in set(candidate_filter["auxiliary_production_files"])


def excluded_path(path: str, candidate_filter: dict[str, Any]) -> bool:
    return any(
        path.startswith(prefix) for prefix in candidate_filter["hard_excluded_prefixes"]
    )


def ignored_path(path: str, candidate_filter: dict[str, Any]) -> bool:
    return path in set(candidate_filter["ignored_exact_paths"]) or any(
        path.startswith(prefix) for prefix in candidate_filter["ignored_prefixes"]
    )


def classify_pr(
    pr: dict[str, Any],
    config: dict[str, Any],
    excluded_prs: set[int],
    excluded_base_commits: set[str],
) -> dict[str, Any]:
    candidate_filter = config["candidate_filter"]
    reasons: list[str] = []
    files = pr["files"]
    changed_lines = sum(int(file["additions"]) + int(file["deletions"]) for file in files)
    paths = [file["filename"] for file in files]
    production_paths = [path for path in paths if path_is_production(path, candidate_filter)]
    test_paths = [path for path in paths if path_is_test(path, candidate_filter)]
    unsupported_paths = [
        path
        for path in paths
        if path not in production_paths
        and path not in test_paths
        and not excluded_path(path, candidate_filter)
        and not ignored_path(path, candidate_filter)
    ]

    if pr["number"] in excluded_prs:
        reasons.append("known_public_benchmark_pr")
    if pr["base_sha"] in excluded_base_commits:
        reasons.append("known_public_benchmark_base_commit")
    if any(excluded_path(path, candidate_filter) for path in paths):
        reasons.append("contains_excluded_path")
    if not production_paths:
        reasons.append("no_supported_production_change")
    if not test_paths:
        reasons.append("no_separable_test_fixture_or_snapshot")
    if unsupported_paths:
        reasons.append("contains_unclassified_paths")
    if len(files) > int(candidate_filter["max_changed_files"]):
        reasons.append("too_many_changed_files")
    if len(production_paths) > int(candidate_filter["max_production_files"]):
        reasons.append("too_many_production_files")
    if changed_lines < int(candidate_filter["min_changed_lines"]):
        reasons.append("too_few_changed_lines")
    if changed_lines > int(candidate_filter["max_changed_lines"]):
        reasons.append("too_many_changed_lines")

    labels = {label.lower() for label in pr.get("labels", [])}
    excluded_labels = {label.lower() for label in candidate_filter["excluded_labels"]}
    if labels & excluded_labels:
        reasons.append("excluded_label")
    if any(
        re.search(pattern, pr["title"], re.I)
        for pattern in candidate_filter["excluded_title_patterns"]
    ):
        reasons.append("excluded_title")

    return {
        "reasons": sorted(set(reasons)),
        "changed_lines": changed_lines,
        "production_paths": sorted(production_paths),
        "test_paths": sorted(test_paths),
        "unsupported_paths": sorted(unsupported_paths),
    }


def strip_markdown_sections(body: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in body.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            title = re.sub(r"[*_`]", "", heading.group(1)).strip().lower().rstrip(":")
            skipping = title in REMOVED_SECTIONS
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def scrub_prompt(title: str, body: str) -> str:
    body = strip_markdown_sections(body)
    body = SOURCE_REFERENCE_LINE_RE.sub("", body)
    body = URL_RE.sub("[redacted source link]", body)
    body = PR_RE.sub("the source change", body)
    body = NUMBER_REFERENCE_RE.sub("a related issue", body)
    body = COMMIT_SHA_RE.sub("[redacted revision]", body)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    prompt = title.strip()
    if body:
        prompt = f"{prompt}\n\n{body}"
    return prompt.strip()


def make_prompt(pr: dict[str, Any]) -> tuple[str, str]:
    linked_issues = pr.get("linked_issues", [])
    if linked_issues:
        issue = linked_issues[0]
        return scrub_prompt(issue["title"], issue.get("body") or ""), "linked_issue"
    return scrub_prompt(pr["title"], pr.get("body") or ""), "pull_request"


def create_patch(repository: Path, base_sha: str, head_sha: str, paths: list[str]) -> str:
    if not paths:
        return ""
    attempts = 3
    for attempt in range(attempts):
        try:
            return git(
                repository,
                "diff",
                "--binary",
                "--full-index",
                base_sha,
                head_sha,
                "--",
                *paths,
                capture=True,
            )
        except subprocess.CalledProcessError:
            if attempt + 1 == attempts:
                raise
            # Partial clones may need to retrieve a promised blob. Retry a
            # transient fetch/packing failure without weakening the filter.
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def subsystem_for(paths: list[str]) -> str:
    joined = "\n".join(paths).lower()
    for needle, subsystem in (
        ("ruff_python_formatter", "formatter"),
        ("ruff_formatter", "formatter"),
        ("ruff_python_parser", "parser"),
        ("ruff_server", "language-server"),
        ("ruff_cli", "cli"),
        ("ruff_workspace", "configuration"),
        ("notebook", "notebooks"),
        ("ruff_linter", "linter"),
    ):
        if needle in joined:
            return subsystem
    return "other-core"


def size_bucket(changed_lines: int) -> str:
    if changed_lines <= 75:
        return "small"
    if changed_lines <= 250:
        return "medium"
    return "large"


def select_validation_queue(
    candidates: list[dict[str, Any]], target: int, seed: int
) -> list[dict[str, Any]]:
    """Deterministically round-robin across subsystem and size strata."""
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        bucket = size_bucket(int(candidate["changed_lines"]))
        strata.setdefault((candidate["subsystem"], bucket), []).append(candidate)

    for key, values in strata.items():
        values.sort(
            key=lambda item: hashlib.sha256(
                f"{seed}:{item['task_id']}".encode()
            ).hexdigest()
        )

    selected: list[dict[str, Any]] = []
    ordered_keys = sorted(strata)
    while len(selected) < min(target, len(candidates)):
        made_progress = False
        for key in ordered_keys:
            values = strata[key]
            if values:
                item = dict(values.pop(0))
                item["size_bucket"] = key[1]
                item["selection_rank"] = len(selected) + 1
                selected.append(item)
                made_progress = True
                if len(selected) >= target:
                    break
        if not made_progress:
            break
    return selected


def test_commands_for(paths: list[str], subsystem: str) -> list[str]:
    # These commands are intentionally conservative placeholders. The validator
    # will refine them to task-specific targets before a candidate is selected.
    if subsystem == "formatter":
        return ["cargo test -p ruff_formatter"]
    if subsystem == "parser":
        return ["cargo test -p ruff_python_parser"]
    if subsystem == "language-server":
        return ["cargo test -p ruff_server"]
    if subsystem == "cli":
        return ["cargo test -p ruff_cli"]
    if subsystem == "configuration":
        return ["cargo test -p ruff_workspace"]
    if subsystem == "linter":
        return ["cargo test -p ruff_linter"]
    return ["cargo test --workspace"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--limit",
        type=int,
        help="Generate at most this many accepted candidates (useful for a smoke test)",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Do not update the cached bare repository before generating patches",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, config_dir = load_config(args.config)
    paths = config["paths"]
    prs = read_jsonl(resolve_path(paths["fetched_prs"], config_dir))
    if not prs:
        raise SystemExit("No fetched PRs found; run scripts/01_fetch_prs.py first")

    exclusions = read_jsonl(resolve_path(paths["exclusions_file"], config_dir))
    excluded_prs = {
        int(record["pr_number"]) for record in exclusions if record.get("pr_number") is not None
    }
    excluded_base_commits = {
        str(record["base_commit"]) for record in exclusions if record.get("base_commit")
    }
    excluded_prompt_hashes = {
        str(record["problem_statement_sha256"])
        for record in exclusions
        if record.get("problem_statement_sha256")
    }
    excluded_patch_hashes = {
        str(record["patch_sha256"]) for record in exclusions if record.get("patch_sha256")
    }

    repository_path = resolve_path(paths["repository_cache"], config_dir)
    if args.no_fetch:
        if not repository_path.exists():
            raise SystemExit(f"Repository cache does not exist: {repository_path}")
        repository = repository_path
    else:
        repository = ensure_repository(config, config_dir)

    tasks_dir = resolve_path(paths["tasks_dir"], config_dir)
    candidates: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for pr in sorted(prs, key=lambda record: record["merged_at"] or "", reverse=True):
        classification = classify_pr(pr, config, excluded_prs, excluded_base_commits)
        if classification["reasons"]:
            rejections.append(
                {
                    "task_id": f"ruff__ruff-{pr['number']}",
                    "pr_number": pr["number"],
                    "reasons": classification["reasons"],
                }
            )
            continue

        ensure_commit(repository, pr["base_sha"])
        ensure_commit(repository, pr["head_sha"], f"refs/pull/{pr['number']}/head")
        gold_patch = create_patch(
            repository,
            pr["base_sha"],
            pr["head_sha"],
            classification["production_paths"] + classification["unsupported_paths"],
        )
        tests_patch = create_patch(
            repository,
            pr["base_sha"],
            pr["head_sha"],
            classification["test_paths"],
        )
        if not gold_patch.strip() or not tests_patch.strip():
            rejections.append(
                {
                    "task_id": f"ruff__ruff-{pr['number']}",
                    "pr_number": pr["number"],
                    "reasons": ["empty_split_patch"],
                }
            )
            continue

        prompt, prompt_source = make_prompt(pr)
        if len(prompt) < 40:
            rejections.append(
                {
                    "task_id": f"ruff__ruff-{pr['number']}",
                    "pr_number": pr["number"],
                    "reasons": ["prompt_too_short"],
                }
            )
            continue
        if len(prompt) > int(config["candidate_filter"]["max_prompt_characters"]):
            rejections.append(
                {
                    "task_id": f"ruff__ruff-{pr['number']}",
                    "pr_number": pr["number"],
                    "reasons": ["prompt_too_long"],
                }
            )
            continue
        overlap_reasons = []
        if sha256_text(prompt) in excluded_prompt_hashes:
            overlap_reasons.append("known_public_benchmark_prompt")
        if sha256_text(gold_patch) in excluded_patch_hashes:
            overlap_reasons.append("known_public_benchmark_patch")
        if overlap_reasons:
            rejections.append(
                {
                    "task_id": f"ruff__ruff-{pr['number']}",
                    "pr_number": pr["number"],
                    "reasons": overlap_reasons,
                }
            )
            continue

        task_id = f"ruff__ruff-{pr['number']}"
        subsystem = subsystem_for(classification["production_paths"])
        task = {
            "task_id": task_id,
            "prompt": prompt,
            "base_commit": pr["base_sha"],
            "subsystem": subsystem,
            "environment_key": "ruff-rust-toolchain",
            "test_commands": test_commands_for(classification["test_paths"], subsystem),
            "timeouts": {"agent_seconds": 1800, "test_seconds": 900},
        }
        task_dir = tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        write_json(task_dir / "task.json", task)
        atomic_write_text(task_dir / "gold.patch", gold_patch)
        atomic_write_text(task_dir / "tests.patch", tests_patch)

        candidates.append(
            {
                "task_id": task_id,
                "pr_number": pr["number"],
                "merged_at": pr["merged_at"],
                "base_commit": pr["base_sha"],
                "head_commit": pr["head_sha"],
                "subsystem": subsystem,
                "prompt_source": prompt_source,
                "prompt_sha256": sha256_text(prompt),
                "gold_patch_sha256": sha256_text(gold_patch),
                "tests_patch_sha256": sha256_text(tests_patch),
                "changed_lines": classification["changed_lines"],
                "production_paths": classification["production_paths"],
                "test_paths": classification["test_paths"],
                "validation_status": "pending",
            }
        )
        print(f"Accepted static candidate {task_id} ({subsystem})")
        if args.limit is not None and len(candidates) >= args.limit:
            break

    write_jsonl(resolve_path(paths["candidates_file"], config_dir), candidates)
    validation_queue = select_validation_queue(
        candidates,
        int(config["candidate_filter"]["target_candidates"]),
        int(config["candidate_filter"]["random_seed"]),
    )
    write_jsonl(
        resolve_path(paths["validation_queue_file"], config_dir),
        validation_queue,
    )
    write_jsonl(resolve_path(paths["rejections_file"], config_dir), rejections)
    print(
        f"Wrote {len(candidates)} candidates, {len(validation_queue)} queued for "
        f"validation, and {len(rejections)} rejections"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
