# Ruff execution environment

This image provides the clean `/testbed` workspace used by both mini-swe-agent
and the scorer. It contains Ruff at one base commit, the commit's requested Rust
toolchain, uv, Python, common shell tools, downloaded Cargo dependencies, and
optionally precompiled core test targets.

The image intentionally contains:

- A shallow Git checkout with exactly one visible commit.
- No Git remote.
- No task prompt, source PR metadata, hidden tests, or gold patch.

Network isolation is a runtime responsibility because Dockerfiles cannot disable
network access for a later container. Agent and scoring containers must be
started with `--network none`.

## Build

Use an immutable Ruff commit for benchmark tasks:

```shell
docker build \
  --file environment/Dockerfile \
  --build-arg RUFF_COMMIT=<full-base-commit-sha> \
  --tag mini-unsat-ruff:<task-id> \
  environment
```

The default `RUFF_COMMIT=main` exists only for local smoke testing. It must not
be used for a measured task.

Set `PRECOMPILE=false` for a faster diagnostic build. Final task images should
retain the default precompilation so agent and evaluator runs reuse Cargo
artifacts.

## Verify

```shell
docker run --rm --network none mini-unsat-ruff:<task-id> smoke-test
```

The smoke test checks the single-commit history, absence of remotes, clean
worktree, tool versions, lock file, and Cargo workspace metadata.

## Agent run

```shell
docker run --rm --network none \
  --cpus 4 \
  --memory 8g \
  mini-unsat-ruff:<task-id>
```

The benchmark runner should add its own timeout and persist the final patch
outside the container. The scorer should use a new container from the same image
rather than reusing the agent container.

## Reproducibility

For each selected task, record:

- Full Ruff base-commit SHA.
- Final image digest, not only the local tag.
- Resolved Rust, Cargo, uv, and Python versions from the smoke-test output.
- Values of `RUST_IMAGE`, `UV_IMAGE`, and `PRECOMPILE`.
- Host architecture and Docker version.

The base image tags are configurable to support older task commits. Before the
final benchmark, replace them with immutable image digests in the recorded build
configuration.
