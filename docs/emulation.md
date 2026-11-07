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
`{outputs, threads, steps, visited, exit[, trace]}` or `{error}`. Since 2026-11-03,
all engines use the C++ scheduler in `include/sass/run.hpp`. Native instructions execute directly;
Python reference and Ghidra lanes use a C callback adapter. `schedule.py` supplies request
validation, decoded instruction facts, and diagnostics; it contains no scheduling loop.

`scope="launch"` is the default for every engine: global (`ram`) storage is shared across the
launch, shared storage is private to each block, and local storage is private to each thread.
`scope="block"` isolates global memory by block; `scope="thread"` runs isolated threads without
warp collectives. Ghidra routes global memory through one launch state, shared memory through
each block's state, and local memory through each thread's state.
The removed `cooperative` request field raises a migration error. Replace old `False` with
`scope="thread"` and old `True` with `scope="block"`; remove the positional boolean from
`kernel_request` calls and pass scope by keyword.

Launch execution is sequentially consistent with all blocks resident. Each round visits every
block's warps once, executing the live lanes at their lowest PC in ascending lane order.
`schedule="reverse"` reverses block and warp visitation; `"round_robin"` is the default.
`"seeded"` shuffles both orders reproducibly using `schedule_seed` (default `0x5a55`).
Alternate schedules require launch scope. BAR.SYNC holds remain block-local, and WARPSYNC
membership and collective operands remain warp-local. Outputs are read after every thread exits.
A launch uses one scheduler invocation regardless of host worker count; independent requests
can execute concurrently. Memory resets for each request.

This stronger visibility model does not validate CUDA weak-memory protocols or GPU residency
requirements. Supported ATOMG transactions and MEMBAR fences run in launch scope. Other atomics,
MMIO and external/system scope accesses remain rejected. BREAK and BMOV remain rejected.
Other unsupported instructions retain their runtime errors; synchronization context
flags cannot enable rejected ordering operations. Polling may exhaust the per-thread instruction
limit; when no warp can progress the scheduler reports `no thread can proceed` instead.

### Operand groups, barrier results, and async copies (2026-11-03)

Warp context retains the scalar `values` and `predicates` columns and adds
`operand_groups`: named groups of 32 rows, each row holding that operand's
register words. The scheduler captures every participating lane before any lane
writes. LDSM uses address groups, including uniform operands; direct HMMA uses
Ra/Rb/Rc fragment groups. Indexed HMMA operands remain unresolved.
The large snapshot storage is referenced by the context, rather than copied
at every instruction. The native and Python callback ABIs carry the same
groups. A standalone instruction must supply its explicit warp context.

On 2026-11-04 the shared scheduler also models ordinary BAR.SYNC results, counted
generation completion between warp turns, and nonblocking BAR.ARV. Barrier
arrivals retain their predicate values independently of later register writes.
Blocking and deferred participants receive completed results; earlier ARV
participants retain their prior result, and the completing warp receives the
new result. Cooperative kernels start with result zero.

B2R.RESULT reads that result and sets Pu when it is nonzero. B2R.WARP reads its
low eleven bits. B2R.BAR reads a selected packed barrier state: operation tag,
explicit expected warp count (zero for implicit counts), accumulated predicates
(false for AND, true otherwise), and arrived warp count. Completed states clear.
Python, native and Ghidra callbacks receive the same sixteen state words,
snapshotted only for instructions that read BAR state.
Standalone instructions require explicit `barrier_result` or sixteen
`barrier_states`; missing context fails explicitly. Packed-register BAR producers
and overlapping arrival generations remain unresolved.

Each lane owns an async-copy queue in the common C++ scheduler. LDGSTS snapshots
the source value when issued; LDGDEPBAR commits the open group to its encoded
write scoreboard, including an empty group. DEPBAR.LE completes all but the
requested number of most recent groups on that scoreboard. Scoreboard masks
and encoded instruction waits complete the named groups. Completion writes
shared memory before the waiting instruction executes. Ordinary stores remain
immediate; BAR alone does not complete an async copy. Python and Ghidra callback
lanes use the same queue through the scheduler ABI.

This is a deterministic completion policy under SC. It does not model ordinary
instruction latency or claim a complete hardware scoreboard simulation.

On 2026-11-04 ordinary CCTL.D.PF1/PF2 address forms record cache requests;
SM80 and newer also support PML2/DML2. The shared scheduler owns the request
state, with one global state in launch scope. Python and Ghidra callbacks
use that same state. Requests complete immediately under SC and preserve
memory. The model does not simulate physical cache contents, residency,
eviction timing or performance. DML2 requires 128-byte alignment.
The hardware-established CCTL.D.IVALL form records a whole-cache invalidation
and advances an internal generation for each enabled request. Its input
predication gates both transitions. Descriptor operands, predicate results,
other cache selectors, other whole-cache operations and RML2 discard remain unresolved. Standalone evaluation records
an explicit event; native callers can provide a cache state to inspect requests.
Unsupported environment controls still fail explicitly. Atomic sources, old
memory, and returned values are captured before the instruction commits any
register or memory writes; scheduler turns never interleave inside the transaction.

Since 2026-11-03, BSSY snapshots enabled participants in a warp-local convergence ID.
BSYNC holds arrivals until the remaining live participants arrive; exited lanes are removed.
IDs can nest and be reused after completion. BSYNC resumes at its next instruction, even
when BSSY's target is farther away. Since 2026-11-06 the ordered bodies capture
membership and issue explicit entry, arrival, wait and release operations through
the shared coordinator in native, Python and Ghidra execution. The scheduler no
longer performs instruction-specific convergence actions. Isolated instruction
evaluation still requires explicit context.

