# Experiment Plan

## Objective

Compare at least three small, fast coding models on the same 100 validated Ruff
tasks, then measure how performance changes with agent budget, repeated attempts,
task characteristics, runtime, and cost.

The fixed-budget, 100-task comparison is the primary experiment. Ablations
should use smaller, predetermined subsets so they do not dominate runtime or
cost.

## Experimental principles

- Use the same frozen 100-task index for every model.
- Give every model the same prompt, tools, environment, step limit, and timeout.
- Run each model-task pair from a clean copy of the task's base state.
- Keep hidden evaluator tests unavailable to the agent.
- Record actual cost rather than using a restrictive equal dollar cap as the
  primary constraint.
- Use both a step limit and a wall-clock timeout.
- Decide all task subsets before looking at model results.
- Separate agent failures from evaluator or infrastructure failures.

## Phase 0: Evaluator calibration

Before interpreting model results, confirm the expected evaluator behavior:

1. The unchanged base repository does not solve the task.
2. The evaluator-owned tests fail when applied without the implementation fix.
3. The human gold patch passes the evaluator.
4. A patch that does not compile cannot receive a misleadingly high score.

Dynamic validation already establishes most of this contract. Summarize those
validation results in the report rather than rerunning unnecessary work.

## Phase 1: Five-task pilot

Run all three selected models on the same five representative tasks before
starting the full benchmark.

The pilot should include a mixture of subsystems and task sizes. Its purpose is
to verify:

- mini-swe-agent launches correctly;
- model credentials and routing work;
- patches are persisted;
- evaluation and scoring complete;
- timeouts stop cleanly;
- cost and latency are within the available budget;
- result summaries distinguish agent and infrastructure failures.

Suggested initial resource envelope:

- 30 to 40 agent steps;
- 10 to 15 minutes of wall time per task;
- the lowest supported temperature;
- one attempt per model-task pair;
- identical tool and token settings for every model.

If the pilot regularly reaches the step limit while still making progress,
prefer 40 steps. If models stop naturally well before the limit, use 30 steps
to reduce worst-case runtime.

## Phase 2: Primary benchmark

Run three models on all 100 frozen tasks with one attempt per model-task pair.
This produces 300 primary runs.

Use identical:

- task ordering and task set;
- starting commit and container environment;
- prompt;
- available tools;
- step limit;
- wall-clock and command timeouts;
- temperature;
- output-token limits;
- evaluator and scoring procedure.

Do not use equal dollar limits as the main fairness mechanism. Model prices
differ, so an equal cost cap can provide unequal effective interaction budgets.
Use a sufficiently high safety cap, then report actual expenditure.

### Primary metrics

Report the following for each model:

1. Mean score across all 100 tasks.
2. Median score.
3. Full-resolution rate: fraction of tasks scoring exactly `1.0`.
4. Partial-resolution rate: fraction scoring strictly between `0` and `1`.
5. Zero-score rate.
6. Compilation or core-test success rate.
7. Score distribution:
   - `0`;
   - `(0, 0.5)`;
   - `[0.5, 1)`;
   - `1`.
8. Total and mean cost.
9. Cost per fully resolved task.
10. Score per dollar.
11. Median and mean wall time.
12. Time per fully resolved task.
13. Timeout rate.
14. Median steps used.
15. Fraction of runs reaching the step limit.

### Failure categories

Classify unsuccessful runs into mutually understandable categories:

- no patch produced;
- patch does not apply;
- compilation failure;
- core behavior remains incorrect;
- only edge or regression checks fail;
- agent step limit;
- wall-clock timeout;
- model or routing error;
- evaluator or infrastructure failure.

Infrastructure failures should be retried and should not be counted as model
failures unless the retry policy is exhausted and disclosed.

### Statistical comparison

Because every model receives the same tasks, use paired comparisons.

- Bootstrap the 100 tasks to calculate 95% confidence intervals for each
  model's mean score.
- Bootstrap pairwise per-task score differences between models.
- Report both the mean difference and its confidence interval.
- Avoid claiming meaningful ranking differences when intervals are wide or
  strongly overlapping.

## Phase 3: Step-budget ablation

Use one representative model on a fixed, stratified 20-task subset.

Run the model with:

- 10 steps;
- 20 steps;
- 40 steps.

Hold every other setting constant. Report:

- mean score versus step limit;
- full-resolution rate versus step limit;
- mean cost versus step limit;
- median runtime versus step limit;
- fraction exhausting the step budget;
- marginal score gain from 10 to 20 and from 20 to 40 steps.

