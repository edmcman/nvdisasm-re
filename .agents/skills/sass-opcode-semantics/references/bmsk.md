# BMSK worked example

This is the investigation performed in `/home/ed/Projects/nvdisasm-re` on
2026-10-11. Addresses are historical evidence for the pinned binary, not
portable identifiers. Consult the maintained repository notes for updates.

## Compiler and path

- Compiler: `/usr/local/cuda-13.0/bin/ptxas`, CUDA 13.0.88, non-PIE.
- SHA256: `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`.
- ORI dispatcher: `dispatch_ori_opcode_conversion`, `0x9ed2d0`.
- ORI case `0x18`: `lower_bmsk_operation_to_target`, `0x9d9f60`.
- Target class 1: destination role 6, position role `0xd`, width role `0x14`.
  Final descriptor low bit supplies clamp 0 / wrap 1 through builder slot
  `+0x218`.
- SM100 finalizer row `0x2339760`, keys `00/05`:
  `encode_sm100_BMSK_variant_00_05`, `0x1a0eeb0`.
- Native opcode `0x21b`; Rd bits 16..23, position register bits 24..31,
  width register bits 32..39, mode bit 75.

A live breakpoint hit the lowerer for SM100. The SM89 compilation bypassed
that handler. The SM100 compiler trace and SM89 GPU checks are separate pieces
of evidence.

## Probes and reproducible captures

Repository artifacts:

- `research/ptxas/bmsk_clamp.ptx`
- `research/ptxas/bmsk_wrap.ptx`
- `research/ptxas/bmsk_clamp_finalizer.jsonl`
- `research/ptxas/bmsk_wrap_finalizer.jsonl`

The PTX probes use runtime parameters and thread-dependent XORs to keep the
operation in varying registers. Their arithmetic instructions are:

```ptx
bmsk.clamp.b32 %r2, %r0, %r1;
bmsk.wrap.b32  %r2, %r0, %r1;
```

To capture one probe, run from the repository root with a fresh JSONL path:

```sh
PTXAS_CAPTURE=/tmp/bmsk-clamp-new.jsonl gdb -q -batch \
  -ex 'source research/ptxas/capture_finalizer.py' \
  -ex run --args /usr/local/cuda-13.0/bin/ptxas \
  -arch=sm_100 research/ptxas/bmsk_clamp.ptx -o /tmp/bmsk-clamp-new.cubin
python3 research/ptxas/validate_capture.py /tmp/bmsk-clamp-new.jsonl /tmp/bmsk-clamp-new.cubin
```

Each original capture matched all 24 native words / 384 bytes of its same-run
cubin. The target words, as little-endian bytes:

| Mode | Word | SASS |
|---|---|---|
| Clamp | `1b720500050000000000000000ca0f00` | `BMSK R5, R0, R5` |
| Wrap | `1b720500050000000008000000ca0f00` | `BMSK.W R5, R0, R5` |

Their XOR is exactly `1 << 75`. The md calls the field
`BITS_1_75_75_relu`, but assigns it to `cw`; the shared field name does not
describe this instruction's semantics. `CWMode` defines `C=0`, `W=1`.

## Functional rule

For 32-bit unsigned position `a` and width `b`, set output bit `i` precisely
when `0 <= i < 32` and `a <= i < a+b`. In wrap mode, first replace `a` and `b`
with `a & 31` and `b & 31`. Evaluate this reference with mathematical integers
so `a+b` cannot overflow.

Wrap masks the counts; it does not rotate mask bits around the word. Clamp
position >=32 gives zero. Clamp width >=32 fills from a valid position to
bit 31. Width zero gives zero in either mode. Position 32, width 1 distinguishes
the modes: clamp 0, wrap 1.

The independent oracle enumerates output bits. Native P-code uses shifts and
an intersection of masks; it caps clamp width before addition to avoid unsigned
overflow. P-code shifts by at least the operand width produce zero.

Specification: [PTX bmsk](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#integer-arithmetic-instructions-bmsk).

## Implementation and verification

`src/sass/semantics/operations.py::emit_BMSK` supports register, uniform-register, immediate
and ordinary constant-bank width operands. It retains the unresolved CX form
as an opaque fallback. UBMSK was not added in this change.

The fixtures are `tests/cases/bmsk.py` and `tests/kernels/bmsk_semantics.cu`.
Focused value tests can run as:

```sh
uv run python -m tests.semantics SM89 exact --family bmsk
uv run python -m tests.semantics SM89 readable --family bmsk
uv run pytest tests -m gpu --require-gpu --family bmsk
```

The SM89 hardware oracle compiles two whole kernels, verifies one BMSK in each
requested mode, and asserts their target words differ only at bit 75. It checks
target execution and compares both GPU and whole-kernel Ghidra outputs with the
independent reference. Inputs include all position/width pairs from 0 through
33, large unsigned values and random cases.

Recorded results for the completed investigation:

- 1,504 hardware and whole-kernel P-code vectors per mode; 3,008 total.
- Focused value tests: 3,750 per language for SM75/80/86/89/90 and 3,398
  per language for SM100/101/103/120; 64,684 across both language variants.
- Dataflow/control-flow checks passed on all nine architectures in exact mode
  and on SM89 in readable mode, with 2,000 real words plus synthetic samples.
- Full SM89 semantics regressions: exact 13,581/13,581; readable 8,751/8,751.
- All 18 languages compiled with zero unused temporaries and below 16 MiB
  decompressed; the largest was SM90 exact at 12,190,260 bytes.

Hardware evidence covers SM89 register-input kernels. Other operand forms and
architectures have emulator evidence. Full records are in
`processor/SASS/semantics/verification.json` and the trace is in
`research/ptxas/ptxas-finalizer-notes.md`.
