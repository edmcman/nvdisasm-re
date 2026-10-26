# ATOMS.POPC.INC.32 (2026-10-25)

Investigation for [issue 75](https://github.com/edmcman/nvdisasm-re/issues/75).
The lane-scoped IR remains the chosen model. Support for the **returning** form is
deferred until a real compiler-generated use is found; a `read_init`/pre-instruction
snapshot primitive is a possible future extension, not an implemented interface.
The discarded-result form is present in compiler output and is a separate support question.

## Hardware finding

On an RTX 4070 Laptop GPU (SM89), the tested uniform-address form returns the **same old
counter value to every participating lane**, while memory becomes
`old + popcount(participating_mask)`, modulo 2^32. There is no lane-rank addition.
For a counter initially 10 and lanes 0, 2 and 5 participating, the results are
10, 10, 10 and memory ends at 13. This corrects the hypothesis in issue 75.

`observations.json` preserves 44 scenarios, and `controls.json` preserves four
ordinary `ATOMS.ADD` controls. The controls return distinct old values (10, 11, 12
for that sparse mask), establishing that the wrapper does not broadcast a single capture.
Each live lane captures its register result, final shared word, lane id and a marker;
inactive predicate lanes retain the seeded destination, and exited lanes leave their
output slots unwritten.

Scope: full, sparse, alternating, single-lane and zero predication masks; separately,
exited-lane masks; initial counters 0, 10, 0x7fffffff, 0x80000000, 0xfffffff0 and
0xffffffff; destinations R8 and RZ; offsets 0, +16 and -16; up to three sequential
executions. The address uses `Ra=RZ`, `UR2`, and the immediate offset.

These are **synthetic returning instructions and raw discovery observations**, not
reference-backed regression evidence. Nonzero/nonuniform `Ra`, competing warps, other
architectures' return behavior, and memory ordering remain untested. The capture inserts
`MEMBAR.ALL.CTA` after the target; observed final memory is not evidence for an intrinsic fence.

Run from the checkout, outside the sandbox:

```sh
nice uv run python -m research.popc_inc.probe --output /tmp/popc-inc
nice uv run python -m research.popc_inc.probe --controls --output /tmp/popc-inc-controls
```

The research wrapper reuses `Cases.probe`, native instruction construction and scheduling,
the cubin writer, and the isolated GPU runner. It combines shared-memory initialization
with warp masking because the existing adapters do not support both for ATOMS.
It uses no value-semantics implementation. Validate the recorded captures without a GPU:

```sh
nice uv run python -m research.popc_inc.probe --check research/popc_inc/observations.json
nice uv run python -m research.popc_inc.probe --check research/popc_inc/controls.json
```

## Compiler mapping

CUDA 13.0.88 `ptxas`, SHA256
`daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`;
CUDA 13.4.92 `nvdisasm`, SHA256
`fa719d636e2dcebdbce466e07ab69f144feabc2fdb3eda52380fbd4a5c189faf`.
`compiler_observations.json` preserves 128 successful compilations: eight variants,
SM80/SM89/SM90/SM100, and optimization levels O0–O3. SM100 used PTX 9.0; the
other architectures used PTX 8.0.

At O1–O3, a uniform shared address and a discarded return value select the target:

```ptx
atom.shared.add.u32 _, [address], 1;
// or:
red.shared.add.u32 [address], 1;
```

Both produce `ATOMS.POPC.INC.32 RZ, [URx]` (URZ for the zero static address).
`atom.shared.inc.u32 _, [address], 0xffffffff` also selects it.
At O0 these probes use ordinary ATOMS.ADD/INC.
When the atomic-add result is consumed, optimized code elects a lane to add the
warp population count, broadcasts its old value with SHFL, and adds each lane's
rank explicitly. The global-address controls use ATOMG/RED/REDG, not POPC.INC.
Separately, `mbarrier.arrive_drop.shared.b64` selected POPC.INC with Rd=RZ plus
ATOMS.ARRIVE.64 on SM80/SM89; the SM90 lowering used SYNCS.

PTX describes a per-thread atomic operation; each consumed destination receives
the value before that thread's update, without a specified order among threads.
Using `_` lets the compiler aggregate those increments without preserving individual
return values. This establishes a mapping for the **discarded-result** SASS form,
not a PTX specification for its synthetic returning form.

## Independent sources

- Dong and Pai, [*Modeling Utilization to Identify Shared-memory Atomic Bottlenecks*,
  §2 and §4.1](https://arxiv.org/html/2503.17893v1): describes the population-count
  update and compiler selection when the return value is unused, measured on an A6000.
- [Shared Memory Atomic throughput on RTX 4090](https://girl.surgery/shmem_atomic/):
  independent compiler examples and microbenchmarks.
- NVIDIA PTX ISA [`atom`](https://docs.nvidia.com/cuda/parallel-thread-execution/#parallel-synchronization-and-communication-instructions-atom)
  and [`red`](https://docs.nvidia.com/cuda/parallel-thread-execution/#parallel-synchronization-and-communication-instructions-red):
  atomicity and scope contract; unspecified `.sem` defaults to relaxed.

These sources corroborate the update and compiler usage, not a non-RZ destination's
old-value result. No acquire/release guarantees for the standalone SASS form are
claimed from the single-warp probes.
