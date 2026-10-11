# Operand values and instruction semantics

The generator preserves register spans and predicates for every class. Value semantics
add executable scalar values and native overrides or named runtime primitives
for the opcode families registered in `sass/operations.py`. The test harness
can load cubin text and run independent threads as whole kernels. The general
Ghidra loader, warp scheduler, and concurrent memory model remain separate work.

`sass/semantic.py` builds operand values independently of assembly display.
Encoded immediates retain their size, sign and scale; multi-field operands and
inverse tables use the same md rules as the decoder. Floating immediates carry
bits, including F64 high-word immediates. Modifiers are independent inputs;
native overrides apply them in the consuming type's context. Register operands
use exact md spans and true/zero-register behavior. Constant operands load from
`cbank` at `(bank << 32) | uint32(offset)`; LDC forms calculate an address before
loading. `ram`, `shared`, and `localmem` are separate spaces.

Encoded constants, register modifiers, signedness, comparisons, shift modes and
memory widths are selected during decoding. Supported native instructions emit
their calculation directly; unsupported selectors choose an opaque constructor.
For example, immediate and register MOV emit one operation, and ordinary IMAD
and IADD3 emit two. LOP3 truth tables simplify to the boolean expression for the
selected table; discarded predicate results do not generate calculations.
Shared value tables are evaluated only when used, and only reachable tables are
emitted. The build fails on unused-temporary diagnostics.

Opaque calls pass register inputs and decoded immediates. Instruction primitive
calls also carry decoded modifiers and attributes. Dynamic constant pointers (CX), descriptors, attribute memory, and
unencoded stateful operands use `sass_operand_value`. A context provider must
supply their values; the implementation does not invent their address layouts.
Latency-only implicit resources are annotations, not guessed fixed registers.
Opaque control-flow variants retain md branch properties in the manifest; they
require a handler to execute their control effects.

## Coverage

[coverage.json](coverage.json) is a compact, generated ledger for all nine
architectures. The generator also writes `sass_smXX_coverage.json` next to each
language, with operand inventory, input order, output widths for every span
variant, and native selector guards. These detailed manifests are required by
the runtime and are generated rather than committed.

`native` means an override has a supported class shape; selector guards can
still choose an opaque fallback. It does not mean every encoding in the class
has value semantics. `primitive` means the runtime validates and executes the
operation or rejects an unsupported selector/context. `opaque` means the md's
register effects, registers and immediates are passed without asserting a result.
The coverage ledger's architecture-wide `hardware_verified` flag remains false:
GPU comparisons currently cover the 60 whole-kernel SM89 fixtures recorded in
[verification.json](verification.json), rather than every class and selector.

IMAD.HI adds the 64-bit addend before selecting the high word; IMAD.WIDE
writes the whole sum. The CUDA mad.hi lowering places its scalar addend in
the upper register of that pair. The whole-kernel oracle executes that compiler lowering directly. Constant-bank widths are class
specific; SM75/80 HI/WIDE forms with 32-bit constant addends stay opaque
until their extension convention is established.

Native overrides cover ordinary MOV, IABS, SEL, IMAD low/high/wide, IADD3,
LOP3, SHF, LEA low/high/extended, IMNMX, ordinary ISETP, S2R/CS2R, basic memory operations,
and supported BRA/EXIT/CALL/RET forms. Common supported restrictions include:

- ISETP .EX and SM120's extra IMNMX predicate operands/results remain opaque.
- Wide views containing the hardwired zero slot, except views starting at the
  zero register itself, remain opaque. This avoids writing ordinary backing
  storage for a zero-register component.
- Indexed-register, descriptor, extended/uniform memory, unsupported address
  modes, and 256-bit memory variants retain opaque calls.
- CALL handles NOINC forms and RET handles absolute NODEC forms. Branch-stack
  changes and reconvergence-sensitive variants require additional state.

