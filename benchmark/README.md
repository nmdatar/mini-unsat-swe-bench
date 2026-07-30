# mini-swe-agent benchmark configuration

This directory contains only inference configuration. Task generation lives in
`scripts/`, task assets live in `tasks/`, the execution image lives in
`environment/`, and scoring is implemented separately.

## Files

- `mini.yaml` is the shared agent, prompt, Docker, resource, and limit config.
- `models.yaml` lists the model configurations included in a batch run.
- `models/*.yaml` are mini-swe-agent overlays that change only the model.
- `requirements.txt` pins the mini-swe-agent version used for reportable runs.

## Credentials

Keep credentials on the host. Do not put them in YAML files or forward them to
the task container.

```shell
set -a
source .env.local
set +a
```

All three configurations use the host's `OPENROUTER_API_KEY`. The key is not
copied into Docker images or task containers.

## Install

```shell
python3 -m venv .venv
.venv/bin/pip install -r benchmark/requirements.txt
```

## Run one task manually

First build the task image from the task's base commit as described in
`environment/README.md`. Then run mini-swe-agent by merging the shared config,
one model overlay, and the task-specific image override:

```shell
TASK_ID=ruff__ruff-20777
MODEL_CONFIG=benchmark/models/qwen3-coder-next.yaml
IMAGE=mini-unsat-ruff-validation:ruff--ruff-20777

.venv/bin/mini \
  --yolo \
  --exit-immediately \
  --config benchmark/mini.yaml \
  --config "${MODEL_CONFIG}" \
  --config "environment.image=${IMAGE}" \
  --task "$(jq -r .prompt "tasks/${TASK_ID}/task.json")" \
  --output "results/manual/${TASK_ID}.traj.json"
```

The model API call runs on the host. Bash commands run through mini-swe-agent's
Docker environment in the network-isolated task container.

The submitted patch is stored in the trajectory's `info.submission` field. The
batch runner extracts it to `patch.diff`, evaluates it immediately, and writes
the run, check, score, resource-cleanup, and usage telemetry as JSON.

## Fair-comparison policy

For reportable runs:

- Freeze the task set, image digests, mini-swe-agent version, shared config, and
  model overlays before starting.
- Use the same step, cost, wall-time, command-timeout, CPU, and memory limits for
  every model.
- Run one fresh container per model/task pair.
- Retry confirmed infrastructure failures only; do not retry model failures.
- Record exact provider-returned model versions because aliases and preview
  models can change.
- Do not change prompts or model parameters after inspecting final results.

The registry currently compares Qwen3 Coder Next, DeepSeek V4 Flash, and
GPT-5.4 mini through OpenRouter. Each response is capped at 4,096 tokens, while
the shared agent configuration enforces identical step, time, and cost limits.
