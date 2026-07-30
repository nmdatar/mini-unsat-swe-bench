# Fast Validation Plan

## Objective

Identify 100 usable Ruff benchmark tasks in 1–2 hours, run at least three
mini-swe-agent model configurations in 2–3 hours, and preserve enough evidence
for a defensible report.

The original plan—three validation repetitions, task-specific Docker builds,
and 100-step agent runs—is too expensive for this deadline. This fast path uses
one validation repetition, focused tests, shared compilation caches, parallel
workers, and shorter agent limits.

## Current Position

The repository already has enough candidates:

- 420 generated candidates.
- 324 statically eligible candidates.
- 258 strong candidates under tighter quality criteria.
- 180 candidates in the dynamic-validation pool.

Stop fetching more PRs. Candidate generation is no longer the bottleneck.

## Fast Candidate Selection

Start with approximately 130 candidates, expecting validation failures to leave
at least 100.

Require every selected candidate to have:

- Static triage status `eligible`.
- Priority score of at least 70.
- No more than three files in `gold.patch`.
- A focused test command rather than `cargo test --workspace`.
- A test patch containing at least one of:
  - A directly registered Rust test.
  - A modification to an existing fixture.
  - A modification to an existing snapshot.
  - A formatter expectation.
- No file overlap between `gold.patch` and `tests.patch`.
- No test registration accidentally left in `gold.patch`.
- A nonempty prompt without PR numbers, commit hashes, source links, or explicit
  solution instructions.
- No overlap with known public benchmark tasks.

Immediately reject:

- Snapshot-only changes whose snapshots are not registered by the hidden test
  patch.
- New fixtures without an executable test registration.
- Workspace-wide test commands.
- Dependency and Cargo-lock changes.
- Tasks requiring schema or code generation.
- Tests requiring external services or network access.
- Large, vague, or implementation-revealing prompts.
- Language-server tasks unless they are necessary for diversity.

Prefer recent PRs because nearby commits are more likely to share Rust
toolchains, dependencies, and incremental build artifacts. There are currently
60 strong candidates at PR number 24000 or newer; validate these first, then
fill from progressively older candidates.

## Fast Diversity Target

Favor Ruff’s fastest and most predictable subsystems:

- 75 linter tasks.
- 12 formatter tasks.
- 5 configuration or CLI tasks.
- 3 parser tasks.
- 5 other-core tasks.

Avoid language-server tasks in the emergency version because they are generally
slower to compile and test.

## Fast Dynamic Validation

The current validator is sequential and designed for high-confidence
validation. Do not run it unchanged across 100 tasks.

Add or use an emergency mode equivalent to:

```text
--workers 8
--repeats 1
--stop-after-valid 100
--fast
```

For each candidate, perform:

1. Prepare a clean worktree at the base commit.
2. Apply `tests.patch`.
3. Run the focused target command.
4. Require at least one expected failure.
5. Apply `gold.patch` without discarding compilation artifacts.
6. Run the same focused target command.
7. Require success.
8. Run `cargo check` or one small regression command.
9. Record the result and rejection reason.
10. Stop once 100 candidates have passed.

Use the cheapest checks first:

```text
Artifact and prompt checks
    ↓
Patch-application checks
    ↓
Tests-only focused test
    ↓
Gold focused test
    ↓
Small regression or cargo check
```

Do not run base regression tests before confirming that the target test
transitions from failing to passing.

## Compilation and Container Caching

Share these caches across validation jobs:

- `CARGO_HOME`.
- Cargo registry and Git dependency caches.
- Installed rustup toolchains.
- Incremental Cargo target directories.

Group candidates by Rust toolchain and affected crate. Use a shared target
directory such as:

```text
.cache/cargo-target/<toolchain>/<crate>/
```

Keep source trees isolated with separate Git worktrees, but reuse compilation
artifacts between the tests-only and gold phases.

Avoid building three independent Docker images or containers for each phase.
Use one reusable tooling image and clean task worktrees, or otherwise ensure
BuildKit and Cargo caches persist across task images.

## Manual Review Under the Deadline

A complete manual review of 100 tasks is not realistic.

Instead:

- Automatically scan all 100 prompts for PR numbers, commit hashes, GitHub
  links, implementation sections, and solution leakage.
