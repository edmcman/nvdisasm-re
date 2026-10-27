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
- `cxx.instruction` lowers one `Semantics` to `SASS_HD static void f(sass::Ctx &)` in `evaluate.apply`'s
  order: guard, effects, every write, store and branch evaluated against the pre-state, then the
  writes, the stores and the PC. Widths up to 64 bits live in `uint64_t`, up to 128 in
  `unsigned __int128`, every bit above the width zero. Pure nodes are computed once and eagerly;
  an `ite` whose arms reach a primitive keeps its laziness (SHFL never reads an invalid lane). Memory
  controls are checked when the code is generated (`runtime.memory_controls`), since they are
  encoding constants. A form the lowering lacks becomes that instruction's fault (`not compiled`).
- `codegen.kernel` makes one translation unit per kernel: a function per instruction word, a
  dispatch table, EXIT flags and the decoder's message for undecodable words. `build.library`
  compiles it into `CACHE/emulate` keyed by the source, the runtime headers, the compiler and the
  flags (`-O2 -ffp-contract=off -fno-fast-math -frounding-math -msse2 -mfpmath=sse
  -DSASS_FP_SOFT`). Required flags follow `SASS_CXXFLAGS` so custom options cannot silently
  override strict arithmetic or switch the emulator's backend.

## Primitives and floating point

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
leading bit, far above the jammed bit. Its namespace is `sass::softfloat`; comparisons order
encodings directly, with NaNs unordered and both zeros equal, so the reference also compiles
on CUDA devices.

### Floating-point backends (2026-10-27)

`include/sass/fp.hpp` selects `sass::fp_cuda` in a CUDA device pass (`__CUDA_ARCH__`),
`sass::softfloat` when `SASS_FP_SOFT` is defined, and `sass::fp_host` otherwise. The
`sass::fp` alias preserves generated calls and their templates: add, multiply with a signed
power-of-two scale, FMA, convert, comparisons and NaN classification at widths 16, 32 and 64.
Arguments and results are integer IEEE encodings. Arithmetic NaNs are positive canonical
quiet NaNs; conversion NaNs keep the source sign and use the destination's canonical payload.
`fp_common.hpp` owns `Rnd` (RN=0, RZ=1, RP=2, RM=3) and `SASS_HD`.

The emulator still builds with `SASS_FP_SOFT`. The C ABI hook `sass_fp` and Python
`native.fp` explicitly use the reference. The ABI-identical `sass_fp_host` and
`native.fp_host` expose the host implementation for comparison without switching the emulator.

The host backend requires x86 SSE2, IEEE subnormal handling, dynamic rounding support
(`-frounding-math`), no fast math and no contraction. Every native arithmetic operation
establishes the requested rounding mode, including RN, and restores the caller's mode.
The scope reads the current mode and calls `fesetround` only when it differs from the
requested mode; restoration likewise happens only after a change. It does not cache
process or thread settings, so external changes and nested scopes remain visible.
FP32/FP64 use native add/multiply and `std::fma`. Scaled multiplication first tries to
adjust either normal operand's exponent exactly, retaining a normal result, then performs
one native multiplication in the requested mode. This preserves single rounding even when
the unscaled product would underflow or overflow. If neither operand can be rescaled this
way, `fp_multiply.hpp` normalizes both significands exactly, including subnormals, and
redistributes their combined exponent plus the scale between two normal operands.
One native multiplication then rounds the final scaled value, including subnormal results.
Writing the normalized operands as `sa*2^ea`, `sb*2^eb`, their significand product lies
in [1,4). For combined exponent `e`, the only nontrivial rounding range is
`minimum-M-2 <= e <= bias`. Assigning exponents `max(e, minimum)` and
`e-max(e, minimum)` keeps both operands normal in that range. More extreme shifts have
known overflow/underflow results according to mode and sign. Exponent arithmetic uses
64 bits for every signed-int scale. CUDA needs no exact 106-bit product or double intermediate.
On the host, prepared operands whose combined exponent is below the normal minimum instead
use a specialized integer product rounded on the subnormal grid. This avoids the measured
SSE tiny-result penalty. FP32 needs a 64-bit product, FP64 a 128-bit container for its at-most
106-bit product; guard/sticky rounding also handles a carry into the first normal binade.
The prepared exponents bound the shifts. Hot host multiplication, preparation and rounding
scope construction are explicitly inlined to avoid calls and returning structs through memory.

