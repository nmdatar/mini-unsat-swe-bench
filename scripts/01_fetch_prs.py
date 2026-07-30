#!/usr/bin/env python3
"""Fetch merged Ruff PR metadata and known public-benchmark exclusions.

All GitHub responses are cached by URL-derived keys, so interrupted runs can be
resumed without spending API quota on completed requests.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from _common import load_config, resolve_path, write_json, write_jsonl


ISSUE_CLOSING_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:https://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)|#(\d+))"
)


class GitHubClient:
    def __init__(
        self,
        *,
        api_url: str,
        cache_dir: Path,
        token: str | None,
        timeout: int,
        refresh: bool,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.cache_dir = cache_dir
        self.token = token
        self.timeout = timeout
        self.refresh = refresh
        try:
            import certifi
        except ImportError:
            self.ssl_context = ssl.create_default_context()
        else:
            self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{query}"
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.json"
        if cache_path.exists() and not self.refresh:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "mini-unsat-swe-bench",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(url, headers=headers)
        attempts = 4
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response:
                    result = json.load(response)
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    reset = response.headers.get("X-RateLimit-Reset")
                break
            except urllib.error.HTTPError as exc:
                remaining = exc.headers.get("X-RateLimit-Remaining")
                reset = exc.headers.get("X-RateLimit-Reset")
                if exc.code == 403 and remaining == "0" and reset:
                    delay = max(0, int(reset) - int(time.time())) + 1
                    if delay > 60:
                        raise RuntimeError(
                            "GitHub rate limit exhausted. Cached responses are safe; "
                            f"resume the crawl after {delay}s."
                        ) from exc
                    time.sleep(delay)
                    continue
                if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"GitHub API returned {exc.code} for {url}: {detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(f"Could not reach GitHub API at {url}: {exc}") from exc
        else:
            raise RuntimeError(f"GitHub request retry loop exhausted for {url}")

        write_json(cache_path, result)
        if remaining == "0" and reset:
            delay = max(0, int(reset) - int(time.time())) + 1
            if delay > 60:
                raise RuntimeError(
                    "GitHub rate limit exhausted. Cached responses are safe; "
                    f"resume the crawl after {delay}s."
                )
            print(f"GitHub rate limit exhausted; sleeping {delay}s", file=sys.stderr)
            time.sleep(delay)
        return result

    def paginated(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        item_key: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        page = 1
        while limit is None or len(values) < limit:
            request_params = {**(params or {}), "per_page": 100, "page": page}
            payload = self.get(path, request_params)
            items = payload[item_key] if item_key else payload
            if not isinstance(items, list):
                raise TypeError(f"Expected a list from GitHub endpoint {path}")
            values.extend(items)
            if len(items) < 100:
                break
            page += 1
        return values[:limit] if limit is not None else values


def resolve_github_token(token_env: str) -> tuple[str | None, str]:
    """Resolve authentication without ever printing or persisting the token."""
    if token := os.environ.get(token_env):
        return token, token_env
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "GitHub CLI"
    return None, "unauthenticated"


def linked_issue_numbers(body: str, owner: str, repo: str) -> list[int]:
    numbers: set[int] = set()
    for match in ISSUE_CLOSING_RE.finditer(body):
        ref_owner, ref_repo, url_number, short_number = match.groups()
        if url_number and (ref_owner.lower(), ref_repo.lower()) != (owner.lower(), repo.lower()):
            continue
        numbers.add(int(url_number or short_number))
    return sorted(numbers)


def normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue["number"],
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "html_url": issue.get("html_url"),
        "labels": sorted(label["name"] for label in issue.get("labels", [])),
    }


def newest_first_date_windows(start: str, end: str, days: int) -> Iterable[tuple[str, str]]:
    earliest = date.fromisoformat(start)
    window_end = date.fromisoformat(end)
    if earliest > window_end:
        raise ValueError(f"merged_from {start} is after merged_to {end}")
    while window_end >= earliest:
        window_start = max(earliest, window_end - timedelta(days=days - 1))
        yield window_start.isoformat(), window_end.isoformat()
        window_end = window_start - timedelta(days=1)


def fetch_prs(config: dict[str, Any], config_dir: Path, *, refresh: bool) -> list[dict[str, Any]]:
    repo_config = config["repository"]
    github_config = config["github"]
    paths = config["paths"]
    owner, repo = repo_config["owner"], repo_config["name"]
    token, auth_source = resolve_github_token(
        github_config.get("token_env", "GITHUB_TOKEN")
    )
    if token:
        print(f"Using GitHub authentication from {auth_source}")
    else:
        print(
            "Warning: no valid GitHub authentication found; the full crawl will "
            "exceed the unauthenticated API limit",
            file=sys.stderr,
        )
    cache_dir = resolve_path(paths["github_cache_dir"], config_dir) / "responses"
    cache_dir.mkdir(parents=True, exist_ok=True)

    client = GitHubClient(
        api_url=github_config["api_url"],
        cache_dir=cache_dir,
        token=token,
        timeout=int(github_config["request_timeout_seconds"]),
        refresh=refresh,
    )
    max_prs = int(github_config["max_prs"])
    label_exclusions = "".join(
        f' -label:"{label}"' for label in github_config.get("search_excluded_labels", [])
    )
    search_by_number: dict[int, dict[str, Any]] = {}
    for window_start, window_end in newest_first_date_windows(
        github_config["merged_from"],
        github_config["merged_to"],
        int(github_config.get("search_window_days", 31)),
    ):
        remaining = max_prs - len(search_by_number)
        if remaining <= 0:
            break
        query = (
            f"repo:{owner}/{repo} is:pr is:merged "
            f"merged:{window_start}..{window_end}{label_exclusions}"
        )
        window_items = client.paginated(
            "/search/issues",
            params={"q": query, "sort": "updated", "order": "desc"},
            item_key="items",
            # GitHub Search exposes no more than 1,000 results per query.
            limit=min(1000, remaining),
        )
        for item in window_items:
            search_by_number[int(item["number"])] = item
        print(
            f"Search {window_start}..{window_end}: {len(window_items)} PRs "
            f"({len(search_by_number)}/{max_prs} collected)",
            flush=True,
        )

    search_items = list(search_by_number.values())[:max_prs]
    print(f"Found {len(search_items)} merged PRs across date windows", flush=True)

    def fetch_one(item: dict[str, Any]) -> dict[str, Any]:
        number = int(item["number"])
        pr = client.get(f"/repos/{owner}/{repo}/pulls/{number}")
        files = client.paginated(f"/repos/{owner}/{repo}/pulls/{number}/files")
        issues = []
        for issue_number in linked_issue_numbers(pr.get("body") or "", owner, repo):
            issue = client.get(f"/repos/{owner}/{repo}/issues/{issue_number}")
            if "pull_request" not in issue:
                issues.append(normalize_issue(issue))

        return {
            "number": number,
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "html_url": pr.get("html_url"),
            "created_at": pr.get("created_at"),
            "merged_at": pr.get("merged_at"),
            "base_sha": pr["base"]["sha"],
            "head_sha": pr["head"]["sha"],
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "labels": sorted(label["name"] for label in pr.get("labels", [])),
            "linked_issues": issues,
            "files": [
                {
                    "filename": file["filename"],
                    "status": file["status"],
                    "previous_filename": file.get("previous_filename"),
                    "additions": file.get("additions", 0),
                    "deletions": file.get("deletions", 0),
                    "changes": file.get("changes", 0),
                }
                for file in files
            ],
        }

    output = resolve_path(paths["fetched_prs"], config_dir)
    records: list[dict[str, Any]] = []
    workers = max(1, int(github_config.get("workers", 1)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for index, record in enumerate(executor.map(fetch_one, search_items), 1):
            records.append(record)
            if index % 25 == 0 or index == len(search_items):
                print(f"Fetched {index}/{len(search_items)} PRs", flush=True)
                write_jsonl(output, sorted(records, key=lambda item: item["number"]))

    write_jsonl(output, sorted(records, key=lambda record: record["number"]))
    return records


def iter_huggingface_records(source: dict[str, Any]) -> Iterable[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "Public benchmark fetching requires the 'datasets' package; "
            "install requirements.txt or use --skip-public-benchmarks"
        ) from exc

    for split in source.get("splits", ["test"]):
        dataset = load_dataset(
            source["dataset"],
            source.get("config"),
            split=split,
            streaming=True,
        )
        yield from dataset


def extract_pr_number(record: dict[str, Any], owner: str, repo: str) -> int | None:
    for key in ("pull_number", "pr_number", "number"):
        value = record.get(key)
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            return int(value)
    instance_id = str(record.get("instance_id") or "")
    match = re.search(rf"(?i){re.escape(owner)}(?:__|/){re.escape(repo)}-(\d+)$", instance_id)
    return int(match.group(1)) if match else None


def fetch_public_exclusions(config: dict[str, Any], config_dir: Path) -> list[dict[str, Any]]:
    owner = config["repository"]["owner"]
    repo = config["repository"]["name"]
    normalized_repo = f"{owner}/{repo}".lower()
    exclusions: dict[tuple[str, str], dict[str, Any]] = {}

    for source in config.get("public_benchmarks", {}).get("sources", []):
        if source.get("type") != "huggingface":
            raise ValueError(f"Unsupported public benchmark source: {source.get('type')!r}")
        source_name = source["dataset"]
        for record in iter_huggingface_records(source):
            record_repo = str(record.get("repo") or record.get("repository") or "").lower()
            instance_id = str(record.get("instance_id") or "")
            belongs_to_repo = record_repo == normalized_repo or instance_id.lower().startswith(
                f"{owner}__{repo}-".lower()
            )
            if not belongs_to_repo:
                continue
            pr_number = extract_pr_number(record, owner, repo)
            key = (source_name, instance_id or str(pr_number))
            exclusions[key] = {
                "source": source_name,
                "instance_id": instance_id or None,
                "repository": normalized_repo,
                "pr_number": pr_number,
                "base_commit": record.get("base_commit"),
                "problem_statement_sha256": hashlib.sha256(
                    str(record.get("problem_statement") or "").encode()
                ).hexdigest(),
                "patch_sha256": hashlib.sha256(
                    str(record.get("patch") or record.get("gold_patch") or "").encode()
                ).hexdigest(),
            }

    output = resolve_path(config["paths"]["exclusions_file"], config_dir)
    records = sorted(exclusions.values(), key=lambda item: (item["source"], item["instance_id"] or ""))
    write_jsonl(output, records)
    print(f"Wrote {len(records)} Ruff public-benchmark exclusions")
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached GitHub responses")
    parser.add_argument(
        "--skip-public-benchmarks",
        action="store_true",
        help="Do not download and normalize configured public benchmark datasets",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config, config_dir = load_config(args.config)
    fetch_prs(config, config_dir, refresh=args.refresh)
    if not args.skip_public_benchmarks:
        fetch_public_exclusions(config, config_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