BAR.SYNC generations are block-local and keyed by ID. Immediate/register IDs and
immediate/register counts are supported when they are separate operands; explicit counts must
be multiples of 32 within the launched block. Omitted counts include all non-exited threads.
The form encoding ID and count in one shared register remains explicitly unresolved.
DEFER_BLOCKING records arrival and permits coupled instructions to proceed; subsequent
decoupled instructions wait for that generation. The SM89 timing probe establishes this
boundary for LDS, MUFU, S2R and BRA, against a passing CS2R/MOV control. Other decoupled classes
use the same machine-description classification, an inference beyond those tested forms.

Kernel arguments follow the cubin's parameter order, sizes and offsets.
`launch.serialize_arguments` accepts unsigned integers, encoded little-endian at the declared
size, or `bytes`, `bytearray` and `memoryview` payloads of exactly that size. Aggregate arguments
can use raw bytes (for example, a 12-byte struct); signed integers and floating-point values
use explicitly packed bytes, such as `struct.pack("<i", -1)` or `struct.pack("<f", 1.5)`.
Missing or extra arguments, negative or overflowing integers, wrong payload sizes and unsupported
types are rejected. Packing preserves alignment gaps and neighboring constants (2026-11-03).

Launch geometry (2026-11-03): `kernel_request(image, arguments, memory, threads=None,
block=32, output=None, context=None, *, grid=None, scope="launch", schedule="round_robin")` accepts integers or
one-to-three-element tuples/lists for `grid` and `block`; omitted axes are 1. For example,
`grid=(2, 3), block=(3, 3, 5)` launches 270 threads. Explicit geometry derives the count;
a supplied `threads` must agree. Dimensions are positive uint32 integers and the total
count must fit uint64. Explicit requests carry normalized `kernel.grid` and `kernel.block`
triples alongside `threads` and `block_size`.

Without `grid`, the requested count is retained and an X-only grid is inferred by ceiling
division. Scalar calls keep their legacy request shape, including callers that edit
`kernel.threads`; a final emulator block may be incomplete. Both blocks and their threads
are enumerated with X varying fastest, then Y, then Z. Output records, traces and error
thread numbers use that block-major linear order. `SR_LANEID` is the block-local linear
thread rank modulo 32.

Interacting blocks may end in a partial warp. Warp groups restart in every block;
active/waiting masks and WARPSYNC membership include only existing lanes. BAR.SYNC
still waits for every live thread in the block. Barrier scope remains the block.

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
  flags (`-O2 -ffp-contract=off -fno-fast-math -frounding-math -msse2 -mfpmath=sse`).
  Required flags follow `SASS_CXXFLAGS` so custom options cannot silently override strict
  arithmetic. `SASS_CXXFLAGS=-DSASS_FP_SOFT` selects the reference backend; its libraries
  have different cache keys from the default host libraries.

### Generated operator calls (2026-10-27)

`codegen.fragments`, `codegen.semantic_fragments` and `codegen.kernel` call N5's pure
defined-operator functions in `sass::def` by default. Each translation unit emits only
the requested forms, once per signature, before the instruction functions. Operand
widths determine the registered form and its `Bits<W>` arguments; operand-count
variants use overloads. Nested definitions inside these functions remain expanded
inline. Instruction operands still read the pre-state, and primitive arguments in an
`ite` remain lazy. Guards still precede effects and value evaluation.

Each instruction's functions are validated before its uses join the translation
unit. An unsupported expansion produces that instruction's `not compiled` fault;
its partial uses cannot leave invalid functions in the library. Functions must be
pure, have bound parameters and resolved selectors, and fit in 128-bit values.
Pass the trailing `definition_calls=False` option to any of the three generators
to expand definitions inline for comparison or troubleshooting. `machine.Fragments`
and `kernel.Kernel` expose the same option. Direct `cxx.instruction` and `cxx.Lower`
retain inline expansion unless given a definition-use collector.

## Primitives and floating point

The runtime primitives (`sass.semantics.primitive`) are what the IR cannot express: other lanes'
values, the environment's state, hardware approximations. Their signatures, context, state changes
and ordering are listed in the [runtime reference](runtime-primitives.md).
`include/sass/primitives.hpp` is their
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

The native emulator defaults to `fp_host`. Use `SASS_CXXFLAGS=-DSASS_FP_SOFT` for
the explicit reference option. The C ABI hook `sass_fp` and Python `native.fp`
remain bound to softfloat independently of that selection. The ABI-identical
`sass_fp_host` and `native.fp_host` explicitly expose the host implementation.

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
The host and CUDA backends neither include nor call softfloat; it remains an
explicit emulator option and the independent reference for tests.

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
that guard in the shim. F16C optimization, broader A.6 campaigns and the complete
emulator CUDA port remain separate work.

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

### Native backend and generated-call benchmark (2026-10-27)

[`research/native_backend_benchmark.py`](../research/native_backend_benchmark.py)
compares all four host/softfloat and generated-call/inline combinations. Each workload
uses the same deterministic input image, 8,192 threads and 16 repetitions of its SASS
body, on SM89 exact semantics with one worker. Arithmetic covers packed FP16 and
FP32/FP64 add, multiply and FMA (including directed modes); conversion covers
FP16/32/64 and integer conversions. Integer work uses PRMT/IMAD/IADD3; cooperative
work uses SHFL, WARPSYNC and BAR.SYNC. The first warp is checked against the Python
reference using only its 1 KiB input buffer, and every full response is compared across
all four configurations before reporting comparisons.