FP16 host add/multiply use exact double intermediates and specialized integer narrowing
in `fp_convert.hpp`, without changing the caller's rounding mode. Half widening constructs
the exact double encoding; narrowing needs only 64-bit integers and also handles scaled
half products directly, including extreme scales.
FP16 FMA widens exactly, forms an exact double product, then uses RN TwoSum to recover
the exact sum's residual. If it is nonzero, the residual's sign determines whether the
RN sum overshot in magnitude; that case moves one double step toward zero. Setting
the low significand bit produces a round-to-odd intermediate, which the specialized
converter rounds once to FP16 in the requested mode. This retains tiny products beside large
addends without double rounding. The intermediate calculation establishes RN regardless
of the caller's mode, with contraction disabled. Exact-zero signs and exceptional
values are handled separately. FP32↔FP64 conversions use native widening or directed
narrowing. Same-width conversions preserve the encoding except for signed canonical NaNs.
The host and CUDA backends neither include nor call softfloat; it remains the emulator's
selected backend and the independent reference for tests.

The CUDA backend uses explicit rounding intrinsics (RP maps to `_ru`, RM to `_rd`),
directed double-to-float conversion and the same scaled multiplication strategy.
FP16 RN uses `__hadd_rn`, `__hmul_rn` and `__hfma`. Directed half arithmetic uses FP32
rounding intrinsics followed by the same directed half conversion; composing roundings
in the same direction gives the final directed result. Finite half products are exact
in FP32, and finite half FMA values stay inside its normal range. Scaled half products
use the shared specialized integer converter.
CUDA conversions use `__half2float`, `__float2half_*` and `__double2half` (RN).
Directed double-to-half conversions compose directed double-to-float and float-to-half
conversions. NaNs are normalized explicitly.
The development dependencies include matching CUDA CCCL headers for `cuda_fp16.h`;
an old system `nv/target` is insufficient. Compile with C++20,
`-ftz=false` and `--fmad=false`; exclude `--use_fast_math`. `SASS_HD` annotates bit
helpers, floating-point functions and generated instruction functions, including
empty and fallback functions. These annotations prepare generated code for reuse;
the host machine, scheduler and primitive infrastructure still need a separate CUDA port.

After each runtime library load, `build.library` checks MXCSR bits `0x8040`
(FTZ/DAZ) and raises a clear Python error if either is enabled. It does not change
process settings. A consumer must arrange the IEEE environment before loading the
runtime and keep it while executing. The external sass2cu shim must perform the
same check at startup; its source is absent here, so this repository cannot install
that guard in the shim. F16C optimization, broader A.6 campaigns, the complete
emulator CUDA port and the A.7 backend switch remain separate work.

Focused normal-input benchmarks on 2026-10-27 measured host FP16 add/multiply/FMA at
about 7–19 ns per operation, roughly 1.5–2.5 times faster than CPU softfloat in RN/RM.
Ordinary scaled FP64 measured about 2.3 ns in RN and 11 ns in RM. With two subnormal
inputs and normal scaled outputs, the host path measured about 6 ns in RN and 11 ns in RM.
For subnormal scaled outputs, the integer path measured about 7–11 ns, versus 31–37 ns
for native multiplication and 14–18 ns for softfloat in the paired measurements.
On the SM89 RTX 4070 Laptop GPU, FP16 CUDA kernels were about 9–22 times faster than
device softfloat. CPU times divide a single core's loop time by its operation count;
CUDA event times cover 65,536 threads executing 256 operations each and measure aggregate
throughput. The loops include checksum and operand maintenance; these are backend
microbenchmarks, exclude GPU transfers, and do not predict whole-emulator performance.
Reproduce them with `nice uv run python research/fp_backends_host.py` and, outside the
sandbox, `nice env PYTHONPATH=. uv run python research/fp_backends_cuda.py`. The CUDA
script also prints aggregate Gop/s and saves its source, cubin and JSON results under
`CACHE/fp-benchmark` (or `--output DIRECTORY`). Both scripts compare result checksums
against the reference and include scaled FP64 with two subnormal operands producing normal
or subnormal outputs. These focused measurements do not establish the fastest implementation
for every operand distribution, compiler or CPU.

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
