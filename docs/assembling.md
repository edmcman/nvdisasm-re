# Assembling SASS

`sass-assemble` derives instruction syntax and encoding from the committed machine
descriptions. Assembly needs Python, without ptxas, nvdisasm, Ghidra, or a GPU.
CUDA tools and an SM89 GPU provide independent checks in the tests.

```sh
uv sync
uv run sass-assemble examples/assembler/write_value.sass -o write_value.cubin
uv run sass-decode write_value.cubin
uv run sass-assemble --format raw --arch SM89 code.sass -o code.bin
```

Input and output paths accept `-` for stdin and binary stdout. Cubin is the default
format. `--arch` supplies a target when `.target` is absent; conflicting targets
fail. Raw output additionally accepts `--base 0x100` for instruction addresses.
Assembly errors leave an existing output file untouched.

## Instructions, symbols, and numbers

Use nvdisasm-style SASS with semicolon-terminated instructions, labels, and `//` or
`/* */` comments. Address and encoding comments in decoder listings are ignored.
Instructions may span lines; directives end at a newline or semicolon.

```sass
.target SM89
MOV R0, 3;
loop:
IADD3 R0, R0, -1, RZ;
ISETP.NE.U32.AND P0, PT, R0, 0, PT;
@P0 BRA loop;
EXIT;
```

Numeric relative-branch operands are **absolute target addresses**, as in the
decoder. Labels can be bare, `$loop`, or nvdisasm's `` `(loop) `` spelling.
`$symbol` accepts integer addends, such as `$output+4`. Labels are kernel-local.
The alternate spelling `BRA -0x10.REL` takes a displacement instead of an address.

Registers, guards, modifiers, memory operands, and alternate forms follow the
selected machine description. Spelling accepted on SM89 need not work on SM100:
some newer IADD3 forms require explicit predicate outputs, and constant operands
use different instructions. Invalid combinations, out-of-range values, and
misaligned operands fail; values are never truncated or approximated by a nearby
encoding-table row. Decoded `???N` placeholders are not valid source.

Floating immediates accept decimals, signed `INF`, `QNAN`, and `SNAN`, or raw hex
bits. Decimals round to the selected immediate format. F64 immediates store only
their high 32 bits; values needing nonzero low bits are rejected. Use raw bits for
exact NaN payloads. Ignored bits initialize deterministically: disassembly text
alone does not preserve every bit of arbitrary input words.

## Explicit scheduling

Controls use machine-description operand names and enum labels. Annotations
precede the instruction's semicolon:

```sass
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R0, 1;
LDG.E R2, [R4.64] @sched(dst_wr_sb=0);
FADD R0, R1.reuse, R2 @sched(usched_info=W1);
.schedule
```

`.schedule` replaces defaults for following instructions; the empty directive
resets them. `@sched(...)` overrides individual defaults for one instruction.
Names include `usched_info` (wait/group behavior), `batch_t`, `req_bit_set`
(scoreboard wait mask), and, where present, `src_rel_sb` and `dst_wr_sb`. Enum
operands require labels; integer operands accept decimal or hex. Unknown names,
duplicate assignments, and illegal combinations fail.

Unspecified controls use machine-description defaults, including `7` for unused
scoreboard fields. These are encoding defaults, **not a dependency-safe schedule**.
Default `DRAIN` conflicts with `.reuse` in many forms; select a compatible mode
explicitly. The assembler does not analyze hazards, allocate scoreboards, reorder
instructions, or insert waits.

## Kernel declarations and cubins

The [write_value example](../examples/assembler/write_value.sass) takes launch
arguments `(device_pointer, uint32_value)` and initializes its registers and global
memory descriptor. It has been executed through the SM89 CUDA Driver API. The
assembler adds no kernel prologue or epilogue.

| Directive | Meaning |
|---|---|
| `.target SM89` | Module machine-description target |
| `.kernel name` / `.endkernel` | Declare an entry kernel |
| `.param name, size, alignment` | Parameter byte size and power-of-two alignment, 1–16 |
| `.registers count[, uniform_count]` | Required GPR count and optional uniform-register count |
| `.shared bytes` | Static shared-memory allocation |
| `.constant bank, "hex bytes"` | Initialize a kernel-local constant bank, 1–17 |

Parameters pack in declaration order with the requested alignment. `$name` gives
their address in constant bank zero, including the target's ABI parameter base.
The supported parameter bank is limited to 4096 bytes. Bank zero is reserved for
launch constants and parameters. Register counts include register zero and all
referenced spans, excluding RZ/URZ. An omitted uniform count means zero uniform
registers. Declare enough registers for implicit uses as well. Dynamic shared
memory comes from the CUDA launch argument independently of static storage.

Multiple kernels produce one executable with multiple entry symbols. The native
writer emits CUDA ELF64 ABI v8 headers, toolkit notes identifying `sass-assemble`,
SM90+ compatibility records, symbol tables, parameter/resource metadata, constants, shared storage, exit and
CTA-ID read offsets, and named-barrier counts. Code is padded with NOPs to 256-byte
multiples without moving source instructions or labels.

| Machine-description targets | Parameter base | Cubin target |
|---|---|---|
| SM75, SM80, SM86, SM89 | `0x160` | Same SM |
| SM90 | `0x210` | SM90 |
| SM100, SM103, SM120 | `0x380` | Same SM |
| SM101 | `0x380` | SM110, CUDA 13's renamed target |
| SM107 | Unestablished | Raw output only |

Profiles were checked against CUDA 13.0.88 toolchain output on 2026-10-15. Only SM89
native cubins have hardware execution verification. Other targets have structural
and disassembly validation, which does not establish every instruction or
specialized kernel feature on those GPUs.

Device functions, calls/returns, external symbols, linking/relocations, local-stack
declarations, specialized tensor/cluster launch metadata, and CuAssembler's ELF
directives are outside this interface. Raw assembly remains available for code
intended for another executable writer.

## Python API and tests

```python
from sass.assemble import assemble_instruction, assemble_raw, assemble_cubin

word = assemble_instruction("SM89", "MOV R0, 1")  # 16 little-endian bytes
code = assemble_raw("SM89", "entry: NOP; BRA entry;", base=0x100)
image = assemble_cubin(open("examples/assembler/write_value.sass").read())
```

The APIs return `bytes`. Invalid source raises `sass.assemble.AssemblyError`, a
`ValueError` with filename, line, column, and a diagnostic. `encoding.encode`/`word` (arch, class, operand
values) are the harness-facing API over the same encoder; `permissive=True` truncates out-of-range and
misaligned values, picks the nearest table row and skips legality and rendering checks, for the
semantics harness's synthetic cases (`tests/harness/case.py` passes it). Both modes write starred literals.

```sh
uv run pytest tests/test_assemble.py --arch all
# Outside the sandbox, on SM89:
uv run pytest tests/test_assemble.py -m gpu --require-gpu
```

Tests round-trip sampled legal forms and explicit controls on every machine
description, compare independent nvdisasm output, check ELF and parameter metadata,
and execute native SM89 cubins without compiled templates, including three-dimensional
grid launches. CTAID.Z reads set the corresponding CUDA launch metadata flag.
