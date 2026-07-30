#!/usr/bin/env bash
set -euo pipefail

readonly TESTBED=/testbed

if [[ ! -d "${TESTBED}/.git" ]]; then
    echo "error: ${TESTBED} is not a Git worktree" >&2
    exit 1
fi

cd "${TESTBED}"

# An agent needs Git for inspecting and exporting its patch, but must not be
# able to fetch the source PR or commits newer than the task's base revision.
git remote remove origin >/dev/null 2>&1 || true
git config user.name "Benchmark Agent"
git config user.email "benchmark-agent@localhost"
git config --global --add safe.directory "${TESTBED}"

if [[ "${1:-}" == "smoke-test" ]]; then
    readonly commit_count="$(git rev-list --count HEAD)"
    readonly remote_count="$(git remote | wc -l | tr -d ' ')"

    if [[ "${commit_count}" != "1" ]]; then
        echo "error: expected one visible commit, found ${commit_count}" >&2
        exit 1
    fi

    if [[ "${remote_count}" != "0" ]]; then
        echo "error: expected no Git remotes, found ${remote_count}" >&2
        exit 1
    fi

    git diff --quiet
    git diff --cached --quiet
    rustc --version
    cargo --version
    uv --version
    python3 --version
    cargo metadata --locked --no-deps --format-version 1 >/dev/null

    echo "environment smoke test passed at $(git rev-parse HEAD)"
    exit 0
fi

exec "$@"
