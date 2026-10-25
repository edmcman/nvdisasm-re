# PTX feature fixtures

This directory is the shared home for small PTX inputs used to observe how
`ptxas` lowers particular PTX constructs to SASS. Each fixture should keep its
target architecture and a single clearly marked instruction of interest.
Record the compiler version and expected SASS behavior here or alongside the
fixture when either is version-dependent.

A fixture's first line names its instruction of interest: `// sass: MNEMONIC[.MODS] ...` (each
must be emitted, matched like a GPU fixture target: equal or a `.`-extended prefix) or
`// sass-absent: MNEMONIC` (must not be). `python -m tests.evidence` compiles every annotated
fixture with the dev group's ptxas (CUDA 13.0.88) for its `.target`, fails on a broken
expectation, and records each emitted `sass:` class as ptxas evidence. Unannotated fixtures
(`f2fp/f8_scaled_upconvert.ptx`, PTX 9.2) are documentation only.

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
| `f2f/f2f_f32_f64_param_sm75.ptx` | SM75 | `cvt.rn.f32.f64` of an `.f64` kernel parameter; CUDA 13.0.88 emits `F2F.F32.F64 R, c[0x0][0x160]` reading the whole 8-byte parameter (evidence for the `f2f_f64_downconvert__RCR_RCR` md fix) |
| `f2f/f2f_f32_f64_param_sm80.ptx` | SM80 | `cvt.rn.f32.f64` of an `.f64` kernel parameter; CUDA 13.0.88 emits `F2F.F32.F64 R, c[0x0][0x160]` reading the whole 8-byte parameter (evidence for the `f2f_f64_downconvert__RCR_RCR` md fix) |
| `qspc/address_spaces.cu` | SM89 | Inline PTX emits `QSPC.E.G`, `.L`, and `.S`; the GPU distinguishes generic global, local, and shared pointers |
| `atom/global_int.ptx` | SM89 | `atom.global` integer operations at 32 and 64 bits; CUDA 13.4 emits `ATOMG.E.<op>.STRONG.GPU`, and warp-aggregates a uniform-address `add.u32` into `REDUX` plus one lane's `@P0 ATOMG` |
| `atom/global_cas.ptx` | SM89 | `atom.cas d, [a], b, c`: `ATOMG.E.CAS Rd, [Ra], Rb, Rc` with the compare value `b` in Rb and the new value `c` in Rc; `.b64` uses register pairs for both |
| `atom/global_float.ptx` | SM89 | `atom.add.f32`/`.f64`/`.noftz.f16x2`: `ATOMG.E.ADD.F32.FTZ.RN`, `.F64.RN`, `.F16x2.RN` |
| `atom/spaces.ptx` | SM89 | `red.global` emits `RED`, shared atomics `ATOMS`, generic ones `ATOM.E`; a shared `add.f32` has no SASS atomic: a loop of `LDS`, `FADD` and `ATOMS.CAST.SPIN`, retried until its Rd is 1 |

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

`qspc/address_spaces.cu` is an executable GPU probe rather than a lowering-only
fixture. Build it with `nvcc --allow-unsupported-compiler -arch=sm_89`, then run
it on an SM89 GPU. It checks a loaded global pointer, a low generic address,
local/shared pointers, and cross-space negative queries. The local/shared PTX
conversions first move to the specific space and then back to generic before
`isspacep`, matching the instruction's generic-address input.

`qspc/cluster.cu` compiles for SM90 and probes PTX `isspacep.shared::cluster`,
which maps to the SASS `D` selector. The available GPU is SM89, so this probe
can establish ptxas output by disassembly but cannot establish SM90 execution.
`qspc/const.cu` compiles an `isspacep.const` query for SM89; ptxas lowers it to
integer range comparisons rather than QSPC, so `.const` is not another QSPC
selector in this probe.
`qspc/rd.cu` is the input fixture for patched-Rd G/L/S probes. In the SM89
cubin, each QSPC `Rd` field was patched to the register holding the final
result store, and the following predicate `SEL` destination was moved to a
spare register. A seed of `0x5a17c0de` confirmed the observed values were
written by QSPC. Matching and cross-space queries show `Rd` carries a one-hot
tag for the input pointer's space (G=1, S=2, L=4), independent of the query.
