# mini-unsat-swe-bench

A reproducible benchmark mined from recent merged pull requests in
[`astral-sh/ruff`](https://github.com/astral-sh/ruff). The complete design is
in [PLAN.md](PLAN.md).

The final report is available as [Markdown](report/report.md) and
[PDF](report/report.pdf). The exact primary experiment procedure is documented
in [EXPERIMENT_RUNBOOK.md](EXPERIMENT_RUNBOOK.md), and AI assistance is
disclosed in [AI_USE.md](AI_USE.md).

## Current implementation

The repository implements the complete sourcing, validation, execution,
scoring, and reporting path:

1. Fetch merged PR metadata and normalize known public-benchmark exclusions.
2. Apply static filters, construct prompts, and split each surviving PR into a
   production `gold.patch` and evaluator-owned `tests.patch`.
3. Validate candidates through clean base, tests-only, and gold-patch phases.
4. Validate evaluator specifications and map completed semantic checks to a
   gated score in `[0, 1]`.
5. Run mini-swe-agent against task-specific Docker images, evaluate each patch
   immediately, clean disposable resources, and aggregate telemetry.

## Dynamic validation

The validator reads candidate artifacts without modifying `tasks/`. It first
copies a stable snapshot to `.cache/validation/staging`, then runs repeated
base, tests-only, and tests-plus-gold phases in clean, network-disabled
containers. Inspect a candidate selection without Docker:

```bash
python scripts/03_validate_tasks.py --dry-run --limit 5
```

Run an inexpensive single-repeat development pass:

```bash
python scripts/03_validate_tasks.py \
  --task-id ruff__ruff-23635 \
  --repeats 1
```

Run the configured one-repeat validation pass after the commands and
environment are working. Repeated phases can be enabled later to measure
flakiness:

```bash
python scripts/03_validate_tasks.py --limit 10
```

Detailed results and staged artifacts are written under
`.cache/validation`; candidate directories remain untouched.

## Scoring contract

Each evaluated task receives an evaluator-only `eval.json` generated in its run
output directory. It assigns named semantic checks to `core`, `edge`,
`regression`, and `quality` groups and declares at least one compilation gate.
The evaluator runner records the status of those checks in a separate JSON
file. Score one completed run with:

```bash
python scripts/05_score_results.py \
  --eval-spec results/runs/<run-id>/<model-id>/<task-id>/eval.json \
  --check-results results/runs/<run-id>/<model-id>/<task-id>/checks.json
```

The scorer does not execute untrusted code. Docker execution and hidden-test
installation belong to the validation and evaluation stages; the scorer is a
pure, independently testable mapping from check outcomes to a score.

## Setup

Python 3.11+ and Git are required. Valid GitHub authentication is effectively
required for the full PR window because unauthenticated API limits are too
small. The crawler first checks `GITHUB_TOKEN`, then safely falls back to the
token managed by the GitHub CLI:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export GITHUB_TOKEN=...
```

If you already use the GitHub CLI, authenticate instead with:

```bash
gh auth login -h github.com
```

The token is only passed in the API request header. It is never written into
the benchmark cache or generated task files.

Review the date range and thresholds in `config.yaml`, then run:

```bash
python scripts/01_fetch_prs.py
python scripts/02_make_tasks.py
python scripts/02b_triage_candidates.py
```

Static triage ranks candidates by executable-test evidence and validation cost,
flags patch-split problems, and proposes focused test commands. It writes
`ranked.jsonl`, `eligible.jsonl`, `dynamic_pool.jsonl`, and `summary.json`
under `.cache/triage` without changing task artifacts.

Validate the ranked pool in resumable batches:

```bash
python scripts/03_validate_tasks.py --limit 10 --workers 2 --skip-existing
```

Within a candidate, the validator shares a disposable Cargo target volume
across base, tests-only, and gold phases, then removes it. Saved per-task
results allow later batches to skip completed candidates.

For an inexpensive first pass:

```bash
python scripts/01_fetch_prs.py --skip-public-benchmarks
python scripts/02_make_tasks.py --limit 5
```

Do not use `--skip-public-benchmarks` for the frozen benchmark. It exists only
to make development and cache debugging faster.

## Sourcing outputs

Large and reproducible downloads live under the ignored `.cache/` directory.
The frozen benchmark tasks are tracked under `tasks/`; development candidates
and intermediate artifacts may be kept under ignored paths when generated
locally:

```text
tasks/
├── exclusions.jsonl
├── candidates.jsonl
├── validation_queue.jsonl
├── rejections.jsonl
└── ruff__ruff-<pr-number>/
    ├── task.json
    ├── tests.patch
    └── gold.patch
```

`task.json` is the only task artifact intended to be visible to an agent.
`tests.patch`, `gold.patch`, rejection details, and source PR metadata are
evaluator-only data.

The generated test commands are subsystem-level starting points. The validation
stage must replace or supplement them with precise target and regression test
commands.

`candidates.jsonl` retains every static survivor. `validation_queue.jsonl`
contains the deterministic subset prioritized for expensive Docker validation.
Selection uses the configured random seed and round-robins across subsystem and
small/medium/large change-size strata; it never uses model results.

The frozen index contains 100 validated tasks. The completed model comparison
reported in `report/report.md` uses the predeclared 20-task `core20` subset and
three models (60 model-task runs); the remaining frozen tasks are available for
the full benchmark run described in the runbook.

## Fast assessment workflow

The following is the reportable path. Dynamic validation uses one
base/tests-only/gold cycle and reuses a Cargo target cache across the three
isolated containers.

To overlap validation, three-model inference, evaluation, and reporting, use
the streaming orchestrator:

```bash
.venv/bin/python scripts/07_stream_pipeline.py \
  --run-id final \
  --validation-workers 2 \
  --agent-workers 2
```

It writes a live machine-readable report to
`.cache/validation/pipeline-progress.json`. Validation-only ETA and acceptance
statistics are available at any time with:

```bash
.venv/bin/python scripts/00_status.py --workers 2
```

The equivalent individual stages are:

```bash
# 1. Rank candidates and validate enough to fill 100 tasks plus reserves.
.venv/bin/python scripts/02b_triage_candidates.py
.venv/bin/python scripts/03c_validate_until.py \
  --target-valid 100 --batch-size 6 --workers 2

# 2. Freeze exactly 100 validated tasks.
.venv/bin/python scripts/03b_freeze_tasks.py

# 3. Calibrate the evaluator on a small frozen-task sample.
.venv/bin/python scripts/05b_calibrate_scoring.py \
  --run-id scorer-calibration --limit 3

# 4. Run all enabled OpenRouter models and summarize.
.venv/bin/python scripts/04_run_benchmark.py --run-id final --workers 2
.venv/bin/python scripts/06_summarize_results.py results/runs/final
```

The benchmark runner performs evaluation task-by-task as part of the run. It
builds or reuses one image per task, runs all selected models, scores their
patches, and removes the task image and per-run Cargo volumes. Use
`--keep-resources` only for debugging. `04b_evaluate_results.py` remains
available for evaluating trajectories collected with `--skip-evaluation`.

Each run directory contains a reproducibility manifest, per-job trajectories,
patches, run records, check outcomes, scores, and aggregate `runs.json` and
`scores.json` files. Records include elapsed time, steps, provider token usage,
cost, limit-hit flags, provider/model identity, image metadata, config hashes,
infrastructure failures, and cleanup status. The summarizer reports score
distributions, resolution rates, runtime, cost, token use, steps, compile rate,
failure categories, subsystem results, and pairwise model comparisons.

For pipeline testing before validation completes, `03b_freeze_tasks.py
--provisional` creates a clearly marked non-reportable index. The benchmark
runner refuses those tasks unless `--allow-unvalidated` is supplied.