Generation medians use three samples after warming MD/lifting discovery. Each cold
compilation sample uses a fresh temporary cache, and includes source hashing, compilation,
loading and MXCSR validation. Warm execution takes 15 samples (five per compiled library),
after an untimed warmup, including `Kernel.run`'s Python launch, memory setup and output
handling. The compiler is Clang 18.1.8 with the strict flags above. The CPU is an Intel
i9-13900HX, pinned to logical CPU 8, a performance core; configurations rotate between
repetitions. No other test jobs ran during measurement.

Warm execution medians, milliseconds:

| Workload | Softfloat inline | Host inline | Softfloat calls | Host calls |
|---|---:|---:|---:|---:|
| FP16 | 25.50 | 16.54 | 25.37 | 16.71 |
| FP32 | 14.36 | 7.93 | 14.16 | 8.06 |
| FP64 | 14.70 | 8.68 | 14.72 | 8.46 |
| Conversions | 11.39 | 11.41 | 10.95 | 10.78 |
| Integer | 5.60 | 5.45 | 5.46 | 5.46 |
| Cooperative | 20.70 | 20.54 | 20.49 | 20.76 |

Generation / cold compilation medians, milliseconds:

| Workload | Softfloat inline | Host inline | Softfloat calls | Host calls |
|---|---:|---:|---:|---:|
| FP16 | 18.10 / 758.9 | 18.25 / 936.3 | 18.70 / 756.0 | 18.50 / 959.0 |
| FP32 | 14.67 / 745.9 | 14.70 / 776.1 | 13.62 / 730.9 | 13.63 / 775.4 |
| FP64 | 18.38 / 772.4 | 18.64 / 812.2 | 15.16 / 754.8 | 15.46 / 762.6 |
| Conversions | 105.03 / 1024.5 | 106.03 / 1076.7 | 68.12 / 869.9 | 63.97 / 904.5 |
| Integer | 20.44 / 681.0 | 20.22 / 720.1 | 19.81 / 695.9 | 19.24 / 737.4 |
| Cooperative | 17.93 / 784.8 | 17.74 / 825.9 | 17.51 / 784.6 | 17.55 / 838.2 |

Holding generated calls fixed, the host backend improves arithmetic execution by
1.52× (FP16), 1.76× (FP32) and 1.74× (FP64). It adds cold compilation cost, most
visibly FP16 (959 versus 756 ms), because the selected backend compiles different
arithmetic implementations; this first-use cost is retained for faster repeated execution.
Conversion, integer and cooperative workloads establish no substantial backend gain.

Holding the host backend fixed, generated calls reduce conversion generation by
40% and compilation by 16%, with a 5.5% warm improvement. The conversion translation
unit shrinks from 397,104 to 77,745 bytes by sharing expansions. FP64 generation and
compilation also improve. Most other warm differences are within approximately 2%,
with overlapping sample ranges; there is no general runtime speedup claim for calls.
Small compilation costs remain for FP16/integer/cooperative kernels (about 1–3%),
accepted for the shared function interface and reduced duplication; inline lowering
remains available for consumers measuring a different workload.

The initial unpinned run showed apparent warm regressions, including integer differences
where the FP backend should have little effect, with sample ranges up to 2–3×.
Pinning the repeated run removed those large differences. The pinned
[complete samples](../research/native-backends-2026-10-27.json) and the
[unpinned diagnostic run](../research/native-backends-2026-10-27-unpinned.json)
retain all configurations, flags and output hashes. Reproduce the pinned run on this
machine with:

```sh
nice taskset -c 8 uv run python research/native_backend_benchmark.py \
    --out /tmp/native-backends.json
```

Choose a suitable performance core on other machines. These results describe these
workloads, this compiler and one CPU; they do not predict every kernel's performance.

## Models

- `scope="thread"`: isolated thread execution, parallel native worker chunks.
- `scope="block"`: interacting warps within isolated blocks, parallel native worker chunks.
- `scope="launch"` (default): interacting blocks sharing global storage under the SC contract above.
- Scoped CUDA weak memory remains planned (`docs/open.md`).

## Real-workload validation (2026-11-03)