Mixed ordinary/uniform register forms of IMAD, IADD3, ISETP and LEA use the
same native calculations. UIADD3, UIMAD, USHF, ULEA, UISETP, ULOP3, USEL and
UMOV reuse those emitters with uniform register and predicate names. Ordinary
32-bit IADD and VIADD are native; packed/saturating VIADD and carry modes retain
opaque fallbacks. VIMNMX implements PT=min and !PT=max for S/U32, S/U16x2,
and SM120's S/U8x4 formats with register, uniform-register, and immediate B
sources. Signed RELU forms clamp each selected lane at zero; unsigned RELU
encodings are invalid according to the machine description. Non-PT predicate
outputs and CX forms remain opaque. CuLifter's per-lane behavior has
independent exact/readable emulator fixtures on SM100 and SM103, and VIMNMX
also on SM120; this path has no GPU verification because these instructions
are newer than the available SM89 hardware. Uniform wide views follow the
same zero-slot restrictions.
VIADDMNMX adds B modulo each selected lane width, then applies the same
predicate-selected min/max against C. It supports S/U32 and S/U16x2 with R,
uniform-register, and immediate B/C forms on SM100 and SM103. The encoded B
negation is applied before lane extraction, matching CuLifter's operand
materialization. Signed RELU forms clamp each result lane at zero; unsigned
RELU encodings are invalid according to the machine description. Independent
exact/readable emulator fixtures cover lane overflow, signed comparisons,
predicate polarity, B negation, RELU, and all source shapes. No GPU
verification is available for these newer instructions.
Native IADD3.X adds both predicate inputs to the complemented 32-bit operands.
Ordinary negation retains its 33-bit +1, including the carry from negating zero.
IADD3 predicate outputs indicate a sum at least 2^32 and 2^33; the first output
wins if both destinations name the same predicate. LEA computes the shifted
low/high word before applying negate/complement and adds its predicate for .X.
IMAD.X adds one predicate; HI/WIDE carry indicates unsigned overflow of the
full 64-bit product and pair addend, including signed product bit patterns.
These operations capture input predicates before writing overlapping outputs.
IADD3/LEA encodings modifying both Ra and Rb retain opaque fallbacks. Discarded
carry outputs preserve compact arithmetic. Uniform aliases share these emitters.

PRMT emits native byte selection for IDX (including sign replication), F4E,
B4E, RC8, RC16, ECL and ECR, in SASS source order: data A, selector, data B.

BMSK emits native 32-bit mask generation from position and width. Instruction
bit 75 selects clamp (0, `BMSK`) or wrap (1, `BMSK.W`). Wrap masks both counts
with 31; the resulting mask still ends at bit 31. Clamp returns zero for a
position at least 32 and saturates large widths at the end of the word. Zero
width returns zero in both modes. Register, uniform-register, immediate and
ordinary constant-bank inputs are supported. The `cx[URx][offset]` form also
has native P-code on SM75-SM90. Its 64-bit uniform-register pair contains a
bounded constant-buffer header, and its offset is in bytes. SM75-SM89 decode
the low 45 bits as base/16 and the high 19 bits as size/16; SM90 decodes the
low 51 bits as base/64 and the high 13 bits as size/16. The load uses `ram`
at the decoded GPU virtual address, returning zero outside the region.
The layouts come from Mesa's `nvk_bindless_cbuf` definitions; SM89 hardware
validates addressing and bounds. SM90's layout and zero-on-out-of-bounds
behavior are implemented and emulator-tested, with hardware validation pending.
Other opcode families' CX values still require a `sass_operand_value` provider.
Dedicated SM89 whole-kernel GPU/reference/p-code comparisons cover
1,504 inputs per mode; emulator tests cover both languages on all architectures.
`tests/bmsk_cx_semantics.py` supplies a separate manually patched SASS oracle
for CX counts, region sizes and offsets: 8,192 GPU/reference/P-code comparisons
pass in each language variant. Another 446 focused CX cases pass in each of
the ten SM75-SM90 languages. The oracle does not claim PTX generated CX.

FSEL selects the modified floating input, with optional .FTZ. FMNMX implements
ordinary min/max, including .FTZ: a numeric input wins over NaN, two NaNs yield
`0x7fffffff`, and equal signed zeros yield -0 for min or +0 for max. Predicate
true selects min; false selects max. .NAN, .XORSIGN and predicate-result .IS_A forms remain opaque. The GPU checks
compare FSEL and ordinary FMNMX (including FMNMX.FTZ) bits exactly on SM89.
FSEL.FTZ and other architectures have emulator coverage.

