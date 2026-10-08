# Operand values and instruction semantics

The generator preserves register spans and predicates for every class. Value semantics
add executable scalar values and native overrides or named runtime primitives
for 36 opcode families. This is a single-instruction execution
model. It does not implement a CUDA kernel loader, warp scheduler, or concurrent
memory model.

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

Every opaque call retains both raw 64-bit instruction halves and the register
inputs. This permits lossless decoding of scalar operands, scheduling hints,
and reserved bits without adding thousands of duplicated scalar templates to
languages without value semantics. Instruction primitive calls also carry decoded scalars and
attributes. Dynamic constant pointers (CX), descriptors, attribute memory, and
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
register effects and raw word are preserved without asserting a result.
The coverage ledger's architecture-wide `hardware_verified` flag remains false:
GPU comparisons currently cover the 11 scalar SM89 fixtures recorded in
[verification.json](verification.json), rather than every class and selector.

IMAD.HI adds the 64-bit addend before selecting the high word; IMAD.WIDE
writes the whole sum. The CUDA mad.hi lowering places its scalar addend in
the upper register of that pair. The GPU oracle supplies the matching pair
rather than treating it as a 32-bit addend. Constant-bank widths are class
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

Runtime primitives cover FADD/FMUL/FFMA, FSETP, F2I/I2F, PRMT, SHFL/VOTE,
and synchronization events. Floating arithmetic uses exact rational values
with one IEEE rounding step; FTZ, saturation, and directed rounding are explicit.
Conversion results for NaN, infinity, or out-of-range inputs and FMZ/NTZ modes
whose conventions are unconfirmed raise `UnsupportedSemantics`. NaN results
are canonical quiet NaNs; GPU payload selection is not verified. PRMT implements
IDX and the six specialized modes.

S2R/CS2R require a special-register context. SHFL/VOTE require explicit 32-lane
values, predicates, active mask, and lane index. Inactive shuffle sources
are rejected because their values are undefined. BAR/BSSY/BSYNC require an
explicit synchronization event context; recording an acknowledged event is
not a warp-scheduling implementation. Barrier reductions/results require a
handler. MUFU is a named primitive that requires a hardware-specific handler;
it does not substitute host approximations silently. CPU arithmetic references
use the [NVIDIA PTX arithmetic definitions](https://docs.nvidia.com/cuda/parallel-thread-execution/)
as reference conventions; GPU checks are needed to establish SASS equivalence.

## Runtime ABI

Opaque: `sass_opaque_OPCODE(SM:u32, raw_lo:u64, raw_hi:u64, register_inputs...)`.
The class can be decoded from the raw word, allowing identical class templates
to share p-code. Primitive:
`sass_prim_OPCODE(SM:u32, class_ordinal:u32, raw_lo:u64, raw_hi:u64, inputs...)`.
Input order and output widths come from the detailed class manifest. Multiple
outputs return one byte bundle in manifest order, first output in the low bytes.
All inputs are evaluated before assigning any output. Sinks discard writes to
zero/true registers and register-bank overflow views.

`Runtime(..., handlers={name: callback})` permits environment implementations;
callbacks receive `(arguments, output_bytes, context)`. Without a handler,
unknown operations raise with the decoded class and values. The JSON-line
Ghidra harness accepts `context.special` keyed by numeric special-register id,
`context.operands` keyed by `class.operand`, `context.warp`, and
`context.synchronization`. Memory initialization is explicit. Kernel parameter
bank offsets have not been inferred into the placeholder compiler prototype.

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

The CUDA Driver API oracle compiles 11 scalar families for SM89, confirms each
target mnemonic in its kernel, and compares GPU results to corresponding
Ghidra instruction fixtures. It checks random and edge-case vectors; only NaN
payload differences are normalized. It is not full-kernel emulation. Use `NVCC`
and `CUOBJDUMP` to select toolkit binaries. With no GPU, normal invocation prints
an explicit skip; `--require-gpu` fails. MUFU, synchronization, descriptor
layouts, and remaining variants require separate hardware/context validation.

Recorded results for this implementation are in [verification.json](verification.json).
GPU execution passed 1,408 comparisons across all 11 scalar families on an
RTX 4070 Laptop GPU (SM89). CUDA device access requires running the oracle
outside the sandbox in this environment. These results cover the tested
selectors; NaN payload differences are normalized.