This distinguishes model-capability failures from failures caused by an
insufficient interaction budget. It also reveals diminishing returns.

If time is limited, reduce this to a stratified 10-task subset.

## Phase 4: Reliability and pass@k

Select a fixed, stratified set of 10 to 15 tasks and run each model three times
under the primary configuration.

Report:

- pass@1;
- pass@3;
- mean score over attempts;
- per-task score variance;
- fraction of tasks solved inconsistently;
- changes, if any, in the model ranking across attempts.

This tests whether one run per task is a stable estimate. If time is limited,
use five tasks per model.

## Optional ablations

Run these only if the primary benchmark, step-budget ablation, and reliability
experiment are complete.

### Fixed interaction budget versus fixed cost

On the same 20-task subset, compare:

- identical step and time limits;
- identical dollar limits.

Use the fixed-interaction result for the capability comparison and the
fixed-cost result for practical economic efficiency.

### Prompt-information ablation

For 10 to 20 tasks, compare:

- the full cleaned PR-derived problem description;
- a short problem statement with implementation hints removed;
- the short statement plus the relevant public test command.

This measures sensitivity to prompt specificity and possible implementation
leakage from PR descriptions.

### Test-command availability

Compare an agent that receives the focused public test command with one that
must discover the relevant tests. Hidden evaluator tests remain unavailable in
both conditions.

This separates repository-navigation ability from implementation ability.

### Retry after limited feedback

For failed runs, permit one additional attempt that receives only a broad
failure category such as:

- compilation failed;
- core behavior remains incorrect;
- regression checks failed.

Do not reveal hidden-test contents. This approximates an iterative development
workflow.

## Post-hoc benchmark analysis

The following analyses require no additional model calls.

### Performance by subsystem

Report mean score and resolution rate for:

- linter;
- formatter;
- parser;
- configuration;
- CLI and other core tasks.

### Performance by task characteristics

Group tasks using model-independent information:

- gold patch size;
- changed production-file count;
- changed test-file count;
- prompt length;
- subsystem;
- task change-size category;
- modification of existing behavior versus addition of new behavior.

Do not define task difficulty using only one model's results.

### Empirical difficulty

After all three models run, categorize tasks as:

- solved by all models;
- solved by some models;
- solved by exactly one model;
- solved by no models.

A useful unsaturated benchmark should contain a meaningful middle and hard
tail. Near-universal success suggests saturation; near-universal zero scores
suggest excessive difficulty, insufficient budgets, or evaluator problems.

### Model complementarity

Report:

- tasks solved only by each individual model;
- tasks solved by any model;
- the oracle-ensemble score obtained by selecting the best model score on each
  task.

This shows whether models with similar aggregate scores have different
strengths.

### Patch characteristics

Compare model patches with human patches behaviorally and structurally without
requiring textual equality:

- files touched;
- lines added and deleted;
- patch size relative to the gold patch;
- whether tests were modified;
- whether unrelated files were modified;
- whether the model solution is smaller or larger than the human solution.

### Cost-quality frontier

Plot:

- x-axis: mean cost per task;
- y-axis: mean benchmark score;
- label or bubble size: median runtime.

Also report cost per full solution and score per dollar. This identifies models
on the practical quality-cost Pareto frontier.

## Recommended execution order

1. Run the five-task, three-model pilot.
2. Correct any environment, timeout, persistence, or scoring problems.
3. Run the 100-task primary benchmark for all three models.
4. Generate the main metrics and failure breakdown.
5. Run the 10/20/40-step ablation on a stratified subset.
6. Run three repeated attempts on a smaller reliability subset.
7. Produce subsystem, difficulty, complementarity, patch, cost, and latency
   analyses from the saved results.
8. Run optional prompt or fixed-cost ablations only if time remains.

## Minimum reportable experiment set

If time or budget becomes constrained, preserve these in order:

1. Three models on all 100 tasks under one fixed configuration.
2. Mean score, full-resolution rate, cost, runtime, and failure categories.
3. Paired bootstrap confidence intervals.
4. One-model step-budget ablation on 10 to 20 tasks.
5. Three-attempt reliability study on 5 to 10 tasks.

The fixed-budget benchmark is the headline result. The step-budget and
reliability ablations answer the two most important objections: whether models
merely needed more time and whether another attempt would materially change
the ranking.