`research/yolo_all.py` and `research/yolo_record.cpp` record ordinary eager YOLOv8n inference
through CUPTI and replay every launch independently against its captured GPU memory. The first
run used pretrained weights, a seeded random `[1, 3, 64, 64]` input, PyTorch 2.7.1+cu126 and an
SM89 RTX 4070 Laptop GPU. All 313 inference launches (27 symbols) were captured without reported
capture errors or unsupported launch APIs. The native executor matched every captured allocation
byte for 16 launches: five additions, two nearest-neighbor upsamplings, six arange launches and
three fills. The other 297 launches stopped explicitly: 167 at BSSY, 69 at
BAR.SYNC.DEFER_BLOCKING, 57 at unresolved MUFU.RSQ and four at unresolved MUFU.RCP. There were
no numerical mismatches or replay budget skips. These are first executed blockers; static
instruction inventories also contain missing tensor-core and asynchronous-memory operations.
The executor does not yet run the entire network. The retained summary is
`research/yolo-results-2026-11-03.json`; missing semantics are tracked in
[issue #117](https://github.com/edmcman/nvdisasm-re/issues/117).

Capture runs outside the sandbox. Use a separate environment with
`torch==2.7.1`, `torchvision==0.22.1`, and `ultralytics==8.3.0`:

```bash
nice c++ -std=c++20 -O2 -shared -fPIC -pthread -I/usr/local/cuda/include \
  research/yolo_record.cpp -L/usr/local/cuda/lib64 -L/usr/local/cuda/lib64/stubs \
  -Wl,-rpath,/usr/local/cuda/lib64 -lcupti -lcuda -o /tmp/yolo-record.so
nice /path/to/yolo-env/bin/python research/yolo_all.py capture /tmp/yolo-capture \
  --recorder /tmp/yolo-record.so --weights /path/to/yolov8n.pt --size 64
nice uv run python research/yolo_all.py replay /tmp/yolo-capture --max-threads 262144
```

The recorder serializes launches, saves loaded cubins and parameter bytes, and snapshots
parameter-reachable allocations (including candidate indirect pointers) before/after each launch.
CUDA memory and metadata queries run on a helper thread rather than inside CUPTI callbacks.
Unsupported launch forms, inaccessible allocations and capture limits become explicit errors.
Replay compares all captured allocations, including unchanged inputs; it records the first
execution failure per launch and retains budget skips separately. The capture is about 2.5 GiB;
raw cubins, weights and snapshots remain local. This validates individual launches from this
specific inference path, not alternate shapes, algorithms, dtypes, or concurrent execution.

## Equivalence testing

`sass.emulate.equivalence.differences(a, b, launches)` runs two kernel images
(`cubin.kernel_image`) on the same launches (`equivalence.Launch`: parameter values, memory regions,
output layout, grid, context), each through its own `launch.kernel_request` (so each architecture
gets its bank-0 layout), and yields a `Difference` per thread whose outputs differ (`same` may relax
the comparison), or per launch where only one side failed or they failed differently. `equivalent`
is whether there is none. `test_equivalence` checks a kernel against itself and against itself with
its instruction under test replaced by a NOP.
`equivalence.Launch` accepts the same optional `grid` and multidimensional `block`;
`threads` may be omitted when `grid` is supplied.

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

## Ordered-body backend checks (2026-11-05)

The ordered-body executors are exercised by `test_ordered`,
`test_ordered_native`, `test_ordered_schedule`, `test_ordered_ghidra`, and
`test_ordered_formal`, and `test_ordered_kernel`. Production Python and native
memory, cache, special-register, fence, global-atomic, control-flow, query,
asynchronous-copy issue/commit/waits, MUFU, SHFL, VOTE/VOTEU, MATCH, REDUX, LDSM,
and HMMA instructions now execute
direct ordered bodies alongside the converted numerical instructions. The shared callback scheduler
retains suspended Python frames, lets peers progress, and advances counts and
completed-instruction traces only on completion. The native kernel generator
uses ordinary functions for non-suspending instructions and a separate resumable
function table. Ordinary functions using runtime-owned state also share the
per-block runtime; they do not allocate execution frames. Kernel tests retain
snapshots through a later barrier and check
that an earlier register increment runs once under three schedules. The migrated
collectives capture their source values or predicates in their bodies and release
each participant's snapshot after use. Aliased SHFL, VOTE, and 64-bit MATCH loops,
and repeated REDUX, check reuse of the same instruction site across two warps
under round-robin, reverse, and seeded schedules. Native standalone fragments use
the same ordered function with an explicit warp-context provider. Synchronization
families still need migration.

LDSM and HMMA use typed retained word groups for one, two, or four source words
per lane (2026-11-06). Each lane supplies its own captured value. Indexed snapshot
reads validate the participant and word index. Standalone contexts are copied
when captured; modifying a context later does not change an existing snapshot.
The scheduler no longer scans register values, predicates, or matrix operands.
The native snapshot-word binding returns a typed value without constructing a
generic result record. Resumable native functions save their position at
suspending operations and execute the intervening statements directly.

LDGDEPBAR supplies its decoded dependency slot as an argument to the commit
operation. The callback adapter no longer sets an implicit commit slot. A native
test supplies an invalid slot in the warp context while exercising all six valid
IR arguments, then checks that arguments 6 and 7 fault.

DEPBAR executes distinct group-count and slot-mask waits in its ordered body.
The former validates the slot before changing copy queues; both write completed
copies at their statement positions. Native tests cover two queued groups,
another occupied slot, counts 0/1/2/63, all encoded slot values, and four masks.
Invalid slots preserve an earlier register write and leave queued copies intact.

Ghidra's executor advances the p-code cursor before calling a user operation.
`ghidra_bridge.ordered.Execution` retains that frame and the pending operation's
output varnode. On completion it writes the result and calls
`finishInstruction()`, preserving unique-space temporaries and the selected
control path. Reissuing `stepInstruction()` would restart instruction execution
and is therefore inappropriate for a suspended frame.
The execution wrapper exposes the frame's next p-code index to the shared
runtime so captures at different statement positions use distinct keys.
A regression interleaves two pending captures at indices 3 and 7 and verifies
their separate lane values and handle release.
Fresh instructions begin with `stepPcodeOp()` before `finishInstruction()`
(2026-11-06). Ghidra's `stepInstruction()` exposes its new frame only after
execution returns or suspends. Without the explicit begin, a lane completing
one capture immediately could enter a second capture without a visible cursor
and join a different group from resumed peers. Cooperative matrix fixtures
exercise this path in both modes under all three schedules.

Multiple results use one runtime-owned record handle, typed result projections,
and explicit record release. Atomic boundaries are separate user operations;
ordinary p-code loads and stores remain between them. The isolated thirteen-form
language, including explicit barrier-ticket release, occupies 10,647 decompressed
bytes in exact mode and 10,382 in readable mode (2026-11-06).
Its atomic read/update/write uses
five operations: two boundary calls and ordinary load, add, and store. The
decompiler retains the memory update between the boundary calls. This small
language measurement does not establish the sizes of migrated SASS languages.
Its numerical form passes captured operands to the existing numerical-definition
scratch registers; it uses 69 p-code operations in exact mode and 63 in readable
mode. A second output using that captured result adds two operations in either
mode, without repeating the numerical calculation.

Two-thread capture tests collect original operands before destination writes
and retain the snapshots through suspension. Python and Ghidra exercise
round-robin, reverse, and seeded orders; native tests exercise both lane orders.
Fault tests retain earlier writes and release atomic ownership.
Ready frames are polled before fresh instructions. A poll that makes no progress
lets another PC in the warp run, so a fresh lane's loop cannot starve a ready
frame. Capture identities use lane numbers within each block, including blocks
whose sizes are not multiples of 32.
The production lifted regression selected 973,986 shared cases across all ten
architectures and both FP modes (12,786 tests passed). Focused native
SM89/SM90 instruction and kernel regressions passed 1,731 tests. These checks
establish software agreement, not new hardware observations.

`nice uv run python -m tests.harness.ordered_perf` compares the existing native
lowering with an ordinary ordered function on the same two aliased assignments.
With Clang 18, nine alternating runs of 100 million iterations had medians of
0.058717464 s and 0.059027684 s, a ratio of 1.00528. The difference is within the
sample variation. This checks the small ordinary-function path, not whole-kernel
performance after migration. Source and samples are saved under
`.cache/ordered-gate/`.

A pinned comparison on CPU 8 (2026-11-05) used the original checkout and the
ordered checkout in separate processes and caches, checked identical outputs,
and alternated 13 batches of 200 calls per version. The ordinary barrier-result
and asynchronous-copy fixtures had candidate/original median ratios of 1.0180
and 1.0133 after specializing the scheduler for native execution without
callbacks. VOTE, MATCH, and REDUX loops remained at 1.3738, 1.4205, and 1.4007.
These residual regressions remain unresolved; no timing baseline was updated.
The comparison precedes the separate DEPBAR wait migration. Samples are in
`.cache/ordered-gate/collectives-performance-specialized.json`.

A later comparison using the same method (2026-11-06) included the matrix
fixtures. Outputs agreed for every sample. Candidate/original ratios were
1.0428 for the barrier-result fixture, 1.0310 for asynchronous copies,
1.4240/1.4521/1.4414 for VOTE/MATCH/REDUX, and
1.1758/1.1630/1.1967 for the F16 16816, F16 1688, and TF32 1688 matrix fixtures.
These regressions remain unresolved. A controlled source variant removing only
the new ordinary shared-runtime dispatch measured current/variant ratios of
0.9483–0.9979 across those eight inputs; the dispatch did not introduce a
slowdown in this comparison. Samples are in
`.cache/ordered-gate/shared-runtime-{performance,ablation}.json`.

Specializing generated functions to the concrete native runtime did not
materially improve matrix performance and slowed collective fixtures by 1–2%
in a pinned alternating comparison. The experiment was removed; source equality
with the saved pre-experiment files was checked. Samples are in
`.cache/ordered-gate/concrete-runtime-performance.json`. No timing baseline
was changed.

Typed snapshot-calculation bindings improved VOTE and REDUX by 2.8% and 4.2%
against an otherwise identical source copy in a pinned alternating comparison
(2026-11-06). Direct release bindings then improved VOTE/MATCH/REDUX by
2.0%/5.7%/2.8% and the three matrix fixtures by 1.6–1.9%. The same runtime
primitives compute the results; direct release still checks the opaque type and
rejects released handles. Python/native ordering, cooperative fixtures, retained
execution, and invalid-reply checks passed 378 tests. Samples are in
`.cache/ordered-gate/snapshot-{binding,release}-ablation.json`.

The final same-input comparison against the original checkout still measured
regressions: 1.0767 for the barrier fixture, 1.0424 for asynchronous copies,
1.3044/1.3176/1.2968 for VOTE/MATCH/REDUX, and
1.1684/1.1627/1.1873 for the three matrix fixtures. These remain unresolved;
no timing baseline was changed. Samples are in
`.cache/ordered-gate/snapshot-release-original-performance.json`.

Native operation arguments now use spans over fixed-size arrays, and common
result tuples use two inline slots with dynamic storage for larger tuples.
Completed captures retain their pending handle instead of allocating another
handle. The arena keeps 64 inline records and uses a map for occupied-slot
collisions; tests retain 257 snapshots simultaneously and reject released handles
after subsequent generations. Non-suspending kernels omit native execution
frames. `nice uvx py-spy` native profiles before and after these changes show
reduced allocation overhead; scheduler turns now dominate the collective run.

A same-input Python comparison ran 109,434 SM89 cases in both modes from the
original source and the ordered source. Worker process CPU time separated case
preparation from instruction work. Initially instruction work grew from
413.16 CPU seconds to 491.57: validation repeatedly expanded the same numerical
definition for different operands. Caching the state-access property by
definition and argument types reduced that phase to 230.70 CPU seconds. Case
preparation used 341.26 CPU seconds before and 256.05 after; wall times were
97.17 and 98.00 seconds, dominated by the longest case-preparation shards.
Both runs passed identical case counts. These measurements do not replace the
historical suite timing baselines.

After direct numerical migration, nine alternating runs of two million native
calls compared IADD3, IMAD, FADD and FFMA against the original generated
functions in the same translation unit. Functions were aligned to 64 bytes,
checksums agreed, and candidate/baseline median ratios were 0.9800, 1.0040,
0.9775 and 0.9904 after normalizing calculation capture. These are instruction microbenchmarks, not whole-kernel
measurements (2026-11-05). Source and samples are in
`.cache/ordered-gate/numerical-performance.{cpp,json}`.

A controlled coordinator comparison (2026-11-06) disabled only the new block
coordinator initialization and the warp-context pointer that enables exit
notifications. Thirteen alternating batches of 200 calls on the same pinned
CPU checked identical outputs before timing. Candidate/control median ratios
were 1.0063 for BAR POPC, 0.9967 for asynchronous copies,
1.0077/1.0074/1.0050 for VOTE/MATCH/REDUX, and
0.9954/1.0035/0.9927 for the three matrix fixtures. This establishes no clear
regression from these hooks; it measured the hooks before production BAR migrated
and does not resolve earlier whole-kernel regressions.
Samples and the saved control root are in
`.cache/ordered-gate/coordination-ablation.json` and
`.cache/ordered-gate/coordination-ablation-root.txt`.

After direct BAR migration, thirteen alternating same-input batches against the
original source passed output equality but exposed a regression (2026-11-06).
Candidate/original median ratios were 1.3228 for BAR POPC, 1.1504 for asynchronous
copies, 1.3386/1.3087/1.2957 for VOTE/MATCH/REDUX, and
1.1815/1.1610/1.1918 for the three matrix fixtures. The BAR fixture grew from
81.4 to 107.7 microseconds. Timing baselines were not changed.

Twenty-second `nice uvx py-spy` native captures of the same BAR fixture collected
1,996 samples each with no sampling errors. Allocation appeared in 68 original
samples and 142 ordered samples; scheduler turns appeared in 580 and 861.
The ordered capture includes ticket retention, separate pending-wait records,
frame storage, and polling. These inclusive counts are not additive and do not
isolate a single cause. They identify allocation and resumption work for the
next optimization; the regressions remain unresolved. Results and profiles are
`.cache/ordered-gate/bar-ordered-original-performance.json`,
`.cache/ordered-gate/bar-original-profile.raw`, and
`.cache/ordered-gate/bar-ordered-profile.raw`.

Selective pure LOP3 definitions introduce no measured native slowdown on
the four tested LUTs (2026-11-06). Thirteen alternating batches of 20 million
calls compare the inline expansion and named definition in the same compiled
library, pinned to the same CPU, with identical inputs and checked outputs.
Candidate/baseline median ratios for XOR (`0x96`), majority (`0xe8`), `0x17`,
and mux (`0xca`) are 1.001, 0.990, 0.981 and 0.993. Clang 18.1.8 inlines the
definitions. This comparison concerns the naming/sharing change, and does
not resolve the scheduler regressions above. Results and generated C++ are
in `.cache/ordered-gate/lop3-native-performance.{json,cpp}`.

Normal WARPSYNC now uses the shared coordinator from its ordered body
(2026-11-06). Arrival returns a typed retained generation; a separate wait
may suspend, and release removes only the original handle. Waiting records
retain the same generation directly, so releasing a ticket cannot cancel an
issued wait. Per-warp live and arrived masks replace repeated member scans
within each coordinator completion check. Completed generations remain ready
when later generations reuse the same members. The old scheduler's WARPSYNC
instruction classification and mask/arrival/release actions are removed.
Unsupported exclusive execution faults in its enabled ordered arm; false
Pg or Pp skips it.

Thirteen alternating batches on unchanged 32-thread immediate, register and
uniform-register fixtures initially measured candidate/pre-migration ratios
of 1.3031, 1.3037 and 1.3044. Sharing the generation directly between tickets
and pending waits reduced times by 2.8–3.5%; replacing repeated membership
scans reduced them by another 6.9–7.6%. Final same-input ratios are 1.2082,
1.2004 and 1.1992 (63.1–63.9 to 76.2–76.7 microseconds). This is an unresolved
regression, not a new timing baseline. Those controls already include the
ordered BAR and collective implementations, so they isolate the WARPSYNC
migration rather than the full refactor.

Moving new WARPSYNC dispatch checks after existing collective checks reduced
MATCH time from 95.1 to 92.9 microseconds in an identical-source comparison.
An eight-slot inline handle arena was also measured against the retained
64-slot arena, but increased the shared BAR/async/collective/matrix timings by
1.5–7.3% and was discarded. The retained arena size and performance baselines
are unchanged. Comparisons, source controls and correctness logs are under
`.cache/ordered-gate/warpsync-*` and `arena-shared-performance.json`.

The cooperative native runtime and its handle arena are shared per block.
Captures and waits belonging to different lanes can occupy records concurrently;
shrinking the inline arena therefore increases map use in collective fixtures.

Direct convergence migration (2026-11-06) initially measured candidate/control
ratios of 1.5588, 1.1810, 1.1672 and 1.4425 on unchanged nested, exited,
predicated and combined 64-thread fixtures. Retaining convergence generations
directly instead of allocating callable pending wrappers reduced the migrated
times by 1.4–2.5% on the same inputs. These costs remain under investigation;
performance baselines are unchanged. Raw comparisons are
`.cache/ordered-gate/convergence-performance.json` and
`convergence-pending-performance.json`.

A final alternating comparison against the pre-convergence source measured
ratios of 1.5256, 1.1699, 1.2062 and 1.4046; the raw samples are in
`convergence-final-performance.json`. Two immutable-snapshot scalar reduction
cache layouts were tested and discarded. The first reduced convergence timings
by 1.0–1.9% (`convergence-reduction-cache-performance.json`). The smaller layout,
tested against an otherwise identical source after validation jobs finished,
improved REDUX by about 3%, but a TF32 matrix fixture remained about 1% slower.
Its samples are in `snapshot-reduction-cache-final-idle-performance.json`.
The retained implementation calculates reductions from the captured values.
These experiments do not remove the larger migration regression.

After canonical interface cleanup (2026-11-06), a fresh comparison against the
original source used fifteen unchanged fixtures, thirteen alternating samples
of 200 calls per variant, pinned to CPU 8. Complete results matched on every
input. Candidate/original medians were 1.3181 for BAR POPC, 1.1089 for async copy,
1.3008/1.3490/1.3370 for VOTE/MATCH/REDUX,
1.1805/1.1532/1.1843 for the three matrix fixtures,
1.8239/1.3384/1.3400/1.8889 for nested/exited/predicated/combined convergence,
and 1.2199/1.2151/1.2166 for immediate/register/uniform WARPSYNC.
The convergence ratios above used a pre-convergence control; these new ratios
use the original source, so they are different comparisons. Regressions remain
open and timing baselines are unchanged.

Two 30-second `nice uvx py-spy` native profiles of the unchanged combined
convergence input collected 3,108 ordered and 3,141 original samples without
errors. The ordered generic operation dispatcher appeared in 20.8% of samples;
ticket and pending-generation allocations were also visible. Inclusive stack
counts overlap and do not apportion the full regression. Further investigation
targets dispatch and handle allocation while preserving statement positions and
retained suspension state. Samples, complete benchmark results, source hashes
and input provenance are in `.cache/ordered-gate/canonical-convergence-*` and
`canonical-runtime-original-performance*.json`.

Typed deferred-wait and convergence enter/wait bindings, plus arena-owned
interned type names, reduce some of this overhead (2026-11-06).
The arena shares its name storage when copied, preserving the lifetime of
retained objects after caller strings and the original arena are destroyed.
A tree-based name pool slowed VOTE and MATCH by 2.6–2.8% and was discarded;
the retained pool scans its small list of owned names.
Instrumentation of the four convergence inputs found no polls of unready
operations: nested/combined inputs each polled 370 times, including 154
convergence polls, and exited/predicated inputs each polled 122 times,
including 30 convergence polls. Avoiding unready scheduler turns therefore
does not explain or fix these inputs' regression.

The combined comparison against the original source, before the subsequent
typed-capture experiment, measured 1.2772 for BAR POPC, 1.1192 for async copy,
1.2732/1.3500/1.3068 for VOTE/MATCH/REDUX,
1.1627/1.1569/1.1643 for matrix,
1.6352/1.2807/1.3125/1.5966 for nested/exited/predicated/combined convergence,
and 1.2053/1.2000/1.2090 for WARPSYNC.
Complete results matched. All 444 combined native checks passed in
136 seconds, including cold generated libraries. A fresh SM90 exact source
comparison retained SHA-256
`873374b66ea5272a29b079a3765cb40dfa496bb064e56676858622e6a13dd840`;
these native bindings do not alter that language.
An anomalous incremental MATCH speedup did not reproduce: isolated tests
in both process orders instead measured about a 2.2% slowdown after the
convergence bindings.
Timing baselines remain unchanged. Diagnostics are in
`.cache/ordered-gate/native-runtime-optimized-original-performance.json`,
`type-name-*-performance.json`, `convergence-poll-counts.json`,
`match-binding-isolated-*.json`, and `native-optimized-*`.

Typed scalar, 64-bit and predicate capture bindings subsequently bypass the
generic dispatcher while retaining the same capture/poll implementation.
Default bindings still forward to a caller's operation handler; native
resumption still validates result arity, width and opaque type.
Twenty-one malformed-reply and suspension checks preserve the preceding write
and issue the operation once. All 465 combined native checks pass in
227 seconds with rebuilt libraries.
Isolated comparisons in both process orders reproduce a 3–4% MATCH gain
and a 0.6–1.5% REDUX cost relative to the pre-capture-binding source.
The full original-source comparison measured
1.2943/1.1025 for BAR POPC/async copy,
1.2578/1.3135/1.3088 for VOTE/MATCH/REDUX,
1.1610/1.1390/1.1668 for matrix,
1.6046/1.2647/1.3042/1.6572 for convergence,
and 1.2103/1.2117/1.2007 for WARPSYNC.
All complete results match; the larger migration regressions remain open.
Samples and source/input provenance are in
`.cache/ordered-gate/capture-binding-*-performance.json` and
`capture-binding-performance-provenance.json`.

Two further frame-storage experiments (2026-11-06) were discarded. A four-value
inline buffer improved MATCH by roughly 2–3% in repeated, reversed-order
comparisons, but slowed the nested convergence input by about 1%. Across the
fifteen fixtures, 119 suspending instruction bodies retained no values and
only eighteen retained any; reserving four values enlarged the common empty
frame. A one-value inline buffer with shared result-storage code produced mixed
costs and no consistent improvement. Both candidates passed 276 native checks,
and every timed complete response matched the control. Production frame storage
and timing baselines are unchanged. Samples and source snapshots are under
`.cache/ordered-gate/frame-values-*` and `frame-hybrid-*`.

Binding SHFL's target and validity calculations once (2026-11-06) removed
repeated Ghidra arithmetic without changing native timing materially.
The aliased, repeated-site 64-thread kernel was compared against the preceding
source snapshot under round-robin, reverse and seeded schedules. Thirteen
alternating samples of 200 calls, pinned to one CPU, checked complete response
equality throughout. Candidate/control ratios were 0.9984, 0.9957 and 0.9997
(about 84–85 microseconds per call), within 0.5%. This isolates the SHFL change;
the larger original-source migration regressions remain open and timing
baselines are unchanged. Inputs, samples and immutable source paths are under
`.cache/ordered-gate/shfl-sharing-*`.

Further barrier allocation/dispatch experiments (2026-11-06) were not retained.
Pooling boxed objects with an arena-owned shared allocator preserved all fifteen
fixtures' complete responses, but increased BAR time by 1.7% and nested
convergence by 4.4%. Typed block-barrier arrival/wait/defer bindings and direct
pending-generation retention passed 364 native checks and twenty malformed
arrival-reply checks, including invalid direct overrides and faults after writes.
Two process orders reproduced a 5.1% BAR gain, but also a 1.7–2.4% MATCH cost
and a 1.8–2.4% nested-convergence cost. Preserving the prior virtual-method order
did not remove the unrelated costs. A pending-generation-only candidate avoided
interface changes but still slowed MATCH and nested convergence.
Each comparison used the same fifteen inputs, thirteen alternating batches of
200 calls, one pinned CPU and complete response equality throughout. These
trials do not justify accepting new timing baselines. Production bindings,
object allocation and pending waits remain unchanged. Immutable candidates and
samples are under `.cache/ordered-gate/object-pool-*`,
`block-bindings-*` and `block-pending-*`.

Inlining the small native instruction dispatcher (2026-11-06) avoids a separate
call between the scheduler and the generated ordinary/resumable function.
The retained change preserves the runtime bindings and frame protocol.
Fifteen identical fixtures, thirteen alternating batches of 200 calls and one
pinned CPU reproduce approximately 4% gains for VOTE/REDUX and 2–3% for
WARPSYNC in both process orders. Every complete response agrees. Other changes
are small and mixed; this does not resolve the original migration regressions
above. A comparison run during concurrent generation showed misleading
wall-time increases; thread CPU time and a later idle comparison did not
reproduce them. Samples are under `.cache/ordered-gate/inline-dispatch-*`.
Rebuilt production libraries pass 391 native frame, scheduler, barrier and
collective checks (eleven skips) in 87 seconds. The production source matches
the immutable benchmark candidate byte for byte.

An isolated entry counter (2026-11-06) separates extra lane execution from
completed instructions. Original/current executions are 1,152/1,215 for BAR
POPC, 1,600/1,786 for VOTE, 1,552/1,922 for nested convergence,
2,192/2,562 for combined synchronization, and 784/815 for WARPSYNC.
Complete responses, including instruction counts, agree. Resuming suspended
frames therefore adds observable scheduler work, but these counts alone do
not account for all of the timing regression. Instrumentation remains in
temporary copies; production has no counters. Results and source paths are
in `.cache/ordered-gate/step-count-{diagnostic,roots}.json`.

Final ordered-IR performance comparison (2026-11-06) uses the original source,
the final production source and the same fifteen fixture inputs. Thirteen
alternating batches of 200 calls per fixture use one pinned CPU and thread CPU
time; complete responses agree. Candidate/original ratios are:

| Workload | Ratio |
|---|---:|
| BAR POPC | 1.2707 |
| Async copy | 1.0806 |
| VOTE / MATCH / REDUX | 1.1869 / 1.2883 / 1.2499 |
| Three matrix fixtures | 1.1560 / 1.1450 / 1.1793 |
| Nested / exited / predicated / combined convergence | 1.6250 / 1.2740 / 1.2720 / 1.6328 |
| Three WARPSYNC operand forms | 1.1813 / 1.1807 / 1.1848 |

These cooperative costs remain material. Explicit per-lane requests, retained
ticket/snapshot ownership, result validation and extra resume calls replace
operations the original scheduler performed outside instruction bodies.
The profiles and ablations above investigate those costs; they do not attribute
every cycle or establish that further optimization is impossible. Timing
baselines are unchanged. The native path for bodies that cannot suspend remains
ordinary code: the final alias-capture gate agrees on every checksum and has a
median ratio of 0.9820 over nine alternating 100-million-iteration samples.
Results are in `.cache/ordered-gate/final-original-native-performance.json`
and `final-fast-path-performance.log`.

Caching the block runtime pointer was also tested and discarded. It preserved
all fifteen complete responses but slowed several fixtures by 1–3% compared
with the retained dispatcher. The production runtime lookup remains unchanged;
the isolated candidate and samples are under `.cache/ordered-gate/runtime-cache-*`.

The final standard Python suite passes 4,219 tests and 109,434 selected shared
cases in both SM89 floating-point modes, with 180 skips. It took 674 seconds
including fixture construction and native comparisons. An earlier invocation
accidentally omitted the default `native_full` exclusion and was interrupted
during optional exhaustive enumeration; it is not the final suite result.
The final result is in `.cache/ordered-gate/final-python-sm89-standard.log`.
