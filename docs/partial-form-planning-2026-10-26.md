# Partial-form planning, 2026-10-26

Issue #80 now uses the existing three-way planner for reference-backed operand rows
inside partially native modifier forms. This changes case selection and evidence,
not instruction semantics or the exhaustive support classification.

## Operand contracts and migration

`tests/harness/autocase_partial.py` declares the eligible opcodes and their operand
constraints. Adapters must preserve those constraints, encode the requested class
and modifiers exactly, and supply independent reference values. Failed construction
does not satisfy a requirement. Native P-code or explicit effect/cost guards still
reject opaque fallback.

| Opcodes | Generated operand row |
| --- | --- |
| LD, LDG, LDL, LDS, ST, STG, STL, STS | Aligned data registers, including pairs/quads; predicate outputs discarded where unresolved. LDG checks its ordinary completion output. |
| LDC, LDCU, ULDC | Direct constant bank, aligned destination pair/quad and every output word checked. |
| FLO, UFLO | Unresolved predicate output fixed to PT/UPT. |
| MOV | Full pixel mask. |
| R2UR | `noOR` class, executing-thread value, Pu=PT. |
| IADD | Ordinary 32-bit add, Pu=PT; extended/wide rows remain unsupported. |
| VABSDIFF4, VIMNMX | Unresolved output predicates fixed to PT. |
| HADD2 | Supported F32 widening with an explicit negative-zero register operand. |
| HFMA2, HFMA2.MMA | Literal half addends with register factors; the supported negative-zero literal-move shortcut has a separate operand row. |

Ordinary manual FLO, R2UR, IADD, VABSDIFF4, partial VIMNMX, full-mask MOV,
wide memory/constant and HADD2 widening cases were removed where generation replaces
them. Focused operation-count, constant-predicate, descriptor/window, conversion-boundary
and opaque-form regressions remain. Whole-kernel compiler fixtures remain integration
tests. The migrated profiles preserve the former FLO bit boundaries, VABSDIFF4 byte
rotations and HADD2 normal/subnormal widening boundaries. Wide-memory profiles use
distinct high words, so a missing output cell fails.

LD's sem/sco/private modifiers share a table field. Coverage enumeration now preserves
the first decoded tuple for each raw field value, rather than counting later spelling
aliases as independent decoded forms. Strict encoding and provenance checks are unchanged.

## Partial-class audit

The audit reads the exhaustive manifests, saved plans and adapter-gap reports:

```sh
PYTHONPATH=src:. python research/partial_form_audit.py --out /tmp/partial-audit.json
```

It reports every partially native class, including classes without a declared operand
adapter. Counts below describe the current three-way selection; they do not imply
exhaustive operand or exact-form verification.

| Architecture | Partial classes | Classes with declared adapter | Selected partial forms | Full-profile partial forms | Remaining interactions in declared adapters |
| --- | ---: | ---: | ---: | ---: | ---: |
| SM75 | 63 | 63 | 1,424 | 394 | 987 |
| SM80 | 70 | 70 | 1,765 | 349 | 774 |
| SM86 | 65 | 65 | 1,622 | 260 | 0 |
| SM89 | 65 | 65 | 1,622 | 260 | 0 |
| SM90 | 61 | 61 | 1,372 | 225 | 0 |
| SM100 | 66 | 55 | 1,373 | 228 | 0 |
| SM101 | 52 | 52 | 1,337 | 192 | 0 |
| SM103 | 66 | 55 | 1,373 | 228 | 0 |
| SM107 | 74 | 50 | 1,325 | 180 | 0 |
| SM120 | 50 | 50 | 1,325 | 180 | 0 |

The SM75/SM80 HADD2 blockers cover additional source-sign/mixed-precision corpora
and operand legality restrictions. SM100/SM103 packed F32 and SM107 mixed F32/half
classes without a declared partial adapter remain explicit gaps. Their existing
manual regressions were not replaced.

## Performance

The baseline is `8ad9abd32bc65761aecba9ef61ff2131ccb20448`, imported from an immutable
archive. Both small-workload planners read the same corrected SM89 manifests and
write isolated plans. Each case-generation measurement reads its corresponding
fresh plan, rather than mixing an old saved plan with a new dry-run inventory.
The workload contains `ld__sImmOffset`, `ldg__sImmOffset`, `stg__sImmOffset`,
`flo__RRR_RRR` and `mov__RR`. Values are medians of three fresh processes.

| Workload | Before wall | After wall | Before CPU | After CPU | Before peak RSS | After peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Plan five classes | 4.78 s | 1.97 s | 4.83 s | 2.02 s | 328 MiB | 327 MiB |
| Generate default inputs | 0.90 s | 1.13 s | 0.95 s | 1.18 s | 83 MiB | 84 MiB |
| Generate 100% inputs | 0.86 s | 1.08 s | 0.92 s | 1.13 s | 82 MiB | 83 MiB |