FMNMX3 is native on SM100/SM103 for PT/!PT, `noftz`/`nonan` RRR, RUR, and
immediate-B forms. It applies two ordered comparisons: PT selects the three-way minimum,
and !PT selects the maximum. Ties and unordered comparisons
retain the earlier input at each step, matching CuLifter's formula. FTZ/NAN
selectors, dynamic predicate registers, and CX forms remain opaque. Independent exact/readable emulator
fixtures cover both predicate directions, ties, signed zeros, infinities, and
NaNs. No compatible GPU is available, so its edge behavior follows the
CuLifter formula rather than hardware measurements.

## Exact and readable floating point

Every architecture has two languages. `SASS:LE:64:smXX` (exact) models hardware
results; `SASS:LE:64:smXX_readable` writes the plain formula for decompiler
reading. They decode and display identically, and have the same register
dataflow. Only floating-point value semantics differ. ELF opinions select the
exact language; choose the readable one explicitly. `sass/fpops.py` holds both
lowerings. `python3 -m sass.build_languages --fp=exact|readable` limits a
build to one mode; both are built by default.

The readable lowering uses native-width `f+`/`f*` (FFMA is `a f* b f+ c`, one
operation per operator plus a copy). It treats every rounding mode as
round-to-nearest, and drops FTZ/FMZ flushing, fused single rounding, and the
FMNMX NaN/signed-zero rules (including .NAN). SAT, absolute and negate remain.
As a result, FADD/FMUL/FFMA/DADD/DMUL/DFMA with .RM/.RP/.RZ or .FMZ, HADD2/HFMA2
.FTZ/.FMZ, and FMNMX.NAN are native in this lowering. Its tests compare against
the unfused round-to-nearest formula, bit for bit.

FADD/FMUL/FFMA/FSETP are native exact p-code (`f+`, `f*`, `f<`, `nan`, ...) for
every rounding mode, .FTZ (denormal inputs/results flushed to signed zero),
.SAT and FMUL scale (computed in fp64, exact). Under round-to-nearest, FFMA
computes `a*b` exactly in fp64 and narrows the fp64 sum. This deliberate double
rounding can differ from hardware in rare midpoint cases.

Directed rounding (.RM/.RP/.RZ) computes the result in the doubled width: fp64
for fp32, binary128 for fp64. It keeps the exact residual: a TwoSum error for
sums, zero for the exact products. It narrows with round-to-nearest, then moves
the encoding by one ulp when the residual's sign calls for it. Under RM, an
exact cancellation gives -0 unless both addends are +0. The corrected result is
exact, and it also undoes Ghidra's underflow error described below.
`sass_prim_*` remains the fallback for .FMZ and unknown selectors.

Ghidra 12.1.4's emulator flushes results strictly between half and one minimum
subnormal to zero (`FloatFormat.getEncoding`, the `n < 0` branch). The p-code
itself is IEEE, so the round-to-nearest tests exclude that band. Its binary128
comparisons also order NaN operands, so the directed-rounding code tests
`nan()` explicitly.

DADD/DMUL/DFMA are native for round-to-nearest, with 64-bit register pairs,
uniform inputs, high-word immediates, constant-bank loads, and source absolute/negate
modifiers. DFMA uses a 16-byte binary128 intermediate: the product is exact,
but under round-to-nearest the wide sum followed by narrowing can double-round
in rare midpoint cases. Directed rounding uses the exact residual correction
above. DSETP shares the ordered/unordered FP32
comparison logic and combines complementary results with AND/OR/XOR; MIN/MAX
selectors retain opaque fallbacks. Dynamic constant operands still require an
explicit operand provider. The same Ghidra underflow limitation applies to FP64.

HADD2/HFMA2/HMUL2 implement packed FP16 under RN, including per-lane absolute/negate,
H1_H0/H0_H0/H1_H1 selection, saturation, register/uniform inputs, separate lane
immediates, and constant-bank values. HFMA2 uses binary128 for an exact finite
half product and sum before a single rounding to half; HADD2 widens to fp64.
Both results are captured before writing an overlapping destination. An
unmodified H0_H0 source reads only its low two bytes in optimized p-code.
HMUL2 multiplies each selected pair after widening to fp32, which holds the
exact finite binary16 product before one narrowing conversion. F16_V2 supports
FMZ, FTZ, and no-flush modes, with SAT and no-SAT variants. FTZ flushes
subnormal inputs and results while preserving their signs. On SM89, FMZ
produces canonical +0 when either selected input has an all-zero exponent,
including zero-times-infinity; this case is checked against the GPU. SAT clamps
the result to [0, 1]. Register, uniform, immediate, and
constant-bank sources are covered where the machine description provides them.
FP32/BF16/E6M9 formats, FP32 inputs, H0_NH1, and RELU remain opaque.
The minimum-subnormal Ghidra emulator limitation also applies to FP16.

