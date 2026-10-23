# Emulation

Three engines run SASS on a CPU, all from the same lifted semantics (`sass.semantics.lifter`'s
bitvector IR) and the same runtime primitives:

| Engine | Module | Runs | Use |
|---|---|---|---|
| Ghidra | `sass.ghidra_bridge.ghidra_emulate` | the generated SLEIGH p-code in Ghidra's emulator | checking the languages |
| reference | `sass.emulate.reference` | `lifter.lift` + `evaluate.apply`, in Python | the definition; no Ghidra |
| native | `sass.emulate.kernel` | C++ generated per kernel (`codegen`, `cxx`), compiled on first use | speed (equivalence testing) |

The goal of the native engine is equivalence testing: running two kernels (for example a cubin and
ptxas's SASS for a translation of it) over very many inputs, often without a GPU.

## Whole-kernel requests and the shared scheduler (2026-10-23)

A kernel request (`sass.launch.kernel_request`) gets the same response from every engine:
`{outputs, threads, steps, visited, exit[, trace]}` or `{error}`. `sass.emulate.schedule` is the one
scheduler (moved unchanged from `ghidra_emulate.run_kernel`): seeding (`SR_TID.X`, `SR_CTAID.X`,
the launch's window registers), non-cooperative threads each alone with their own memory,
cooperative blocks whose warps take turns stepping the live lanes at the warp's lowest PC, BAR.SYNC
and WARPSYNC holds, the warp context collectives read (each lane's operand gathered before the
step), the step limit and every error message. Engines supply lanes (`schedule.Lane`: `pc`, `read`,
`seed`, `step`, `writes`). `docs/testing.md` records a one-time parity check (every fixture's whole
response identical across the engines) and the inherent differences.

## The native machine

- `include/sass/machine.hpp`: a thread's register file (R, UR, SR, P, UP and the PC), sparse
  256-byte pages per space (`ram`, `shared`, `localmem`, `cbank`) over a read-only launch image,
  copied on a thread's first write; the lane's warp context and the launch environment
  (synchronization handlers, BMOV values, QSPC ranges). Unmapped memory reads 0, as in Ghidra.
- `cxx.instruction` lowers one `Semantics` to `static void f(sass::Ctx &)` in `evaluate.apply`'s
  order: guard, effects, every write, store and branch evaluated against the pre-state, then the
  writes, the stores and the PC. Widths up to 64 bits live in `uint64_t`, up to 128 in
  `unsigned __int128`, every bit above the width zero. Pure nodes are computed once and eagerly;
  an `ite` whose arms reach a primitive keeps its laziness (SHFL never reads an invalid lane). Memory
  controls are checked when the code is generated (`runtime.memory_controls`), since they are
  encoding constants. A form the lowering lacks becomes that instruction's fault (`not compiled`).
- `codegen.kernel` makes one translation unit per kernel: a function per instruction word, a
  dispatch table, EXIT flags and the decoder's message for undecodable words. `build.library`
  compiles it into `CACHE/emulate` keyed by the source, the runtime headers, the compiler and the
  flags (`-O2 -ffp-contract=off -fno-fast-math`).

## Primitives and floating point: one implementation

The runtime primitives (`sass.semantics.primitive`) are what the IR cannot express: other lanes'
values, the environment's state, hardware approximations. `include/sass/primitives.hpp` is their
only implementation, warp-wide (32 lanes' inputs and a `need` mask of the lanes whose results are
used). The native kernels call it inline; `sass.semantics.runtime` (Python, so the reference engine
and Ghidra's userops) parses its context and calls the same code through `sass.emulate.native`. MUFU
SIN/COS is `mufu.hpp` (the recovered SM89 model); `tests/refs/mufu.py` stays the independent
reference.

`include/sass/softfloat.hpp` is the IR's floating point (`softfloat.py`: IEEE 16/32/64, round once in
RN/RZ/RP/RM, FMUL's power-of-two scale, canonical quiet NaN that the IR's `nanresult` replaces by the
GPU's). Add and FMA keep exact terms in 128 bits, with a jamming right shift: inputs carry at most 106
significant bits and are normalized to bit 125, so a shift that loses bits cancels at most one
leading bit, far above the jammed bit. Hardware fast paths are not used yet.

## Models

- Model 1, single thread (`include/sass/run.hpp`, 2026-10-23): non-cooperative launches. Each
  thread runs alone from the entry to EXIT over its own copy-on-write memory; threads run in parallel
  chunks (`kernel.Kernel.run`), and the first failing thread's error is the response, as the
  scheduler's sequential order gives. Collectives fail as in the other engines, without a warp context.
- Model 2, multi-thread sequentially consistent (`include/sass/run.hpp`, 2026-10-23): cooperative
  launches, a function-by-function port of `schedule.py` (lowest-PC turns, BAR.SYNC holds and the
  all-waiting release, WARPSYNC groups, collectives' gathered operands, the waiting mask). A block
  shares `ram`/`shared`/`cbank`, each thread keeps its own `localmem`, and blocks run in parallel
  chunks (global memory is per block, as in the other engines). The scheduler's static decisions
  (`schedule.operand_location`, `sync_form`) are made once in Python and emitted per instruction, so
  the C++ only executes them; its outcomes are formatted with `schedule`'s messages.
- Model 3, scoped weak memory: planned after the atomic and fence families (`docs/open.md`).

## Equivalence testing

`sass.emulate.equivalence.differences(a, b, launches)` runs two kernel images
(`cubin.kernel_image`) on the same launches (`equivalence.Launch`: parameter values, memory regions,
output layout, grid, context), each through its own `launch.kernel_request` (so each architecture
gets its bank-0 layout), and yields a `Difference` per thread whose outputs differ (`same` may relax
the comparison), or per launch where only one side failed or they failed differently. `equivalent`
is whether there is none. `test_equivalence` checks a kernel against itself and against itself with
its instruction under test replaced by a NOP.

## Verification

`test_emulate_fp` (C++ against `bitvec.OPS` on special-value products, random encodings,
cancellations and ties), `test_emulate_primitives` (against `refs/primitive.py`), `test_emulate_ops`
(random unsimplified IR trees over every operator, widths 1-128, against `evaluate`),
`test_emulate_cases` (the shared case stream as compiled fragments, against the references; every
SM89 class passes, 2 minutes) and `test_kernels_native` (every SM89/SM90 fixture and the `c.kernel`
cases). These are opt-in (`-m native`), like the Ghidra and GPU tests: the default run keeps
`test_mufu` and `test_lifted_semantics`, which reach the C++ primitives through the Python runtime. Mutating the RN tie rule or an unsigned comparison fails these tests.

## Performance (2026-10-23, 32 cores, the `ffma` fixture kernel, 19 steps per thread)

| Engine | SASS steps per second |
|---|---|
| reference (Python) | 3,000-6,000 |
| native, one core | 47 million |
| native, all cores (262,144 threads) | 83 million |

Small launches are dominated by per-request costs (the image, output assembly in Python). The
next gains are structure-of-arrays registers with SIMD over inputs, flat per-thread memory instead of
hashed pages, and fewer Python-side copies.
