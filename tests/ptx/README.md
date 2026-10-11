# PTX feature fixtures

This directory is the shared home for small PTX inputs used to observe how
`ptxas` lowers particular PTX constructs to SASS. Each fixture should keep its
target architecture and a single clearly marked instruction of interest.
Record the compiler version and expected SASS behavior here or alongside the
fixture when either is version-dependent.

| Fixture | Target | PTX feature / observed SASS |
|---|---|---|
| `arithmetic/add_f32.ptx` | SM100 | Rounded fp32 add lowering |
| `arithmetic/add_s32.ptx` | SM100 | Signed 32-bit add lowering |
| `arithmetic/add_u64.ptx` | SM100 | 64-bit add and carry-chain lowering |
| `arithmetic/sub_u32.ptx` | SM100 | 32-bit subtraction lowering |
| `arithmetic/sub_u64.ptx` | SM100 | 64-bit subtraction and carry-chain lowering |
| `arithmetic/mul_wide_s32.ptx` | SM100 | Signed wide multiply lowering |
| `arithmetic/mul_wide_u32.ptx` | SM100 | Unsigned wide multiply lowering |
| `arithmetic/mad_hi_u32.ptx` | SM100 | High-word multiply-add lowering |
| `arithmetic/mad_wide_u32.ptx` | SM100 | Wide multiply-add lowering |
| `arithmetic/one_add.ptx` | SM100 | Minimal one-instruction add probe |
| `bmsk/bmsk_clamp.ptx` | SM89 | `BMSK` clamp mode |
| `bmsk/bmsk_wrap.ptx` | SM89 | `BMSK.W` wrap mode |
| `bmsk/cx_candidate_indirect_const_no_cx.ptx` | SM89 | Negative CX search probe; emits an ordinary constant load plus register-source `BMSK` |
| `bfind/flo_bfind.ptx` | SM89 | PTX `bfind.u32` and `bfind.shiftamt.u32` lowering to the `FLO` family |
| `vabsdiff4/vabsdiff4_modes.ptx` | SM89 | PTX merge, `.add`, lane mask, and `.sat` forms; CUDA 13.0.88 expands them to scalar SASS sequences and emits no `VABSDIFF4` instruction |

The `BMSK.CX` PTX trigger remains unknown. The selector in CUDA 13.0.88
`ptxas` checks the width operand's modifier bit 24, constant-source metadata
subtype 4, and register eligibility before emitting CX. Existing probes,
including the indirect `.const` load here, do not meet those conditions and
produce no CX instruction. A direct memory expression is rejected by the PTX
operand matcher. The CX semantics fixture in `tests/kernels/` uses manually
patched SASS and is not evidence of a PTX recipe.

Compile a fixture with its recorded target, for example:

```sh
ptxas -arch=sm_89 tests/ptx/bmsk/bmsk_clamp.ptx -o /tmp/bmsk.cubin
nvdisasm /tmp/bmsk.cubin
```

`bfind/flo_bfind.ptx` was compiled by CUDA 13.0.88 `ptxas` (SHA256
`daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`). Its two
instructions lower to `FLO.U32` and `FLO.U32.SH`; the SM89 whole-kernel GPU
oracle compares each form with an independent reference and Ghidra p-code.

The VABSDIFF4 PTX probe was compiled with the same `ptxas` build. Its
`vabsdiff4` PTX forms do not map directly to the SASS opcode: SM89 output
expands them into scalar SASS instructions. The SASS value checks therefore
use separate whole-kernel fixtures that replace a LOP3 word with a manually
encoded VABSDIFF4 instruction while preserving the original scheduling bits.
That distinction matters because PTX `.add` is lane-wise, while SASS
`VABSDIFF4.ACC` is a scalar reduction.

Fixtures here are focused compiler probes, not a comprehensive PTX ISA test
suite. Keep generated cubins, disassembly captures, and temporary variants
outside this directory unless they are stable regression expectations.