The `HFMA2.MMA Rd, -RZ, RZ, imm_hi, imm_lo` form is a native constant move
under nofmz/nosat. Hardware shows it preserves finite bits, signed zero,
infinities, and denormals, while canonicalizing each NaN half to `0x7fff`.
This transform is evaluated from encoded immediates at decode time, so the
move emits one COPY. Other MMA forms retain opaque fallbacks.

Runtime primitives cover the float fallbacks, F2I/I2F, PRMT, SHFL/VOTE,
QSPC, and synchronization events. QSPC requires `context["address_spaces"]`,
a mapping from `global`, `local`, and `shared` to half-open address intervals;
SM90+ also accepts `shared_cluster`. `S` queries CTA shared memory, while
SM90+'s `D` queries cluster-visible shared memory, corresponding to PTX
`isspacep.shared::cluster`. `Pu` reports whether the address belongs to the
queried space. `Rd` reports a one-hot input-space tag: global `1`, shared `2`,
local `4`, based on SM89 GPU probes. `Rd` with D is kept opaque until checked
on SM90 hardware. The emulator cannot infer generic-pointer provenance from
an integer address; missing mappings, unmapped addresses, or overlapping
ranges raise `UnsupportedSemantics`. Floating arithmetic uses exact rational values
with one IEEE rounding step; FTZ, saturation, and directed rounding are explicit.
Conversion results for NaN, infinity, or out-of-range inputs and FMZ/NTZ modes
whose conventions are unconfirmed raise `UnsupportedSemantics`. NaN results
are canonical quiet NaNs; GPU payload selection is not verified. PRMT implements
IDX and the six specialized modes.

F2IP has a native path for packed F32-to-S8/U8 conversion when the instruction
uses round-to-nearest-even, NTZ, no RELU, and direct 32-bit register inputs.
It rounds and saturates each input independently; NaNs become zero and infinities
saturate by sign. The first input occupies bits 8–15, the second bits 0–7, and
the low 16 bits of C occupy bits 16–31. Other destination/source formats,
rounding selectors, modifiers, and CX sources retain opaque fallbacks. The
SM89 GPU oracle verifies the packed byte order and edge behavior.

I2IP has a native path for S32 inputs packed to signed or unsigned 2-, 4-,
8-, and 16-bit lanes. SAT clamps each input to its destination range;
SATRELU clamps signed inputs to zero through the signed maximum. The no-SAT
form keeps the low destination bits. For 2- and 4-bit packing, the supported
`.28`/`.24` extraction selectors place the two converted inputs in the high
and low lane, respectively; the remaining high bits come from C. The 8-bit
and 16-bit forms use H0 extraction, with C supplying the unused upper bits for
the 8-bit form. The SM89 GPU oracle compares all eight signed/unsigned widths,
plus patched no-SAT and SATRELU selector cases, against both an independent
reference and Ghidra p-code. Unsupported selector combinations, source shapes,
and CX forms retain opaque fallbacks.

F2I has a native p-code path for the hardware-verified F32-to-S32
`.TRUNC.NTZ` form. It truncates finite values toward zero, maps NaNs to zero,
and clamps infinities and overflow to the signed 32-bit limits. The path is
generated for all architectures; GPU behavior has been measured on SM89.
Exact mode falls back to the runtime primitive for other rounding modes.
Readable mode ignores the encoded rounding selector and uses truncation, like
its other floating-point operations ignore rounding details. Other formats,
destinations, NTZ selectors, and source modifiers fall back to the runtime
primitive.

