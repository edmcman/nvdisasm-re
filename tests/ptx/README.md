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
| `viadd/packed_add.ptx` | SM100 | PTX signed/unsigned `add.s16x2`/`add.u16x2`; CUDA 13.0.88 emits `VIADD.16x2` |
| `vimnmx/vimnmx_modes.ptx` | SM100 | Signed scalar/packed min-max, `.relu`, and chained three-way min; CUDA 13.0.88 emits `VIMNMX` and `VIMNMX3` |
| `vimnmx/vimnmx_predicate_consumers.ptx` | SM100 | Checks whether compares/selects of min/max inputs or results make `ptxas` use VIMNMX predicate outputs; CUDA 13.0.88 leaves the slots as `PT` and emits separate `ISETP`s |
| `fmnmx3/min_chain.ptx` | SM100 | Chained PTX `min.f32`; CUDA 13.0.88 combines the two operations into `FMNMX3 ... PT` |
| `f2ip/pack_s8.ptx` | SM89 | Two `cvt.rni.s32.f32` operations followed by `cvt.pack.sat.s8.s32.b32`; CUDA 13.0.88 emits `F2IP.S8.F32.NTZ` |
| `i2ip/pack_s4_s2.ptx` | SM89 | `cvt.pack.sat.s4.s32` and `cvt.pack.sat.s2.s32`; CUDA 13.0.88 emits I2IP S4/S2 forms |
| `i2ip/pack_s8_s16.ptx` | SM89 | `cvt.pack.sat.s8.s32` and `cvt.pack.sat.s16.s32`; CUDA 13.0.88 emits I2IP S8/S16 forms |
| `viaddmnmx/viaddmnmx_modes.ptx` | SM100 | Add followed by signed/unsigned min-max, including packed and `.relu` forms; CUDA 13.0.88 combines them into `VIADDMNMX` |

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

The VIMNMX probes use PTX `min`/`max` instructions. A pair of chained scalar
`min.s32` operations lowers to `VIMNMX3`; the two-input scalar and packed
forms lower to `VIMNMX`. PTX `add.s16x2` and `add.u16x2` lower to `VIADD.16x2`.
The VIADDMNMX probe computes `add` followed by `min` or `max`; CUDA 13.0.88
`ptxas -arch=sm_100` combines those sequences into `VIADDMNMX`, including
signed RELU and packed 16-bit cases. These are compiler lowering probes, not
GPU execution tests or independent evidence of runtime semantics. The
compiler emits `PT` in both optional predicate-output slots for the VIMNMX
forms exercised here. The predicate-consumer probe also compares the original
inputs and the min result, then materializes those comparisons as integers;
`ptxas` still leaves VIMNMX's output slots as `PT` and emits separate `ISETP`
instructions. This establishes that these PTX patterns do not expose the
instruction's source-selection predicates.

The FMNMX3 probe loads three scalar floats and chains two `min.f32` PTX
operations. CUDA 13.0.88 folds them into one `FMNMX3 ... PT`; this confirms the
compiler lowering only and does not validate NaN, tie, or signed-zero behavior.

The F2IP probe converts two float inputs with round-to-nearest-even, then uses
the PTX saturating byte-pack operation. CUDA 13.0.88 fuses the conversion and
pack into `F2IP.S8.F32.NTZ`. The GPU oracle checks its packed output and
saturation behavior independently.

The I2IP probes use the PTX saturating integer-pack forms for 2-, 4-, 8-, and
16-bit destinations. CUDA 13.0.88 emits the corresponding I2IP forms on SM89.
These probes establish PTX-to-SASS lowering; the separate GPU oracle covers
signed and unsigned values, packing order, C-field preservation, and patched
SATRELU/no-SAT selectors.

Fixtures here are focused compiler probes, not a comprehensive PTX ISA test
suite. Keep generated cubins, disassembly captures, and temporary variants
outside this directory unless they are stable regression expectations.