- Manually review the 20 highest-risk tasks.
- Review every non-linter task.
- Randomly spot-check another 10 tasks.
- Record which tasks were manually reviewed.

Document this sampled-review policy as a benchmark limitation.

## Task-Selection Schedule

| Time | Activity |
|---|---|
| 0–10 minutes | Freeze fetching and select the top 130 candidates |
| 10–20 minutes | Confirm Docker, caches, and one focused test |
| 20–80 minutes | Validate candidates with approximately 8 workers |
| 80–100 minutes | Fill failures from the next ranked candidates |
| 100–115 minutes | Run automated prompt-leakage checks and manual sampling |
| 115–120 minutes | Freeze the first 100 valid tasks |

If Docker is not running, start it before this process because it otherwise
becomes the critical blocker.

## Fast mini-swe-agent Profile

Override the reportable benchmark configuration with:

```yaml
agent:
  step_limit: 30
  cost_limit: 1.5
  wall_time_limit_seconds: 720

environment:
  timeout: 120
  container_timeout: 15m
```

Use:

- Three models.
- One seed.
- One fresh container per model/task pair.
- One retry only for confirmed infrastructure failures.
- No retry for model errors, malformed patches, compilation failures, or
  timeouts.

## Pilot

Run a five-task pilot across all three models:

```text
5 tasks × 3 models = 15 pilot runs
```

The pilot must confirm:

- Provider authentication.
- Trajectory writing.
- Submission-marker recognition.
- Patch extraction.
- Container cleanup.
- Cost and token tracking.

Do not spend more than 15–20 minutes on the pilot. Use it to fix infrastructure
problems, not to change tasks based on model performance.

## Experiment Concurrency

The complete experiment requires:

```text
100 tasks × 3 models = 300 runs
```

At an average of six minutes per run:

```text
300 × 6 minutes = 1,800 worker-minutes
```

Idealized wall times are:

| Workers | Approximate wall time |
|---:|---:|
| 8 | 225 minutes |
| 12 | 150 minutes |
| 16 | 113 minutes |
| 24 | 75 minutes |

Start with approximately 12 workers. Increase concurrency only if:

- Docker has sufficient memory.
- Model providers are not rate-limiting.
- Rust compilation is not saturating the host.
- Result files are concurrency-safe.

When running many containers, allocate approximately one CPU and 2–3 GB of
memory per agent container instead of the normal four CPUs and 8 GB.

Keep scoring concurrency lower than inference concurrency because scoring
requires Rust compilation and testing.

## Separate Inference and Scoring

During inference:

1. Start a clean task container.
2. Give the agent only `task.json`.
3. Allow repository inspection, editing, and focused testing.
4. Save the submitted patch and trajectory.
5. Destroy the agent container.

During scoring:

1. Start from a new clean task state.
2. Apply the model patch.
3. Restore evaluator-controlled files.
4. Apply `tests.patch`.
5. Run the focused target checks.
6. Run a small regression check.
7. Calculate and save the score.

Score the three model patches for the same task close together so they can
benefit from shared Cargo caches.

## Experiment Fallbacks

If 300 runs will not complete in time, apply these fallbacks in order:

1. Reduce the agent limit to 20 steps and eight minutes.
2. Increase inference concurrency while keeping scoring concurrency lower.
3. Run all three models on a stratified 30-task subset.
4. Clearly report that the benchmark defines 100 tasks but the experimental
   comparison used a 30-task subset.

Do not imply that all 300 runs completed if only a subset was evaluated.

## Evidence Required for the Report

Retain:

- Initial candidate, eligible, and strong-candidate counts.
- Static and dynamic rejection reasons.
- Number of candidates dynamically attempted.
- Test commands and base-fail/gold-pass outcomes.
- The final task index and its hash.
- Exact mini-swe-agent and model configurations.
- Container and toolchain versions.
- Trajectories and submitted patches.
- Per-run scores, runtime, token usage, and cost.
- Infrastructure failures and retries.

Explicitly state these emergency-process limitations:

- Only one dynamic-validation repetition.
- Focused tests rather than full Ruff workspace tests.
- Sampled rather than complete manual prompt review.
- One model trajectory per task.
- Potentially reduced agent step and time limits.

The highest-leverage optimization is parallel validation that shares Cargo
artifacts and stops immediately after finding 100 successful tasks.