The common inventory contains 11,433 legal combinations and 5,892 interaction
requirements. Covered requirements rise from 4,831 to 5,892 and planned forms
from 467 to 634. Default cases rise from 467 to 634; complete inputs rise from
547 to 1,020. Planning also avoids building the old manual families for FLO.
Case construction costs more because the new plan includes additional operand
rows and wider reference-backed corpora. Encoding, decoding and sampling remain
the dominant case-construction phases; memory probes keep their eager checks.
A diagnostic run of the current generator on the old 467-form plan took 0.95 s
wall and 1.00 s CPU, confirming that most of the increase comes from the added
forms/corpora. Decode-IR caches were separated by revision for the final small
comparison, avoiding cache eviction when switching source archives.

One warm all-architecture evidence refresh was measured per revision. It includes
both lowerings, PTX traces and rejudging recorded GPU observations, but excludes
manifest generation, coverage rendering and fresh GPU execution.

| Metric | Before | After |
| --- | ---: | ---: |
| Wall | 112.38 s | 106.93 s |
| Sampled process-tree CPU | 1,838.94 s | 1,665.75 s |
| Sampled peak process-tree RSS | 5.79 GiB | 5.81 GiB |
| Reference cases | 608,897 | 618,798 |
| Recorded reference forms | 139,143 | 151,858 |

Removing duplicate manual corpora offsets much of the added generation cost:
reference cases increase 1.6%, reference forms 9.1%, and measured worker CPU
decreases 9.4%. Wall time decreases 4.9% on an active desktop, with ending
load averages 15.36 before and 16.07 after. This is an observed cost comparison,
not a throughput guarantee. The small diagnostic profiling runs also showed
that switching source archives invalidates decode-IR cache entries; cold IR
construction should not be interpreted as an adapter regression.

Raw measurements are in
[`research/partial-form-performance/`](../research/partial-form-performance/).
Use `research/verification_performance.py` with `--classes` for this workload and
`--plan-dir` for isolated comparison plans. Its process-tree accounting and
memory limitations are documented in the
[three-way performance report](verification-performance-2026-10-25.md).

## Validation

All-architecture exact/readable lifted semantics and family lowering passed:
3,959 tests, including 235,769 selected shared cases in the four affected families.
Ghidra on SM89/SM90, both lowerings, passed 988 tests with 52,354 selected shared cases.
On SM89, 11,974 hardware-valid shared scenarios passed; 1,640 model-only scenarios
reported explicit reasons. The shared/fixture GPU suite passed 479 tests with
15 skips. Existing SM90 observations remain available, but this change was not
rerun on an SM90 GPU.

Those three suites ran concurrently in 192–222 seconds. The historical 15-second
GPU estimate predates today's shared-case workload and does not describe this run;
CPU case preparation competed with the software validation pools. These concurrent
times are validation observations, not replacements for isolated performance baselines.

After completing the migrated boundary corpora, the three changed families
(`int_alu`, `int_simd`, `fp16`) were rerun sequentially: all-architecture CPU
validation passed 3,101 tests with 138,035 shared cases in 48 s; SM89/SM90 Ghidra
passed 814 tests with 31,888 cases in 42 s; SM89 shared GPU validation passed
7,999 scenarios in 25 s (44 model-only exclusions). The planner/adapter regression
suite passed 383 tests. The memory cases and whole-kernel fixtures were unchanged
by that final corpus edit and retain the preceding validation.

All ten saved plans reproduce with `tests.autocases --check`. The final committed
coverage and recorded SM89/SM90 observations passed ten artifact checks.

## Report comparison and limits

Compared with the starting revision, SM89 gains five GPU-evidenced classes and
1,080 GPU-evidenced modifier combinations. SM90 loses ten GPU-evidenced classes
and a net 133 combinations across FLO, HADD2, HFMA2, LDG, LDL, LDS, MOV and
VABSDIFF4: the replacement case selection has not run on SM90. Raw SM90 observations
are preserved; these counts should be restored by running the current cases on
that device rather than transferring SM89 evidence.

Removing HADD2's dense manual widening matrix also reduces recorded exact-form
reference evidence by 37 combinations on SM86 and 38 each on SM100, SM101, SM103,
SM107 and SM120. The generated selection covers the configured three-way
requirements, preserves the migrated numerical boundaries and passes the shared
checks. It does not retain every exact tuple from the old exhaustive manual matrix.
SM75/SM80 gain HADD2 reference forms; SM89/SM90 have additional hardware/fixture
evidence. This is a selection tradeoff, not an exhaustive-verification claim.

The report also flags lower modeled combination counts for LD, LDG, ST and STG.
Their value semantics are unchanged: the corrected table correlations remove
later aliases that cannot be the decoded tuple for their raw field. Atomic
instructions' opaque inventories shrink for the same reason. The opcode badge
is unchanged.
