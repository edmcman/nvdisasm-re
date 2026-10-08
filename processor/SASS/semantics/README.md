# Operand values and instruction semantics

The generator preserves register spans and predicates for every class. Value semantics
add executable scalar values and native overrides or named runtime primitives
for 58 opcode families. The test harness can load cubin text and run independent
threads as whole kernels. The general Ghidra loader, warp scheduler, and
concurrent memory model remain separate work.

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
GPU comparisons currently cover the 43 whole-kernel SM89 fixtures recorded in
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
opaque fallbacks. Uniform wide views follow the same zero-slot restrictions.
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

FSEL selects the modified floating input, with optional .FTZ. FMNMX implements
ordinary min/max, including .FTZ: a numeric input wins over NaN, two NaNs yield
`0x7fffffff`, and equal signed zeros yield -0 for min or +0 for max. Predicate
true selects min; false selects max. .NAN, .XORSIGN and predicate-result .IS_A forms remain opaque. The GPU checks
compare FSEL and ordinary FMNMX (including FMNMX.FTZ) bits exactly on SM89.
FSEL.FTZ and other architectures have emulator coverage.

FADD/FMUL/FFMA/FSETP are native p-code (`f+`, `f*`, `f<`, `nan`, ...) for
round-to-nearest, including .FTZ (denormal inputs/results flushed to signed zero),
.SAT and FMUL scale (computed in fp64, exact). FFMA computes `a*b` exactly in
fp64 and narrows the fp64 sum: a deliberate double rounding that can differ
from hardware in rare midpoint cases, chosen for readable decompiler output.
Directed rounding (.RM/.RP/.RZ) and .FMZ fall back to `sass_prim_*`.
Ghidra 12.1.4's emulator flushes results strictly between half and one minimum
subnormal to zero (`FloatFormat.getEncoding`, the `n < 0` branch); the p-code is
IEEE, and the tests exclude that band.

DADD/DMUL/DFMA are native for round-to-nearest, with 64-bit register pairs,
uniform inputs, high-word immediates, constant-bank loads, and source absolute/negate
modifiers. DFMA uses a 16-byte binary128 intermediate: the product is exact,
but the wide sum followed by narrowing can double-round in rare midpoint cases.
Directed rounding remains opaque. DSETP shares the ordered/unordered FP32
comparison logic and combines complementary results with AND/OR/XOR; MIN/MAX
selectors retain opaque fallbacks. Dynamic constant operands still require an
explicit operand provider. The same Ghidra underflow limitation applies to FP64.

HADD2/HFMA2 implement packed FP16 under RN, including per-lane absolute/negate,
H1_H0/H0_H0/H1_H1 selection, saturation, register/uniform inputs, separate lane
immediates, and constant-bank values. HFMA2 uses binary128 for an exact finite
half product and sum before a single rounding to half; HADD2 widens to fp64.
Both results are captured before writing an overlapping destination. An
unmodified H0_H0 source reads only its low two bytes in optimized p-code.
FP32/BF16/E6M9 formats, FP32 inputs, H0_NH1, FTZ/FMZ, and RELU remain opaque.
The minimum-subnormal Ghidra emulator limitation also applies to FP16.

The `HFMA2.MMA Rd, -RZ, RZ, imm_hi, imm_lo` form is a native constant move
under nofmz/nosat. Hardware shows it preserves finite bits, signed zero,
infinities, and denormals, while canonicalizing each NaN half to `0x7fff`.
This transform is evaluated from encoded immediates at decode time, so the
move emits one COPY. Other MMA forms retain opaque fallbacks.

Runtime primitives cover the float fallbacks, F2I/I2F, PRMT, SHFL/VOTE,
and synchronization events. Floating arithmetic uses exact rational values
with one IEEE rounding step; FTZ, saturation, and directed rounding are explicit.
Conversion results for NaN, infinity, or out-of-range inputs and FMZ/NTZ modes
whose conventions are unconfirmed raise `UnsupportedSemantics`. NaN results
are canonical quiet NaNs; GPU payload selection is not verified. PRMT implements
IDX and the six specialized modes.

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

The CUDA Driver API oracle compiles 43 SM89 kernels and executes their actual
`.text` through Ghidra's normal instruction stepping, from the prologue through
EXIT. It compares 128 random/edge input vectors per kernel (four blocks of 32),
including NaNs, infinities, denormals, signed zero, and integer boundaries.
Target mnemonics must both appear in cuobjdump output and execute in the emulator.
All 5,504 output comparisons passed on the RTX 4070 Laptop GPU.

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
predicated branching with BSSY/BSYNC. FADD.FTZ, FMUL.RZ and FFMA.SAT have dedicated
fixtures. FSEL and FMNMX min/max have dedicated fixtures, including FMNMX.FTZ
and mixed NaN/signed-zero/denormal pairs. FP16 has ordinary/saturating and
broadcast/modifier fixtures; MMA move constants include signed zero, negative
signaling/quiet NaNs, denormals, and infinities. Seven fixtures replace one
arithmetic word in the compiled kernel to request SASS modes directly; scheduling
bits and compiled prologues remain intact. The manifest records these patches.
SM89 F2I.TRUNC.NTZ to S32 maps NaN to zero and clamps infinities/overflow;
other NTZ combinations still reject until verified.

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