S2R/CS2R copy named special registers; the launch environment seeds their values. SHFL/VOTE require explicit 32-lane
values, predicates, active mask, and lane index. Inactive shuffle sources
are rejected because their values are undefined. BAR/BSSY/BSYNC require an
explicit synchronization event context; recording an acknowledged event is
not a warp-scheduling implementation. Barrier reductions/results require a
handler. MUFU is a named primitive that requires a hardware-specific handler;
it does not substitute host approximations silently. CPU arithmetic references
use the [NVIDIA PTX arithmetic definitions](https://docs.nvidia.com/cuda/parallel-thread-execution/)
as reference conventions; GPU checks are needed to establish SASS equivalence.

## Runtime ABI

Opaque: `sass_opaque_OPCODE(inputs...)`, where inputs are the register operands
and decoded immediates in operand order. Modifiers, scheduling hints and reserved
bits are not arguments; they remain in the instruction bytes at the call's address.
Primitive: `sass_prim_OPCODE(class_ordinal:u32, inputs...)`, where inputs include
the decoded modifiers.
The runtime architecture comes from the loaded language. Input order and output
widths come from the detailed class manifest. Multiple
outputs return one byte bundle in manifest order, first output in the low bytes.
All inputs are evaluated before assigning any output. Sinks discard writes to
zero/true registers and register-bank overflow views.

`Runtime(language_dir, sm, handlers={name: callback})` permits environment implementations;
callbacks receive `(arguments, output_bytes, context)`. Without a handler,
unknown operations raise with the decoded class and values. The JSON-line
Ghidra harness accepts named special-register values in `registers`,
`context.operands` keyed by `class.operand`, `context.warp`, and
`context.synchronization`. Memory initialization is explicit. Kernel parameter
bank offsets are read from cubin metadata by the test harness; the general
Ghidra loader and compiler prototype remain Phase 6 work.

## Build and verification

```sh
export SASS_SLEIGH_OUT=/tmp/sass-semantics
python3 -m sass.build_languages --output /tmp/sass-semantics
# Finish compilation before parallel Ghidra tests. The build command writes
# compiled files atomically, checks the 16 MiB decompressed size limit, and
# rejects unused-temporary diagnostics.
python3 -m sass.coverage /tmp/sass-semantics --output processor/SASS/semantics/coverage.json
python3 -m unittest discover -s tests -p test_semantics_cpu.py
python3 tests/test_semantics.py SM89
python3 tests/test_dataflow.py SM89 20000
python3 tests/compare_ghidra.py SM89 synthetic 0
python3 tests/gpu_semantics.py --compile-only
python3 tests/gpu_semantics.py --require-gpu
```

`tests/ghidra_emulate.py` runs decoded p-code through Ghidra's real byte executor
and a userop library. It checks actual register values, memory and control flow,
not a Python reimplementation of the instruction dispatcher. `GHIDRA_PY`
selects the pyghidra Python interpreter. In a restricted environment, set
`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` to writable scratch directories.

The CUDA Driver API oracle compiles 60 SM89 kernels and executes their actual
`.text` through Ghidra's normal instruction stepping, from the prologue through
EXIT. It compares 128 random/edge input vectors per kernel (four blocks of 32),
including NaNs, infinities, denormals, signed zero, and integer boundaries.
Target mnemonics must both appear in cuobjdump output and execute in the emulator.
All 7,680 output comparisons passed on the RTX 4070 Laptop GPU.

`tests/carry_semantics.py --require-gpu` adds 210 patched SM89 whole-kernel
variants (26,880 comparisons) for IADD3, LEA and IMAD carry chains, including
aliased IADD3 outputs, predicate inversion, negate/complement,
shift boundaries and signed/unsigned full-width overflow. Independent integer
references and actual Ghidra execution must both match the GPU. The probe checks
compiler slot/register assignments before replacing eight reserved BAR slots
and a placeholder arithmetic instruction; compiled addressing and EXIT remain.
Other architectures are checked in the emulator, without hardware evidence.

The cubin's PARAM_CBANK and KPARAM_INFO records supply parameter offsets and
sizes. For these SM89 kernels, bank 0 parameters begin at 0x160, with the two
64-bit pointers at 0x160 and 0x168. Actual constant sections are loaded into
`cbank`; launch dimensions and named TID/CTAID special registers are seeded.
A fresh emulator per thread isolates local memory. Shared-memory fixtures use
cells private to each thread, so no cross-thread communication is modeled.
Kernel PC bounds and an instruction budget reject fallthrough and infinite loops.

Coverage includes integer arithmetic, IMAD low/high/wide, LOP3, SHF, LEA,
predicates/selects, PRMT, HADD2/HFMA2 and the MMA constant move, DADD/DMUL/DFMA/DSETP, FADD/FMUL/FFMA, FSEL, FMNMX, FSETP, F2I.TRUNC.NTZ, I2FP, moves,
uniform/ordinary constant loads, global/shared/local loads and stores, and
predicated branching with BSSY/BSYNC. FADD.FTZ and FFMA.SAT have dedicated
fixtures. Directed rounding has eighteen: .RZ/.RM/.RP for each of
FADD/FMUL/FFMA/DADD/DMUL/DFMA. Each uses edge vectors whose residual lies below
the wide format's precision, exact cancellations, and overflow. FSEL and FMNMX min/max have dedicated fixtures, including FMNMX.FTZ
and mixed NaN/signed-zero/denormal pairs. FP16 has ordinary/saturating and
broadcast/modifier fixtures; MMA move constants include signed zero, negative
signaling/quiet NaNs, denormals, and infinities. Seven fixtures replace one
arithmetic word in the compiled kernel to request SASS modes directly; scheduling
bits and compiled prologues remain intact. The manifest records these patches.
SM89 F2I.TRUNC.NTZ to S32 maps NaN to zero and clamps infinities/overflow;
other NTZ combinations still reject until verified.

BREV and POPC are native 32-bit operations for register, uniform-register,
immediate, and ordinary constant-bank sources where present. FLO supports
32-bit sources with `.S32` and `.SH`/default mode: the default result is the
highest set-bit index, `.SH` returns the leading-zero shift, and zero returns
`0xffffffff` in both modes. Its predicate destination is supported only when
encoded as `PT`. SGXT supports `.S32`/`.C`, selects the byte indexed by the low
two bits of its first source, then sign-extends that byte to 32 bits. CX
encodings of these operations remain opaque because their compound sources
need a `sass_operand_value` provider; other FLO and SGXT selectors retain
opaque fallbacks. The tests compare BREV against a per-bit reversal reference,
POPC against `bit_count`, FLO against the bit length/leading-zero definitions,
and SGXT against signed byte extraction.

IDP has native guarded semantics for `IDP.4A` R, URb, and ordinary
constant-bank B forms. The four packed byte products use independent S8/U8
extension selectors and accumulate with Rc modulo 2^32; the Rc negation flag
is applied by the normal operand path. CX and all 2A forms remain opaque.
Independent packed-byte reference fixtures cover each signedness pair,
overflow, Rc negation, and source/destination overlap on all nine architectures
in both exact and readable languages. These are emulator checks; no IDP GPU
oracle result is recorded.

VIMNMX3 uses its Pp input to select three-way min for PT and max otherwise,
per lane, for S32/U32/S16x2/U16x2. Native support covers R, URb, and immediate
B forms on SM100 and SM103. Signed RELU forms clamp each result lane at zero;
unsigned RELU encodings are invalid according to the machine description.
Independent lane-wise fixtures pass in exact and readable mode. No hardware
oracle result is available because these instructions are newer than the
available SM89 hardware.

VABSDIFF4 supports S8/U8 register, uniform, immediate, and ordinary
constant-bank operands for `SIMD_MRG` and `ACC`; CX and non-PT Pu destinations
remain opaque. `SIMD_MRG` packs four absolute byte differences and ignores C.
`ACC` adds the four differences as a scalar, then adds C modulo 2^32. CuLifter's
CPU handler matches these SASS rules. A whole-kernel SM89 oracle patched one
LOP3 instruction per fixture to the target VABSDIFF4 encoding; 128 GPU,
emulator, and independent-reference comparisons passed for each fmt/reduction
pair. This is patched SASS evidence, not a claim that ptxas emits those words
for a PTX vabsdiff4 instruction.

Floating results are bit-exact except that two NaNs compare equal regardless
of payload/sign (independently for each packed half). MMA move constants are
compared bit-exactly, including NaN canonicalization. FTZ and saturation have no additional numeric tolerance.
MUFU approximations, warp collectives, device calls/returns, barrier reductions,
and cross-thread memory ordering remain outside this hardware validation.
The loop fixture explicitly acknowledges BSSY/BSYNC events; this verifies its
per-thread result without asserting a warp scheduler.

Use `NVCC` and `CUOBJDUMP` to select toolkit binaries. Without a GPU, normal
invocation prints an explicit skip; `--require-gpu` fails. The opt-in pytest
entry point is `SASS_GPU=1 python3 -m pytest tests -k semantics_gpu`.
CUDA device access requires running outside the restricted sandbox here.

Recorded results for this implementation are in [verification.json](verification.json).
