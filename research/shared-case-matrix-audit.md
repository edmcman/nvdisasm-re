# Generated comparison matrix audit

2026-10-18. Scope: the current generated HSET2/HSETP2 plans on all ten
architectures, in exact and readable modes. The findings below describe the original eight-input matrix. The reduction
was subsequently applied as described in the implementation section below.

## Findings

Every currently generated class/modifier form still needs execution coverage. Operand
layouts, source swizzles, comparison selectors, boolean combiners, predicate
inversion, output encodings and FTZ are independently capable of binding or
lowering incorrectly. What is excessive is applying the same eight input
scenarios to every point in that product.

The plans contain 285,696 distinct class/form entries for these two opcodes.
Across both lowerings, their eight-input profiles produce **4,571,136 cases per
engine**, or 9,142,272 comparisons across Python and Ghidra.

- **1,523,712 cases (33.3%) mask the numerical comparison.** AND with an effective
  false predicate and OR with an effective true predicate produce constant
  outputs. A few such cases are useful boolean/predicate checks; the full
  numerical corpus adds no discrimination there.
- **1,904,640 cases (41.7%) cannot observe the numerical operands**, including
  the above masking cases and the constant F/T comparison selectors. This
  includes useful output and predicate checks, so it is not a count of cases
  that can all simply be deleted.
- On SM89 each opcode has 14,976 class/form entries but only 3,456 distinct
  numerical/boolean configurations. The remaining multiplicity tests operand
  layouts. Those layouts should remain covered; each does not need an entire
  exceptional-value corpus.
- **22,080 exact-mode FTZ form entries have no FTZ-sensitive witness in the
  current eight inputs**, although one is available by changing just the
  predicate value on an existing input. Predicate values are tied to the
  numeric seed's parity, which masks all FTZ-sensitive seeds for some forms.

The last finding is a gap in generated per-form checks. Existing dense manual
comparison tests still check FTZ on representative register forms; this audit
does not claim the instruction implementation is incorrect.

## Concrete missing witness

Consider SM89 HSET2.LT.AND.BM.FTZ with both sources using H1_H0 and the
predicate input inverted. One existing seed has:

- A = 0x80000000 (low +0, high -0).
- B = 0x00018001 (low negative subnormal, high positive subnormal).
- Raw P0 = 1, so !P0 = false.

AND masks both comparisons. Setting raw P0 to 0 instead exposes them:
the independent reference gives 0x00000000 with FTZ and 0xffff0000 without
FTZ. This distinguishes a broken FTZ implementation using the same numeric
inputs. Both expected results were also checked against Python and Ghidra.

## Quantified reduction proposal

The audit searches all 255 nonempty subsets of each form's existing eight
scenarios. It retains both raw predicate values, and therefore both separate
and input-aliased HSET2 destinations. It also preserves every currently
observed distinction against reference-level fault models:

- A wrong comparison selector or boolean combiner.
- A wrong source-lane swizzle, swapped sources, or swapped output lanes.
- Predicate inversion or a predicate stuck at either value.
- A wrong output mode, floating format, or FTZ setting.

This gives **1,935,834 cases per engine**, a **57.7% reduction** for these two
opcodes while keeping every form and lowering. Most form/lowering entries
need three or four inputs:

| Selected inputs | Form/lowering entries |
| --- | ---: |
| 2 | 12,848 |
| 3 | 358,386 |
| 4 | 167,238 |
| 5 | 31,492 |
| 6 | 1,428 |

Adding one newly exposed FTZ witness for each affected form would bring the
proposal to at most **1,957,914 cases**, still **57.2% fewer** than the current
matrix. Other missing distinctions should be checked before applying a
selection policy.

These counts preserve the distinctions of the stated fault models, not every
possible future bug. They are not source-mutation test results or a proof of
identical bug detection. In particular, cases should not be collapsed merely
because they currently have the same expected output. Architectures, operand
layouts and exact/readable lowerings stay separate.

## Recommended change

Keep all currently generated forms, but select a small set of discriminating inputs for
each form. Decouple predicates from numerical seeds so masking cannot hide
FTZ and comparison boundaries. Keep the dense numerical edge corpora on
representative forms, with explicit interaction tests for NaNs, subnormals,
swizzles and predicate modifiers. Add checks that the reduced profile retains
the intended fault witnesses and exposes FTZ wherever the chosen inputs can.

The approved follow-up applies this as a separate profile change and reruns
the affected Python/Ghidra matrices with refreshed evidence.

## Reproduce

From the repository root:

    PYTHONPATH=. .venv/bin/python -m research.audit_shared_cases OUTPUT.json

The script reads saved-plan files and the declarative half input corpus.
All numerical/boolean results come from independent test references; it does
not use implementation outputs. It caches equivalent logical configurations
only for audit computation, never to skip an execution case. The companion
shared-case-matrix-audit.json contains per-architecture totals and examples.


## Implementation

The approved reduction uses a named half_comparison_forms profile. Its
input-only table, tests/harness/data/half_comparison_inputs.json, stores
(seed index, raw predicate value) pairs for 10,368 logical configurations.
The runtime adapter continues to calculate expected outputs using independent
references; neither expected outputs nor implementation observations are stored
in the table.

All 285,696 generated class/form identities remain, in both lowerings. Including
22,080 new unmasked FTZ witnesses, the comparison matrix now contains
**1,957,914 cases per engine**, a **57.2% reduction**. Dense manual exceptional
comparison corpora are unchanged.

Regression checks cover all table configurations: every previously observed
reference-level fault distinction remains detectable; both raw predicate values
and HSET2 destination placements remain; and each exact FTZ configuration gets
a distinguishing witness whenever one exists in the same numeric corpus.
Table regeneration is checked, and corpus changes cause a stale-table error.

To regenerate the input table and the original baseline audit:

    PYTHONPATH=. .venv/bin/python -m research.audit_shared_cases OUTPUT.json --write-inputs tests/harness/data/half_comparison_inputs.json

The replacement validation executes the reduced suite on every architecture
and lowering. Existing Ghidra result caching reuses identical encoded requests;
new FTZ scenarios execute normally. No comparison tolerances or native guards
were changed.
