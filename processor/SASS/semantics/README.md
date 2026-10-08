# Operand values and instruction semantics

The generator preserves register spans and predicates for every class. Value semantics
add executable scalar values and native overrides or named runtime primitives
for 39 opcode families. The test harness can load cubin text and run independent
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
GPU comparisons currently cover the 23 whole-kernel SM89 fixtures recorded in
[verification.json](verification.json), rather than every class and selector.

IMAD.HI adds the 64-bit addend before selecting the high word; IMAD.WIDE
writes the whole sum. The CUDA mad.hi lowering places its scalar addend in
the upper register of that pair. The whole-kernel oracle executes that compiler lowering directly. Constant-bank widths are class
specific; SM75/80 HI/WIDE forms with 32-bit constant addends stay opaque
until their extension convention is established.

Native overrides cover ordinary MOV, IABS, SEL, IMAD low/high/wide, IADD3,
LOP3, SHF, low LEA, IMNMX, ordinary ISETP, S2R/CS2R, basic memory operations,
and supported BRA/EXIT/CALL/RET forms. Common supported restrictions include:

- Carry/predicate-result conventions that are not established remain opaque:
  IADD3 .X, non-PT carry destinations, IMAD carry variants, LEA high/extended
  forms, ISETP .EX, and SM120's extra IMNMX predicate operands/results.
- Wide views containing the hardwired zero slot, except views starting at the
  zero register itself, remain opaque. This avoids writing ordinary backing
  storage for a zero-register component.
- Indexed-register, descriptor, extended/uniform memory, unsupported address
  modes, and 256-bit memory variants retain opaque calls.
- CALL handles NOINC forms and RET handles absolute NODEC forms. Branch-stack
  changes and reconvergence-sensitive variants require additional state.

FADD/FMUL/FFMA/FSETP are native p-code (`f+`, `f*`, `f<`, `nan`, ...) for
round-to-nearest, including .FTZ (denormal inputs/results flushed to signed zero),
.SAT and FMUL scale (computed in fp64, exact). FFMA computes `a*b` exactly in
fp64 and narrows the fp64 sum: a deliberate double rounding that can differ
from hardware in rare midpoint cases, chosen for readable decompiler output.
Directed rounding (.RM/.RP/.RZ) and .FMZ fall back to `sass_prim_*`.
Ghidra 12.1.4's emulator flushes results strictly between half and one minimum
subnormal to zero (`FloatFormat.getEncoding`, the `n < 0` branch); the p-code is
IEEE, and the tests exclude that band.

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

The CUDA Driver API oracle compiles 23 SM89 kernels and executes their actual
`.text` through Ghidra's normal instruction stepping, from the prologue through
EXIT. It compares 128 random/edge input vectors per kernel (four blocks of 32),
including NaNs, infinities, denormals, signed zero, and integer boundaries.
Target mnemonics must both appear in cuobjdump output and execute in the emulator.
All 2,944 output comparisons passed on the RTX 4070 Laptop GPU.

The cubin's PARAM_CBANK and KPARAM_INFO records supply parameter offsets and
sizes. For these SM89 kernels, bank 0 parameters begin at 0x160, with the two
64-bit pointers at 0x160 and 0x168. Actual constant sections are loaded into
`cbank`; launch dimensions and named TID/CTAID special registers are seeded.
A fresh emulator per thread isolates local memory. Shared-memory fixtures use
cells private to each thread, so no cross-thread communication is modeled.
Kernel PC bounds and an instruction budget reject fallthrough and infinite loops.

Coverage includes integer arithmetic, IMAD low/high/wide, LOP3, SHF, LEA,
predicates/selects, PRMT, FADD/FMUL/FFMA, FSETP, F2I.TRUNC.NTZ, I2FP, moves,
uniform/ordinary constant loads, global/shared/local loads and stores, and
predicated branching with BSSY/BSYNC. FADD.FTZ, FMUL.RZ and FFMA.SAT have dedicated
fixtures. PRMT uses SASS operand order: data A, selector, data B.
SM89 F2I.TRUNC.NTZ to S32 maps NaN to zero and clamps infinities/overflow;
other NTZ combinations still reject until verified.

Floating results are bit-exact except that two NaNs compare equal regardless
of payload/sign. FTZ and saturation have no additional numeric tolerance.
MUFU approximations, warp collectives, device calls/returns, barrier reductions,
and cross-thread memory ordering remain outside this hardware validation.
The loop fixture explicitly acknowledges BSSY/BSYNC events; this verifies its
per-thread result without asserting a warp scheduler.

Use `NVCC` and `CUOBJDUMP` to select toolkit binaries. Without a GPU, normal
invocation prints an explicit skip; `--require-gpu` fails. The opt-in pytest
entry point is `SASS_GPU=1 python3 -m pytest tests -k semantics_gpu`.
CUDA device access requires running outside the restricted sandbox here.

Recorded results for this implementation are in [verification.json](verification.json).
