# Unsaturated Coding-Agent Benchmark Plan

## Goal

Create a reproducible benchmark of 100 real-world coding tasks from
[`astral-sh/ruff`](https://github.com/astral-sh/ruff), then use mini-swe-agent
to compare at least three models or model configurations.

The benchmark should answer two questions:

1. Can a coding agent solve recent, realistic maintenance tasks in a large Rust
   codebase?
2. How do the models compare on correctness, regressions, cost, and runtime?

## Repository

Use `astral-sh/ruff`.

- GitHub reported more than 14,000 merged pull requests in July 2026, comfortably
  exceeding the requirement of at least 1,000.
- Ruff is a large, actively developed Rust monorepo with clearly separated
  subsystems, including the linter, formatter, parser, CLI, configuration,
  notebooks, and language-server support.
- The project has extensive unit, fixture, snapshot, Markdown, and integration
  tests. Many bug-fix PRs already contain the regression tests needed to build
  an automatic evaluator.
- Ruff's toolchains and test commands are explicit and suitable for pinned
  Docker environments.
- Coding models generally have stronger Rust and Python familiarity than Elixir
  familiarity. This should produce a more useful spread of scores instead of a
  benchmark where every model fails primarily because the language is niche.

Ruff is already represented by a small number of tasks in SWE-bench
Multilingual. Therefore, the benchmark must not claim that Ruff is completely
unseen. It will instead pursue freshness by excluding every PR found in public
coding-agent datasets and selecting recent PRs that postdate those tasks.

Here, "unsaturated" means the final task set is fresh, non-overlapping with
known public benchmarks, and not close to fully solved by the evaluated models.

## Initial Directory Structure

Keep the first version deliberately small:

```text
mini-unsat-swe-bench/
├── README.md
├── PLAN.md
├── config.yaml
│
├── environment/
│   ├── Dockerfile
│   └── setup.sh
│
├── scripts/
│   ├── 01_fetch_prs.py
│   ├── 02_make_tasks.py
│   ├── 03_validate_tasks.py
│   ├── 04_run_benchmark.py
│   ├── 05_score_results.py
│   └── 06_make_report.py
│
├── tasks/
│   ├── candidates.jsonl
│   ├── index.jsonl
│   └── <task-id>/
│       ├── task.json
│       ├── eval.json
│       ├── tests.patch
│       └── gold.patch
│
├── results/
│   ├── runs/
│   ├── patches/
│   ├── logs/
│   └── scores.jsonl
│
└── report/
    ├── report.md
    └── figures/
```

Do not add a package hierarchy, formal JSON schemas, CI workflows, or multiple
Docker definitions until the basic pipeline works. Raw GitHub responses,
repository clones, Docker caches, credentials, and other large temporary
artifacts should live in a gitignored cache directory.

## Configuration

Use one `config.yaml` as the source of truth for:

- Repository and PR date range.
- Candidate filtering and task-selection thresholds.
- Known public benchmark task and PR exclusions.
- Docker image, Rust toolchain, Python, and uv versions.
- Task and test timeouts.
- Scoring requirement groups, gates, and weights.
- mini-swe-agent version and shared agent limits.
- The three or more model configurations.
- Random seed and output paths.

Before the final comparison, record exact model identifiers, image digests,
toolchain versions, task hashes, and the mini-swe-agent commit or release.

## Pipeline

### 1. Fetch merged PRs

`scripts/01_fetch_prs.py` will:

- Query the GitHub API for recent merged Ruff PRs.
- Collect PR metadata, linked issues, labels, base and merge commits, changed
  files, and patches.
- Cache responses so later stages are reproducible and do not repeatedly use
  the API.
- Start with enough recent PRs to produce approximately 300 plausible
  candidates. If too few survive validation, extend the date range backward.
- Fetch task identifiers and PR references from relevant public benchmarks so
  known benchmark tasks can be excluded.

### 2. Generate and filter candidate tasks

`scripts/02_make_tasks.py` will perform extraction, overlap detection, and
static filtering. Keeping these together avoids building a framework before the
task-mining rules are understood.

For the initial version, include changes to Ruff's core Rust crates and related
fixtures. Exclude unrelated `ty`, playground, website, release-engineering, and
infrastructure changes unless the initial candidate pool is insufficient.

A candidate must:

- Change production behavior.
- Add or modify deterministic tests that demonstrate that behavior.
- Have a clear issue or PR description that can become a self-contained prompt.
- Run without external services, credentials, or network access.
- Avoid dependency-only, release-only, formatting-only, generated-only,
  documentation-only, benchmark-only, and CI-only changes.
- Have a reasonably bounded implementation patch.
- Allow production changes and evaluator tests, fixtures, and snapshots to be
  separated.
- Have no PR, commit, issue, prompt, or patch overlap with known public coding
  benchmarks.

For each candidate:

- Use the PR's base commit as the starting state.
- Create a prompt from the linked issue when possible.
- Otherwise rewrite the PR description into a behavioral prompt.
- Remove the PR number, commit SHA, author, gold solution, and unnecessary
  implementation hints.
- Split the original PR into `gold.patch` and `tests.patch`.
- Include test fixtures and expected snapshots in `tests.patch`.
- Write the agent-visible task definition to `task.json`.
- Write evaluator-only behavioral requirements, check mappings, gates, and
  scoring weights to `eval.json`.
- Write a summary record to `tasks/candidates.jsonl`.

The task definition should contain only the task ID, prompt, base commit,
subsystem, environment key, test commands, and timeouts. The agent must never
receive `tests.patch`, `gold.patch`, source PR metadata, or future Git history.
The evaluator-only definition must group low-level tests into semantic checks
for core behavior, edge cases, and regression safety. A semantic check may run
multiple assertions or test cases, but it contributes one result so that a
requirement with many assertions does not accidentally dominate the score.

### 3. Validate and select 100 tasks

`scripts/03_validate_tasks.py` will build clean containers and check that:

1. The base commit builds with its pinned Rust toolchain.
2. Selected existing regression tests pass at the base commit.
3. Applying only `tests.patch` causes at least one relevant test to fail.
4. Applying both `tests.patch` and `gold.patch` makes all target tests pass.
5. The gold patch does not break the regression tests.
6. Fixtures and snapshots produce stable results.
7. The result is stable across three clean runs.
8. Evaluation completes within the configured time limit.

Reject candidates that are flaky, platform-dependent, underspecified,
unreasonably slow, or difficult to grade safely. Also reject changes whose tests
cannot be hidden without revealing or requiring the implementation. Save every
rejection reason so the filtering process can be audited.

Select exactly 100 validated tasks while maintaining variety across:

- Linter rule behavior and automatic fixes.
- Formatter behavior.
- Python parsing and semantic analysis.
- CLI and configuration behavior.
- Notebook and source-file handling.
- Small, medium, and larger changes.

Cap repeated tasks involving the same lint rule or subsystem and remove
near-duplicate prompts, patches, and test behavior. Manually review all 100
final prompts and their target-test mappings. Freeze the selected task IDs in
`tasks/index.jsonl` before running the final model comparison.

### 4. Build the evaluation environment

The single `environment/Dockerfile` should accept the Rust toolchain and base
commit as build arguments. It should install the required system packages,
Python, uv, and locked Rust dependencies. Compatible tasks should share cached
dependency and compilation layers.

For every run:

- Start from a clean checkout at the task's base commit.
- Provide only a single-commit Git history with no remote or future commits.
- Disable network access.
- Do not include hidden tests, fixtures, snapshots, the gold patch, or PR
  metadata in the agent container.
- Use fixed CPU, memory, step, token, and wall-clock limits.
- Save the agent's final Git diff as its proposed solution.

### 5. Run mini-swe-agent

`scripts/04_run_benchmark.py` will:

- Run a 10–15 task pilot to find infrastructure problems.
- Use the pilot only to repair invalid tasks or harness bugs, not to favor a
  model or tune the leaderboard.
- Freeze the benchmark after the pilot.
- Run at least three models/configurations on all 100 tasks using the same
  mini-swe-agent prompt, tools, limits, and environment.
- Run one primary trajectory per model and task, for at least 300 scored runs.
- Save trajectories, generated patches, token usage, cost, runtime, exit state,
  and exact model settings.

If budget permits, repeat runs with three seeds. Repeated runs are an extension,
not required for the first complete benchmark.

### 6. Score solutions

`scripts/05_score_results.py` will evaluate each generated patch in a fresh,
network-disabled container.

After applying the model patch, restore evaluator-controlled paths and install
the hidden tests, fixtures, and snapshots. Do not score similarity to the human
patch.

Score semantic requirements rather than the raw number of test functions or
assertions that pass. Each task's `eval.json` must define one or more named
checks in the first three groups below. A check passes only when all low-level
tests mapped to that check pass:

```text
raw_score = 0.60 × core_requirement_fraction
          + 0.20 × edge_requirement_fraction
          + 0.15 × regression_requirement_fraction
          + 0.05 × quality_fraction
```

- **Core requirements** capture the primary behavior explicitly requested by
  the prompt.
- **Edge requirements** capture important boundary cases implied by the
  requested behavior.
- **Regression requirements** capture distinct pre-existing behaviors that the
  change must preserve.
- **Quality** covers the task's applicable formatting and static checks.
- Each group fraction is the number of passing semantic checks divided by the
  number of checks in that group. A task without a meaningful edge-case group
  must move that group's weight into core requirements when the task is frozen;
  weights in every `eval.json` must sum to `1`.

Apply the following gates after calculating `raw_score`:

```text
if patch is invalid, evaluator is tampered with, compilation fails,
or the task-level timeout is reached:
    score = 0
else if core_requirement_fraction == 0:
    score = min(raw_score, 0.20)
else:
    score = raw_score
```

- A fully resolved task must score `1.0`, meaning every behavioral requirement
  and applicable quality check passes.
- A harness or infrastructure failure is retried once and then marked invalid
  rather than counted as a model failure.
- Do not score similarity to the gold patch, patch size, reasoning style, or
  specific file choices as correctness. Alternative implementations that
  satisfy the behavioral requirements must receive the same score.

Write both the numeric score and a detailed machine-readable breakdown to
`results/scores.jsonl`. The breakdown must include every named semantic check,
its group, weight, command, status, runtime, and captured failure reason.

Trajectory properties must remain separate from the task correctness score.
For every run, retain model calls, input and output tokens, estimated cost,
wall-clock time, shell-command count, test-command count, patch size, timeout
state, and policy violations. All models run under the same maximum budgets;
these measurements are reported as efficiency and reliability statistics and
never added to or subtracted from correctness.

### 7. Analyze and report

`scripts/06_make_report.py` will generate the report from the frozen task set
and score records.

For each model/configuration, report:

- Mean task score.
- Full-resolution rate.
- Core, edge-case, regression, and quality requirement pass rates.
- Bootstrap confidence intervals across tasks.
- Results by subsystem and approximate task size.
- Pairwise task wins and losses.
- Token usage, estimated cost, runtime, model calls, shell commands, test
  commands, and patch size.
- Correctness-versus-cost and correctness-versus-token comparisons under the
  shared fixed budgets.
- Failure categories such as localization, incorrect Rust logic, type or borrow
  errors, incomplete fixes, snapshot mismatches, regressions, and timeouts.

The report should also explain:

- Why Ruff was selected over Elixir.
- Ruff's prior benchmark exposure and how known tasks were excluded.
- Docker, Rust toolchain, and dependency setup.
- Candidate mining, prompt construction, and hidden-test validation.
- Scoring and evaluator-integrity protections.
- Whether the final results support the claim that the new task set is
  unsaturated.
- Limitations and concrete next steps.

## Completion Criteria

The first benchmark version is complete when:

- Exactly 100 tasks pass all validation checks.
- Every task has a pinned base commit and reproducible environment.
- Base-plus-tests fails and base-plus-gold-plus-tests passes for every task.
- No selected task overlaps with a known public benchmark instance.
- Hidden tests and gold patches are inaccessible to agents.
- At least three model/configuration pairs complete all 100 tasks.
- Every run has a reproducible score and retained trajectory.
- The final report contains results, cost, runtime, limitations, and proposed
  improvements.

Treat the benchmark as empirically unsaturated if at least one evaluated
configuration scores below `0.90` overall and at least 20 tasks remain
unresolved by every evaluated configuration. If it fails this criterion, report
that honestly rather than replacing tasks after seeing the final model results.

## Likely Later Additions

Add more structure only after the end-to-end version works. Likely follow-ups
include formal task schemas, infrastructure tests, CI validation, other Ruff
subprojects, multiple repositories, distributed runners, repeated model seeds,
mutation testing, private evaluator services, and rolling date-based benchmark
releases.

## Assumptions

- Docker, GitHub API access, and credentials for at least three model providers
  will be available during implementation.
- Historical tasks will be limited to commits with reproducible Rust
  toolchains and locked dependencies.
- Public Ruff benchmark instances can be enumerated before candidate selection.
- Hidden tests and gold patches remain private through the measured evaluation.
- Tasks, prompts, scoring rules, environments, and model configurations are
  frozen before final runs begin.
