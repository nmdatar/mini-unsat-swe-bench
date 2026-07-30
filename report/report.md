# An Unsaturated Coding-Agent Benchmark for Ruff

## Abstract

This project develops a coding-agent benchmark from recent merged pull requests
in [Ruff](https://github.com/astral-sh/ruff), a Python linter and formatter
implemented primarily in Rust. The benchmark targets 100 automatically
verifiable maintenance tasks. Each task is dynamically validated by requiring
the relevant tests to pass at the base commit, evaluator-owned regression tests
to fail before the fix, and those tests to pass after applying the original
developer patch. Three models are evaluated using the mini-swe-agent harness in
isolated environments, and their submitted patches are assessed by an automated
scoring pipeline. **[Add the final model results and main conclusion after the
evaluation is complete.]**

## 1. Introduction

### 1.1 Motivation

Evaluating model capabilities, including comparing models with one another, is
important when selecting a model for a particular task. Some models may perform
well on coding tasks, while others may be better suited to vision or other
modalities. Existing coding-agent benchmarks such as SWE-bench provide a
consistent way to measure coding performance, but new tasks are needed as
models improve and existing benchmarks become less diagnostic.

Open-source projects are a useful source of new coding tasks because their issue
descriptions, pull requests, developer patches, and regression tests provide
evidence that a problem is both realistic and verifiable. This project explores
whether those artifacts can be converted into a reproducible, automatically
scored coding-agent benchmark.

### 1.2 Research Questions

The main question this project attempts to answer is:

1. Can we generate a coding benchmark from an open-source repository whose
   tasks can be automatically validated and scored?

### 1.3 Contributions

This project makes the following contributions:

1. A benchmark of 100 recent, real-world maintenance tasks mined from merged
   Ruff pull requests and screened against known public benchmark tasks, to ensure the same PRs in other benchmarks are not reused.
2. A reproducible construction and validation pipeline that separates
   developer solutions from evaluator-owned tests and verifies each task
   through base-pass, tests-only-fail, and gold-patch-pass outcomes.
3. An automated evaluator that checks submitted patches for compilation,
   hidden-test correctness, regression-test compilation, formatting, and
   evaluator-file tampering.
4. An empirical comparison of three coding models using a shared
   mini-swe-agent configuration, including correctness, runtime, and cost
   measurements.

## 2. Repository and Task Source

### 2.1 Repository Selection

In order to successfully choose a repository for this task, I wanted to choose a repository which was sufficiently large enough to mine enough verifiable tasks, but at the same time was not used extensively in existing benchmarks. This would allow for a large enough dataset to generate an eval set of desired size, while making sure the tasks are not in the model's training data itself. 

### 2.2 Pull Request Sampling Window

Pull requests were sampled from the `astral-sh/ruff` GitHub repository using a
configured merge-date window of August 1, 2025 through July 29, 2026. The 
crawl excluded pull requests labeled `ty` at query time because that
subproject was outside the benchmark's Ruff-core scope.

The resulting dataset contained 1,875 merged pull requests. Their observed
merge timestamps ranged from August 28, 2025 through July 29, 2026. For each
pull request, the crawler recorded its title, description, labels, base and head
commits, changed files, and any explicitly linked issues.

### 2.3 Inclusion and Exclusion Criteria

The initial static filter retained a pull request only when it contained both a
supported production change and a separable source of evaluator-owned tests.
Supported production changes were Rust source files or the repository's
`Cargo.toml`, `Cargo.lock`, and `pyproject.toml` files. Test artifacts were
identified from test, fixture, or snapshot paths; Rust test-file naming
conventions; and snapshot files. Documentation, automation, and repository
metadata could accompany an otherwise eligible change, but those files were
not included in either the production or test patch.

Candidates were excluded when any of the following conditions applied:

- the pull request touched an out-of-scope path such as `playground/`, `ty/`,
  `crates/ty_`, or `crates/red_knot`;
- it lacked a supported production change or a separable test, fixture, or
  snapshot;
- it contained a changed path that could not be classified as production,
  test, ignored, or explicitly excluded;
- it changed fewer than 2 or more than 1,500 total lines, more than 30 files,
  or more than 20 production files;
- it carried a `dependencies` or `release` label, or its title indicated a
  release, dependency upgrade, or version bump;
- its production and test changes could not be split into two nonempty patches;
- its scrubbed task prompt was shorter than 40 or longer than 12,000
  characters; or
- its pull request, base commit, prompt, or production patch overlapped a known
  public benchmark record.

A second static-triage stage inspected the separated patches more closely. It
rejected candidates when the production and test patches modified the same
file, when executable test registration remained in the production patch, when
new fixtures or snapshots lacked an executable registration, or when no
recognized test signal was present. The surviving candidates were ranked using
test executability, subsystem, patch size, and the availability of a focused
test command. Final inclusion additionally required the dynamic
base-pass, tests-only-fail, and gold-patch-pass outcomes described in
Section 4.

### 2.4 Public Benchmark Decontamination

## 3. Benchmark Construction

### 3.1 Task Formulation

Each benchmark task asks an agent to modify Ruff at the state immediately
before a selected pull request was applied. The agent is given the reported
problem and the repository, but the artifacts used to validate and score its
solution remain under evaluator control.

```text
Agent receives
├── Natural-language task prompt
└── Ruff repository at the pull request's base commit
    └── Includes tests already present at that commit

Agent produces
└── A source-code patch

Evaluator adds
├── The model's patch
└── Hidden tests.patch

Evaluator runs
└── Compilation, hidden behavior, regression, and formatting checks
```

#### 3.1.1 Agent Input

The agent receives a natural-language prompt derived from the linked issue or
pull request and the complete Ruff repository checked out at the pull request's
base commit. The repository includes all source files and tests that existed at
that commit. The agent can inspect files, edit source code, and run available
commands through a terminal in an isolated container. It does not receive the
new regression tests introduced by the resolving pull request.

#### 3.1.2 Agent Output

The agent must produce a unified Git diff containing its proposed source-code
changes. The benchmark runner extracts this diff from the agent's final
submission marker and saves it as `patch.diff`. The agent is not required to
reproduce the original developer's implementation; any patch satisfying the
evaluator is acceptable.

#### 3.1.3 Evaluator-Only Artifacts

Each task includes artifacts that are retained by the evaluator and are not
provided to the agent:

- `tests.patch` contains the tests, fixtures, and snapshots introduced by the
  original pull request. These artifacts test whether a submitted patch fixes
  the targeted behavior.
- `gold.patch` contains the original developer's production-code changes. It
  is used to prove that the task is solvable during dynamic validation, but it
  is not applied when scoring a model submission.
- The effective test commands, scoring specification, validation results, and
  source metadata remain under evaluator control unless explicitly included in
  the scrubbed task prompt.

Separating these artifacts prevents an agent from copying the original solution
or directly editing the hidden checks. It also allows alternative
implementations to receive credit when they satisfy the same behavioral
requirements.

#### 3.1.4 Success Criterion

A submission is fully resolved when its patch is nonempty, applies cleanly to
the base repository, does not modify evaluator-owned paths, passes the
compilation gate, and passes all hidden behavior, regression, and formatting
checks. A fully resolved task receives a score of `1.0`. The evaluator may award
partial credit when the compilation gate passes but only a subset of the scored
checks succeeds.

### 3.2 Candidate Collection

Candidate collection converted the fetched pull requests into task artifacts
through the following procedure:

1. **Collect pull request metadata.** The crawler fetched 1,875 merged pull
   requests and recorded their descriptions, linked issues, commits, labels,
   and changed files.
2. **Classify the pull request.** The pipeline applied the path, change-size,
   label, title, and known-public-benchmark criteria from Section 2.3.
3. **Construct and check task artifacts.** For each survivor, the pipeline
   checked that the base and head commits were available, generated a scrubbed
   prompt, wrote production changes to `gold.patch`, and wrote tests, fixtures,
   and snapshots to `tests.patch`. It then applied the remaining prompt,
   nonempty-patch, and public-benchmark prompt/patch overlap criteria. Together,
   these two static steps rejected 1,455 pull requests and retained 420
   candidates.
4. **Perform static triage.** Patch structure and executable-test evidence were
   inspected without running the code. This stage retained 324 eligible
   candidates, of which the 180 highest-ranked candidates formed the dynamic
   validation pool.
5. **Run dynamic validation.** Candidates were executed in Docker until more
   than 100 satisfied the base-pass, tests-only-fail, and gold-patch-pass
   requirements. Within the ranked pool, 103 candidates validated and 9 were
   rejected across 112 completed attempts.
6. **Freeze the benchmark.** The first 100 validated candidates in the
   deterministic ranking were written to the final task index. Model results
   were not used during task selection.

For example, `ruff__ruff-25641`, "Preserve whitespace for Quarto cell option
comments," modified one formatter source file and supplied an integration test,
a formatter fixture, and a snapshot. The pipeline separated those files into
nonempty production and test patches, and the candidate continued to triage
and dynamic validation.

### 3.3 Task Prompt Construction

### 3.4 Production and Test Patch Separation

### 3.5 Static Filtering

The initial static filter applied the size, path, label, prompt, and
decontamination criteria described in Section 2.3. It operated only on pull
request metadata and generated patches; it did not execute Ruff or determine
whether the tests passed. A pull request was rejected if any exclusion reason
was present. Because one pull request could violate multiple criteria,
individual rejection-reason counts are not mutually exclusive.

As a simple rejection example, `ruff__ruff-27284`, "Update Rust crate serde to
v1.0.229," was excluded because it did not contain a separable test, fixture, or
snapshot. The change therefore could not supply an evaluator-owned behavioral
check. Similarly, `ruff__ruff-26436`, "fix typos," reached the prompt
construction step but was rejected because its scrubbed prompt contained fewer
than 40 characters. In contrast, `ruff__ruff-25641` contained both a supported
Rust production change and separable test artifacts and therefore survived this
stage.

### 3.6 Candidate Ranking and Triage

Static triage examined the contents of the separated patches to estimate whether
the proposed hidden tests would be executable. It checked for registered Rust
tests, existing or newly added fixtures, snapshots, formatter expectations,
file overlap between the two patches, and test registration accidentally left
in the production patch. It also proposed focused Cargo commands based on the
affected crate, test target, module, and newly added test-function names.

Eligible candidates received a priority score favoring direct test
registration, existing behavioral artifacts, small production patches, and
predictable Ruff subsystems. Candidates were then sorted first by eligibility,
then by decreasing priority score, and finally by task identifier. The
top-ranked 180 eligible candidates formed the dynamic pool.

For example, `ruff__ruff-19045` contained useful fixtures and snapshots for
three `flake8-gettext` rules, but the corresponding `#[test_case]`
registrations remained in `gold.patch`. Without the gold patch, those tests
would not be registered, so triage rejected the candidate with
`test_registration_left_in_gold`. Another candidate, `ruff__ruff-20273`,
contained only a newly created snapshot without an independent test
registration and was rejected as
`only_new_unregistered_test_artifacts`. By comparison,
`ruff__ruff-25641` received a priority score of 110 and two focused commands:
the Quarto CLI test and the formatter test suite.

### 3.7 Final Task Selection

Dynamic-validation outcomes were joined back to the deterministic triage
ranking. The final index contains the first 100 records whose saved status was
`validated`; all 100 entries in the frozen index have that status. Three
additional validated candidates remained outside the primary set. This
procedure made the final selection reproducible while avoiding selection based
on whether any evaluated model could solve a task.

### 3.8 Task-Set Characteristics

## 4. Dynamic Validation

### 4.1 Validation Criteria

#### 4.1.1 Base-Commit Test Outcome

#### 4.1.2 Tests-Only Outcome

#### 4.1.3 Gold-Patch Outcome

### 4.2 Validation Environment

### 4.3 Focused Test Commands

### 4.4 Parallelization and Caching

### 4.5 Validation Results

The dynamic validator accepted `ruff__ruff-25641`: its focused commands passed
on the unmodified base, failed after applying `tests.patch`, and passed after
applying both `tests.patch` and `gold.patch`. This is the expected transition
for an executable regression test that is repaired by the original developer
solution.

In contrast, `ruff__ruff-20375`, "Add
`analyze.string-imports-min-dots` to settings," survived both static stages but
was dynamically rejected. Its focused test command passed in the base,
tests-only, and gold phases. Because the evaluator-owned test did not fail
before the solution was applied, the candidate was classified as
`hidden_tests_do_not_consistently_fail` and could not enter the benchmark.

### 4.6 Rejection Reasons

### 4.7 Manual Review

## 5. Agent Evaluation Methodology

### 5.1 Models

### 5.2 mini-swe-agent Configuration

### 5.3 Agent Inputs and Repository Access

### 5.4 Runtime, Step, and Cost Limits

### 5.5 Patch Submission and Extraction

### 5.6 Infrastructure Failure Policy

### 5.7 Experimental Procedure

## 6. Evaluator and Scoring

### 6.1 Hidden-Test Installation

### 6.2 Patch Application and Tamper Checks

### 6.3 Compilation Gate

### 6.4 Semantic Check Groups

### 6.5 Score Calculation

### 6.6 Fully Resolved Criterion

### 6.7 Scorer Verification

## 7. Results

### 7.1 Experiment Completion

### 7.2 Model Performance

### 7.3 Resolution Rate

### 7.4 Score Distribution

### 7.5 Runtime

### 7.6 Token Usage and Cost

### 7.7 Infrastructure Failures and Retries

## 8. Analysis

### 8.1 Performance by Subsystem

### 8.2 Performance by Task Size

### 8.3 Common Agent Failure Modes

### 8.4 Qualitative Trajectory Analysis

### 8.5 Evidence of Benchmark Unsaturation

## 9. Threats to Validity

### 9.1 Construct Validity

### 9.2 Internal Validity

### 9.3 External Validity

### 9.4 Validation Limitations

### 9.5 Model and Provider Limitations

## 10. Reproducibility

### 10.1 Software and Container Environment

### 10.2 Configuration and Model Identifiers

### 10.3 Reproduction Commands

### 10.4 Artifact Layout

### 10.5 Data and Credential Handling

## 11. Conclusion

## References

## Appendix A. Final Task Inventory

## Appendix B. Candidate and Rejection Statistics

## Appendix C. Model Configurations

## Appendix D. Scoring Specification

## Appendix E. Supplementary Results
