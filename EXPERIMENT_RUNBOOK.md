# Experiment runbook

This is the primary procedure for reproducing the mini-swe-agent experiment.
Run every command from the repository root.

## Experiment definition

- Repository: Ruff
- Frozen task subset: `benchmark/subsets/core20.txt`
- Models:
  - `qwen3-coder-next`
  - `deepseek-v4-flash`
  - `gpt-5.4-mini`
- Agent limit: 30 model steps per task
- Agent wall-time limit: 180 seconds per task
- Per-command timeout: 60 seconds
- Evaluation timeout: 300 seconds
- Cost limit: $1.00 per model/task run
- Concurrent task workers: 2
- Environment: network-disabled Docker containers with precompiled Ruff targets
- Limit behavior: if a non-empty tracked-file diff exists when the step or
  wall-time limit is reached, the benchmark agent automatically submits it.

These limits and the shared agent prompt must remain identical across models.

## Prerequisites

Create the Python environment and install the pinned dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r benchmark/requirements.txt
```

Docker must be running with approximately 8 CPUs and 16 GB of memory available.

Put the OpenRouter key in `.env.local`:

```text
OPENROUTER_API_KEY=...
```

Do not commit `.env.local`.

Confirm the frozen inputs exist:

```bash
test -f tasks/index.jsonl
test -f benchmark/subsets/core20.txt
wc -l tasks/index.jsonl benchmark/subsets/core20.txt
```

Expected sizes are 100 frozen benchmark tasks and 20 primary-experiment tasks.

Run the local test suite before inference:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile scripts/*.py
```

## Canary

The canary uses `ruff__ruff-19571`, a validated task with a one-line reference
solution. A successful DeepSeek canary completed in 18 steps, took about 167
seconds, cost about $0.009, and received a score of `1.0`.

To reproduce the short canary:

```bash
.venv/bin/python scripts/04_run_benchmark.py \
  --run-id deepseek-canary-autosubmit \
  --model deepseek-v4-flash \
  --task-id ruff__ruff-19571 \
  --workers 1 \
  --step-limit 20 \
  --wall-time-limit-seconds 180 \
  --evaluation-timeout 300 \
  --cost-limit 1.00
```

Summarize it with:

```bash
.venv/bin/python scripts/06_summarize_results.py \
  results/runs/deepseek-canary-autosubmit \
  --config config.yaml
```

## Primary 20-task, three-model run

The primary run ID is `core20-three-model`. The exact command is:

```bash
.venv/bin/python scripts/04_run_benchmark.py \
  --run-id core20-three-model \
  --model qwen3-coder-next \
  --model deepseek-v4-flash \
  --model gpt-5.4-mini \
  --task-id ruff__ruff-20777 \
  --task-id ruff__ruff-22478 \
  --task-id ruff__ruff-21513 \
  --task-id ruff__ruff-24152 \
  --task-id ruff__ruff-25641 \
  --task-id ruff__ruff-25869 \
  --task-id ruff__ruff-20201 \
  --task-id ruff__ruff-20318 \
  --task-id ruff__ruff-20418 \
  --task-id ruff__ruff-20588 \
  --task-id ruff__ruff-20907 \
  --task-id ruff__ruff-21043 \
  --task-id ruff__ruff-21256 \
  --task-id ruff__ruff-21469 \
  --task-id ruff__ruff-22234 \
  --task-id ruff__ruff-22632 \
  --task-id ruff__ruff-22663 \
  --task-id ruff__ruff-22669 \
  --task-id ruff__ruff-22717 \
  --task-id ruff__ruff-22774 \
  --workers 2 \
  --step-limit 30 \
  --wall-time-limit-seconds 180 \
  --evaluation-timeout 300 \
  --cost-limit 1.00
```

The subset contains 15 linter tasks, two formatter tasks, and one task each
from parser, configuration, and other-core. It was selected before inspecting
primary-run model outcomes.

## Resume an interrupted run

Use the same command and add `--skip-existing`. The runner skips only completed,
scorable model/task pairs and retries missing or infrastructure-failed jobs:

```bash
.venv/bin/python scripts/04_run_benchmark.py \
  --run-id core20-three-model \
  --model qwen3-coder-next \
  --model deepseek-v4-flash \
  --model gpt-5.4-mini \
  --task-id ruff__ruff-20777 \
  --task-id ruff__ruff-22478 \
  --task-id ruff__ruff-21513 \
  --task-id ruff__ruff-24152 \
  --task-id ruff__ruff-25641 \
  --task-id ruff__ruff-25869 \
  --task-id ruff__ruff-20201 \
  --task-id ruff__ruff-20318 \
  --task-id ruff__ruff-20418 \
  --task-id ruff__ruff-20588 \
  --task-id ruff__ruff-20907 \
  --task-id ruff__ruff-21043 \
  --task-id ruff__ruff-21256 \
  --task-id ruff__ruff-21469 \
  --task-id ruff__ruff-22234 \
  --task-id ruff__ruff-22632 \
  --task-id ruff__ruff-22663 \
  --task-id ruff__ruff-22669 \
  --task-id ruff__ruff-22717 \
  --task-id ruff__ruff-22774 \
  --workers 2 \
  --step-limit 30 \
  --wall-time-limit-seconds 180 \
  --evaluation-timeout 300 \
  --cost-limit 1.00 \
  --skip-existing
```

Do not change limits, prompts, model overlays, or the subset when resuming the
same run ID.

## Summarize and inspect results

Evaluation is integrated into the benchmark runner. A separate invocation of
`04b_evaluate_results.py` is not needed unless inference was deliberately run
with `--skip-evaluation`.

Generate the report files:

```bash
.venv/bin/python scripts/06_summarize_results.py \
  results/runs/core20-three-model \
  --config config.yaml
```

Primary artifacts:

```text
results/runs/core20-three-model/
├── manifest.json
├── runs.json
├── scores.json
├── summary.json
├── summary.md
├── subsystem-summary.json
├── pairwise-comparisons.json
└── <model>/<task-id>/
    ├── trajectory.json
    ├── runner.log
    ├── patch.diff
    ├── run.json
    ├── eval.json
    ├── checks.json
    └── score.json
```

Report at minimum:

- Mean score and 95% confidence interval
- Full-resolution and partial-resolution rates
- Number attempted, scorable, and unscorable
- Step-limit and wall-time-limit rates
- Median runtime and steps
- Total and mean cost
- Cost per resolved task
- Submission and compilation success rates
- Failure categories and infrastructure errors
- Results by Ruff subsystem

Treat provider errors, Docker failures, and evaluator failures as unscorable
infrastructure outcomes. Do not convert them into model scores of zero.

