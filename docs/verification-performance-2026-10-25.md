# Three-way verification performance, 2026-10-25

Issue #81 replaces exhaustive generated form plans with constrained three-way selection.
Native support remains exhaustively enumerated. This measurement compares `main` at
`da6f8f0` with the implementation on `issue-81-three-way`, using an immutable archive of
the baseline and separate Python processes.

The SM89 saved plan shrank from 110,033 forms to 13,315 (87.9% fewer). The new plan has
10,756 full-profile forms and 2,559 minimal-profile forms.

## Measurements

The small workload contains SM89 `ld__sImmOffset` and `hfma2__RRR`, with 33,216 legal
combinations and 28,440 unresolved combinations in both revisions. Values are medians
of three runs. Memory is median peak process RSS; case-generation runs have no worker pool.

| Workload | Before wall | After wall | Before CPU | After CPU | Before peak RSS | After peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plan the two classes | 1.62 s | 1.72 s | 1.66 s | 1.77 s | 800 MiB | 799 MiB |
| Generate default inputs | 5.43 s | 0.58 s | 4.39 s | 0.63 s | 210 MiB | 96 MiB |
| Generate 100% inputs | 3.36 s | 0.55 s | 3.29 s | 0.60 s | 206 MiB | 95 MiB |

Small-workload planned forms fall from 534 to 191. Selected default cases fall from
534 to 191; 100% cases fall from 1,872 to 407. Minimal hint/effect variants retain their
minimal declared corpus at 100%, while full-profile representatives expand their inputs.
Planning costs slightly more in this pair because it constructs requirements and searches for a legal cover;
the result is reused by all case consumers.

One warm all-architecture **evidence refresh** was measured on each revision. This runs
`tests.evidence`, including both floating-point lowerings, PTX probes and rejudging recorded
GPU observations. It excludes manifest generation, coverage-page aggregation and a new GPU run.

| Metric | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Wall time | 401.22 s | 115.82 s | 71.1% |
| Sampled process-tree CPU | 8,464.46 s | 1,803.49 s | 78.7% |
| Sampled peak process-tree RSS | 11.52 GiB | 5.74 GiB | 50.1% |
| Reference cases | 2,235,749 | 585,305 | 73.8% |
| Recorded reference forms | 946,859 | 127,532 | 86.5% |

Raw form counts sum unique forms per architecture. Positive reference forms are
stored separately from diagnostic error/fallback cases. Raw GPU and PTX evidence remains
available independently of the generated plan.

## Reproduction and limits

Run `research/verification_performance.py` from each checkout with its own source imports
and environment:

```sh
PYTHONPATH=src:. SASS_CACHE=/tmp/verification-bench nice uv run \
  python research/verification_performance.py plan --repeat 3 --out /tmp/plan.json
PYTHONPATH=src:. SASS_CACHE=/tmp/verification-bench nice uv run \
  python research/verification_performance.py cases --sample 0 --repeat 3 --out /tmp/cases-0.json
PYTHONPATH=src:. SASS_CACHE=/tmp/verification-bench nice uv run \
  python research/verification_performance.py cases --sample 100 --repeat 3 --out /tmp/cases-100.json
PYTHONPATH=src:. SASS_CACHE=/tmp/verification-bench nice uv run \
  python research/verification_performance.py refresh --repeat 1 \
  --artifacts /tmp/verification-evidence --out /tmp/refresh.json
```

The baseline uses the same measurement script but its original source, manifests and version 2
plans. Measurements used Python 3.13, 32 logical CPUs, `nice`, a shared warm tool cache, and
separate subprocesses. The desktop was active: refresh ending load averages were 22.18 before
and 17.02 after. These are observed runs, not a controlled throughput guarantee; the reduced
case counts and CPU usage provide stronger evidence than wall time alone.

`getrusage(CHILDREN)` does not reliably include forkserver worker CPU, so the harness samples
the descendant process tree every 100 ms. Tree CPU can miss short-lived processes; summed RSS
includes shared pages and is not proportional set size. Coordinator CPU and largest-process
RSS are also retained in the raw files. The baseline small measurements precede process-tree
instrumentation; their ordinary process metrics remain comparable.

The eight raw measurement files are in
[`research/verification-performance/`](../research/verification-performance/).
This measures case generation and evidence extraction, not GPU execution or Ghidra speed.
