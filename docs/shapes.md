# Tensor shape inference

The implementation is in [`research/tensor_shapes/`](../research/tensor_shapes/README.md).
It infers kernel argument element types and conditional tensor dimensions from
SASS. The [HTML report](shapes.html) contains the results, constraint rules,
examples, and a worked proof for DOT's groups of four reads. The
[manifest](../research/tensor_shapes/common_results/manifest.json) records the
input contexts, cubin hashes, source hashes, tests, and per-entry results.

## Method

The analyzer builds def/use relationships and SSA, giving each definition its
own identity even when registers are reused. Structural type unification connects
loads and stores to their consumers. For example, a value loaded through `p` and
consumed by `FADD.F32` constrains `p` to point to `f32` elements.

A common equality/inequality analysis then propagates address and index facts:

- Parallel assignment substitutes all incoming SSA definitions together.
- Branch comparisons restrict each successor. Contradictory paths are unreachable.
- Merging paths keeps common equalities and bounds that cover every predecessor.
- A finite polynomial basis retains products such as `M*i`, where `M` is unchanged
  during the loop but need not be a literal constant.
- Each natural loop has an auxiliary counter, initialized to zero on entry and
  incremented on a backedge. Ordinary equality propagation derives relationships
  such as `index = start + 4*stride*iterations`.
- Integer range and congruence information strengthens bounds. Masks retain
  quotient/remainder relationships, for example `x = 4*(x >> 2) + (x & 3)` for a
  nonnegative integer view.
- Values selected by the same condition are analyzed together, preserving shared
  relationships. A one-bit true is `1`; zero extension keeps `1`, while sign
  extension gives signed `-1`.
- Widening weakens growing loop bounds to force termination. A final fixed-point
  check verifies that the result covers every successor transfer.

The basis includes expressions from addresses and comparisons, their SSA
preimages, and products with invariant parameters. Degree and feature limits
bound the analysis. Linear algebra derives equalities; Z3 bounds the polynomial
feature columns. Numerical constraints use fresh solver contexts and local
auxiliary names so earlier queries cannot perturb later proofs under a time limit.

Unknown operations and missing or ambiguous bounds remain unresolved. The analyzer
does not recognize kernel names, countdown loops, tile sizes, or unroll recipes,
and does not propose tensor layouts for a separate checker to approve.

## Recovering dimensions

Subtract the pointer base and divide byte offsets by the access width to obtain
element indices. For example:

```text
address = base + 4*(i + M*j)
0 <= i < M and 0 <= j < N
=> a sufficient access shape is A[M,N]
```

Dimensions follow physical element-stride order. Candidate extents come from
comparisons, constant coordinate ranges, and SM90 launch conventions. Inclusive
comparisons are normalized: `j <= 7` supplies extent `8`. Structural strides guide
the decomposition; whole-index bounds are also checked. Smaller comparable bounds
are preferred. When minimal bounds are incomparable, a comparison enforced by the
kernel is preferred to a launch-size expression.

Every coordinate of every access must be nonnegative and below its extent, and
the remaining quotient must be zero, before an argument is marked complete.

## Reciprocal arithmetic and DOT

DOT's compiler-generated division uses floating-point conversion, an approximate
reciprocal, integer refinement, and quotient corrections. Treating the reciprocal
as entirely unknown loses the bound needed to justify groups of four reads.
`numeric_relations.py` carries numerical constraints through the individual
operations instead of inserting a division summary for this kernel.

Conversion metadata preserves the rounding mode alongside expanded bitvector
expressions. The model handles positive-normal float encoding adjustments and
conversion back to an integer. It assumes the documented one-ulp reciprocal
accuracy applies to the SM90 SASS primitive; an ulp is the spacing between
neighboring floating-point values. NVIDIA's
[reciprocal specification](https://docs.nvidia.com/cuda/parallel-thread-execution/#floating-point-instructions-rcp)
and the repository's SM89 survey in `hardware-facts.md` support the accuracy bound.
Applying it to SM90 remains an explicit architecture assumption, not new hardware
verification or an exact reconstruction of reciprocal bits.

For positive stride `s`, initial index `g`, and extent `N`, the corrected quotient
satisfies `q*s <= N-g-1 < (q+1)*s`. Let `r=(q+1)%4`, `h=(q+1)//4`, and let `c`
count completed four-read iterations after the initial scalar reads:

```text
q+1 = r+4*h
index = g+s*r+4*s*c
index < N <= g+s*(q+1)
=> s*(c-h) < 0
=> c <= h-1
=> index+3*s <= g+s*q <= N-1
```

The positive-factor cancellation uses the linear polynomial-feature solver;
integer rounding supplies `c <= h-1`. This bounds the fourth read and therefore
all preceding reads. The loop condition alone would bound only the first read.

Possible zero divisors, unsupported rounding modes, exceptional floating-point
ranges, and solver timeouts supply no numerical fact. Real relaxations of floor
and modular operations can lose precision. Boundary tests ensure that short valid
inputs are admitted, including when intermediate word arithmetic wraps.

## Validation and limits

After the 2026-11-06 cleanup, all **91 current-analyzer tests pass**, without
failures, errors, or skips. The separate corpus pass retains **28 of 28 complete
conditional tensor argument shapes across all 12 entries**, with every coordinate
bound and fixed point verified. Recorded source hashes match the cleaned files.

The recorded corpus comprises 12 SM90 entries and 28 tensor arguments. It includes
DOT's `x[N]`, `y[N]`, `partial[grid.x]` and triangular solve's `L[8,8]`, `X[8,8]`.
The manifest and HTML report give the current measured pass counts and results.
The suite tests the active analyzer, including concrete SASS address and scalar
range checks, reciprocal boundaries, joins, symbolic strides, local calls,
shared-memory types, and all four formerly unresolved arguments.

All dimensions remain conditional on the integer interpretation of machine
arithmetic and the explicit input contexts. Overflow, signed extensions, and
address wrapping still require a complete connection to the bitvector semantics.
Recovered access dimensions do not establish original source rank or allocation
capacity. Both `integer_interpretation_proven` and `source_tensor_rank_inferred`
remain false.

The extra loop and word relationships increase inequality-projection costs even
for previously resolved kernels. The prior full run took roughly 15 minutes:
DOT partial about 206 seconds, naive GEMM 138, GEMV 88, tiled GEMM 37, and triangular
solve 20. Profiling located the cost in projection and loop joins. This is a
precision/runtime tradeoff rather than a performance improvement.

## Reproduction

Follow the [runner instructions](../research/tensor_shapes/README.md). The runner
needs Python 3.13, lz4, and Z3 5.3.3; it needs neither a GPU nor the earlier analysis
toolkits. Tests and corpus runs use `nice`. Logs update during the test phase.
