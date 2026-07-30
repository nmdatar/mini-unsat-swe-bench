# AI Use Disclosure

This document describes how AI assistance was used during the development of
the benchmark, its evaluation pipeline, and the accompanying report. The
purpose of this disclosure is to make clear which work was assisted by AI and
which decisions and checks remained under human responsibility.

## Scope of AI Assistance

AI was used as a development and writing assistant throughout the project. It
was not treated as an authoritative source: generated suggestions and code
were inspected, adapted to the repository, and tested before being retained.
The benchmark's evaluated models are a separate experiment. They received
benchmark prompts and repositories inside the task containers; they did not
write this disclosure or select the benchmark tasks.

## Initial Brainstorming and Project Design

At the beginning of the project, AI was used to brainstorm possible benchmark
questions, repository-selection criteria, task formats, validation procedures,
and scoring schemes. This discussion helped refine the central goal: construct
a recent, realistic coding-agent benchmark from an open-source repository while
ensuring that every retained task is automatically verifiable.

AI assistance was also used to compare possible repository characteristics,
including repository size, language mix, test quality, commit history, and
likelihood of overlap with existing benchmarks. These suggestions informed the
choice of Ruff, but the final repository choice, scope, date range, and
exclusion rules were human decisions.

## Repository and Candidate Discovery

AI helped design searches and scripts for identifying repositories and merged
pull requests that could become tasks. It helped reason about useful signals,
including:

- a clear issue or pull-request description;
- a separable production change and test change;
- focused executable tests or fixtures;
- a manageable patch size and changed-file count;
- a base commit that can be checked out reproducibly; and
- exclusion from known public benchmark datasets.

The resulting scripts fetched merged Ruff pull-request metadata, normalized
records, generated candidate artifacts, and applied static filters. AI helped
interpret candidate examples and refine rejection reasons, but candidates were
not accepted merely because an AI system predicted that they looked useful.
The configured filters, triage rules, and final deterministic ranking were
reviewed against the generated artifacts.

## Scripting and Code Generation

AI assistance was used to draft, explain, review, and debug much of the
benchmark implementation, including scripts for:

1. fetching and caching pull-request metadata;
2. constructing prompts and splitting production and test patches;
3. applying static filters and ranking candidates;
4. validating candidates in base, tests-only, and gold-patch phases;
5. freezing the validated task index;
6. building task-specific Docker images;
7. running mini-swe-agent with OpenRouter model configurations;
8. extracting submitted patches and preserving trajectories;
9. evaluating patches with hidden evaluator-owned tests; and
10. aggregating scores, costs, runtimes, and failure categories.

AI also helped create and revise the execution environment. This included the
Dockerfile, Rust and Python dependency setup, network isolation, resource
limits, Cargo caching and precompilation behavior, smoke tests, and cleanup of
temporary images and volumes. Particular attention was given to keeping
`tests.patch`, `gold.patch`, prompts, and source metadata out of the agent
container when they were evaluator-only artifacts.

Generated code was treated as a starting point. The implementation was
inspected for path handling, patch ordering, timeout behavior, infrastructure
failure classification, cleanup, resumability, and accidental leakage. Unit
tests, calibration runs, Docker smoke tests, and end-to-end pilot runs were
used to check the behavior of the resulting pipeline.

## Running and Debugging the Pipeline

AI was used to interpret command output and logs while the environment and
scoring pipeline were being brought up. This included diagnosing Rust build
times, Docker disk and memory pressure, model timeouts, empty submissions,
formatting failures, and evaluator behavior. It helped propose safer
throughput improvements such as task-level image reuse, precompiling targets,
resuming completed jobs, and limiting concurrent workers to available host
resources.

The final decisions about whether a failure was a model outcome or an
infrastructure error were made by inspecting the run records and evaluator
results.

## Report Preparation and Analysis

AI was used to outline the report and to help organize sections in a style
consistent with coding-agent benchmark papers. It assisted with drafting and
tightening descriptions of:

- task formulation and the agent input/output contract;
- candidate collection, static filtering, triage, and dynamic validation;
- inclusion and exclusion criteria;
- evaluator-only artifacts and the directory layout;
- the Docker execution environment and scoring pipeline;
- pull-request sampling and decontamination; and
- reproducibility commands and artifact handling.

AI also helped summarize validation counts, model scores, runtimes, costs,
submission outcomes, and failure modes from the saved run artifacts.

## Human Responsibility and Verification

The human author retained responsibility for the project scope, repository and
task-selection decisions, benchmark criteria, code review, execution choices,
interpretation of results, and final report text. AI-generated code and prose
were edited as needed, and the repository was checked with unit tests,
compilation/syntax checks, validation runs, scorer calibration, Docker smoke
tests, and end-to-end benchmark runs. The final benchmark index and reported
results were based on the saved task and run artifacts, not on unsupported AI
assertions.
