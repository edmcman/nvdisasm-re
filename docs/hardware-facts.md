# GPU oracle and hardware-established behavior

What GPUs established about SASS behavior, by opcode group, and the GPU oracle that established
it. Everything here is SM89 (NVIDIA GeForce RTX 4070 Laptop GPU) unless stated; SM90 (H100 NVL)
results are under [SM90 runner validation](#sm90-runner-validation-2026-10-22). Neither establishes
other architectures. How to run, write and record GPU tests (shared cases,
fixtures, probes, evidence) is in [`tests/README.md`](../tests/README.md); the harness history is
in [`docs/testing.md`](testing.md).

- [The GPU oracle](#the-gpu-oracle)
- [Integer and predicate](#integer-and-predicate)
- [Floating point](#floating-point)
- [Conversions](#conversions)
- [MUFU](#mufu)
- [Memory](#memory)
- [Warps, threads and control flow](#warps-threads-and-control-flow)
- [Encoding](#encoding)
- [SM90 runner validation](#sm90-runner-validation-2026-10-22)

## The GPU oracle

- Device and toolkit: an NVIDIA GeForce RTX 4070 Laptop GPU, driver 580.178.04. Fixture kernels
  are built with CUDA 13.0.88 (`/usr/local/cuda` or the dev group's identical wheels). GPU access
  works outside the sandbox.
- Fixtures: `pytest -m gpu --require-gpu` (`test_refs_on_gpu`) compiles the fixture kernels for the device's architecture (SM89 here; SM90 below),
  runs them on the GPU through the CUDA Driver API (ctypes), and compares each fixture's vectors
  (typically 128 random/edge vectors) with its reference. `pytest -m ghidra`
  (`test_fixtures_emulated`) runs the same kernels whole in Ghidra's `PcodeEmulator`, checks that
  the target mnemonics execute, and compares with the same reference (no GPU).
- Whole-kernel emulation: `src/sass/decode/cubin.py::kernel_image` reads the actual text, constant
  sections and CUDA parameter metadata. The harness seeds bank-0 launch dimensions,
  metadata-defined pointers and the named TID/CTAID registers, and runs independent threads from
  entry to EXIT with PC bounds and an instruction budget. Scalar fixtures keep shared memory
  thread-private; BSSY/BSYNC are acknowledged for independent scalar loop execution.
- Cooperative fixtures (`Fixture(cooperative=True, block=N)`): one `BlockEmulator` per block, warps
  in lockstep, SHFL/VOTE gathered across lanes, BAR.SYNC as a whole-block barrier; details in
  [`tests/README.md`](../tests/README.md#multi-thread-fixtures).
- Scope: the first per-run record (144 kernels, 2026-10-12) excluded warp collectives,
  cross-thread shared memory, memory ordering, MUFU other than full-width SIN/COS, device
  calls/returns and barrier reductions. Cooperative fixtures (2026-10-14) and the MUFU survey
  (2026-10-16) have since covered warp collectives, shared memory across BAR.SYNC and MUFU
  ([below](#warps-threads-and-control-flow)); memory ordering, device calls/returns and barrier
  reductions are still not hardware-verified. That record was in
  `processor/SASS/semantics/verification.json` (git history); per-class evidence is now derived
  from the tests into `processor/SASS/semantics/evidence.json`.
- Native assembler execution (2026-10-15, `tests/test_assemble.py::test_native_cubin_on_gpu`):
  cubins written directly by `sass.assemble`, without compiling or patching templates, load and
  execute. Verified: pointer/scalar parameters, multiple entry points including a zero-parameter
  entry, a backward branch loop, initialized constant bank 2, static/dynamic shared-memory accesses
  with BAR.SYNC, and CTAID.Z in a three-dimensional grid. These are executable/ABI checks, not
  hardware evidence for every instruction or other architecture.
- Native probe setup (2026-10-16): variable-latency scheduling must cover constant-source ALU, FCHK
  and DSETP as well as loads. Assigning write/read scoreboards from the md scheduling fields and
  draining them resolves stale-output failures. Native local storage uses EIATTR_MIN_STACK_SIZE
  (0x12) and EIATTR_FRAME_SIZE (0x11), matching the compiled `sem_local` kernel's 1024-byte
  records; local addresses include `c[0][0x28]`. Constant bank 2 is per-kernel, while bank 3 uses a
  module-wide `.nv.constant3` section, matching ptxas output; `.nv.constant3.kernel` leaves bank-3
  inputs unreadable as intended data.

## Integer and predicate

- PRMT: SASS source order is data A, selector, data B.
- Carry chains: native carry chains include IADD3.X (two predicate inputs), LEA LO/HI/SX32 with
  .X, and IMAD LO/HI/WIDE .X, including uniform aliases. IADD3 Pu/Pv are unary thresholds at
  2^32/2^33; Pu wins aliased outputs. Ordinary negation preserves the 33-bit +1; .X uses complement
  plus predicate inputs. LEA selects its shifted low/high word before modifiers. IMAD HI/WIDE carry
  reports unsigned overflow of the full 64-bit product-plus-addend. Ra/Rb cannot both be modified
  for IADD3/LEA. The carry GPU fixtures (`tests/cases/carry.py`) check patched whole kernels against
  independent integer references and actual Ghidra p-code.
  - Negated IMAD addend (`-Rc`, 2026-10-17, shared cases `imad_negated`, now the generated profile
    of that name in `tests/harness/autocase_profiles.py`): it enters the sum as `~C + 1`, as in
    IADD3, so Pu = product >= C and `-0` always carries. Checked for IMAD.HI/.WIDE U32/S32 and
    UIMAD.WIDE with zero, one, all-ones and sign-bit pair addends (an earlier model negated C mod
    2^64 and missed the `-0` carry).
  - Every IMAD/UIMAD operand form (2026-10-17, shared cases `imad_forms`, now a generated profile:
    B and C from register, uniform, `c[][]` bank 2, bindless `cx[][]` or immediate, U32/S32, `-C`
    and `.X` `~C` with both carry-ins) follows the same rules. The .HI/.WIDE addend is 64 bits in
    every form, including `cx[][]`, whose md ISRC size describes its header pair rather than the
    loaded value (the semantics used to load 32 bits there).
- Plain IMAD has no carry output (2026-10-16, patched `imad__RRR_RRR` words): the md gives it no
  `Pu` field, bits 81-83 are `PT` in ptxas output, and nvdisasm prints the same `IMAD.U32` for any
  value there. Hardware treats them as a predicate destination and writes false to `Pk` when they
  are `k` < 7 (checked for P0-P3, U32/S32, plain and negated addend, vectors from {0, 1, 0xffff,
  0x10000, 0x7fffffff, 0x80000000, 0xffffffff} x C in {0, 1, 0xffffffff}), whether or not the low
  sum or the 64-bit product-plus-addend overflows. So `IMAD R0, P0, R1, R2, R3` is not a carry
  form; carries exist only on `.HI`, `.WIDE` and the `.X` forms. The result is lo32(A*B) + C (or
  - C) as usual. The md names these bits `Pu` (83:81, plus `Pp` at 89:87) on the plain IMAD/UIMAD
  classes but marks them `ignore`; the semantics do not model the write. See issue #24 and
  [Encoding](#encoding).
- Uniform datapath twins (2026-10-16, shared cases `uniform_plain` in `tests/cases/carry.py`,
  since 2026-10-17 generated profiles): UIADD3 (result, both carry-outs, negated sources), UIMAD
  (U32/S32), UIMAD.WIDE (low, high, carry-out) and USEL (with `!UP`) match their per-thread
  counterparts on the carry vectors, and so do the shared `IDP.4A` register-form cases for all four
  S8/U8 pairs. The uniform `.X` shared cases also run on the GPU, but narrowly (2026-10-17):
  UIADD3.X, UIMAD.WIDE.X (U32) and ULEA.HI.X on the carry vectors with carry-in 1 and no inverted
  sources; UIMAD.X and ULEA.LO.X with one all-zero case each.
- SGXT (2026-10-16, shared cases in `tests/cases/int_alu.py`): the width n is B for `.C` (clamped
  to 32) and B & 31 for `.W`; the result keeps A's low n bits and extends them from bit n-1 (sign
  for `.S32`, zero for `.U32`); n = 0 gives 0 and a clamped n = 32 returns A. Checked on values
  {0, 0x7f, 0x80, 0xff, 0x7fffffff, 0x80000000, 0xffffffff} and widths {0, 1, 7, 8, 31, 32, 33,
  0xff, 0xffffffff} for register, uniform, immediate and constant sources. The shared native probes
  of the same date confirm it for all four cw/fmt variants (SGXT.C.S32 sign-extends A's low
  `min(B,32)` bits, B=0 gives zero) on inputs 0, 1, 0x80000000, 0xffffffff and 0xdeadbeef, widths
  0/1/2/7/8/15/16/30/31/32/33/63/0xffffffff, and register/uniform/immediate/constant sources. The
  earlier byte-select model (Ra & 3 indexing a byte of Rb) was wrong.
- ISETP.EX (2026-10-16, shared native probes): keeps F false and T true even for equal high words;
  Pr decides equal operands only for the other six comparisons.
- FLO (2026-10-12, fixtures `bfind_s32`/`bfind_shift_s32`): FLO.S32 (plain `FLO`; ptxas emits it
  for `bfind.s32`) searches the complement of a negative source: the highest bit differing from the
  sign, so -1 gives -1 like 0. FLO.U32 searches the value. The earlier emitter and reference
  treated S32 like U32.
- I2IP sub-byte forms (2026-10-12): S2/U2 `.28` and S4/U4 `.24` fill the high bits from C
  unshifted; only H1 selects C's high half. The earlier reference and p-code shifted for every
  non-H0 form.
- PLOP3 (2026-10-13, fixtures in `tests/cases/predicates.py`): with Pu == Pv it writes Pu's value.
- P2R merge into a non-RZ base (2026-10-16, 44 patched `P2R Rd, PR, R4, mask` fixtures in
  `tests/cases/predicates.py`, bases 0xffffffff and 0xa5a5a5a5, every byte, masks 0, 1, 0x2a,
  0x55, 0x80, 0xff, 0xffffffff, and Rd == Ra): with `k = (mask & 0xff) << 8b`,
  `Rd = (Ra & ~k) | ((PR << 8b) & k)`. Only the masked bits of the selected byte change. The mask
  covers all eight bits of that byte; bit 7 has no predicate behind it (P7 is PT, but it reads 0),
  so mask bit 7 clears it. Mask bits above 7 are ignored. The earlier fixtures (RZ base, or mask
  0x7f over bytes with bit 7 clear) could not tell this from replacing the whole byte, which the
  semantics wrongly did. The same rule holds for every P2R/R2P mask source (53 fixtures: R6, UR6,
  `c[0x0][0x0]` = blockDim.x with blocks of 32/64/128, and CX through the BMSK descriptor wrapper
  with bit-7-only P2R masks, in and out of bounds); CX reads the mask with BMSK's bounded load.
  UP2UR `.B0` with an immediate mask merges into URa the same way (2026-10-16, 12 fixtures: bases
  0xffffffff and 0xa5a5a5a5, the same masks, URd == URa). ptxas emitted only RZ bases in every
  kernel tried and in 17,580 P2Rs of the CUDA 13.0 libraries' SM89 SASS (cuSPARSE, cuSOLVER, NPP).

## Floating point

- Exact rounding (2026-10-14, references written for the formerly reference-less fixtures):
  FADD/FMUL/FFMA (every rounding mode, FTZ, SAT) and DADD/DMUL/DFMA are exactly rounded.
  Floating comparisons are exact except NaN payload/sign.
- Arithmetic NaN results (2026-10-22, 814 probe observations on SM89: NaN operands of both signs,
  quiet and signaling, in every position and pair, under `-`/`|.|`, .SAT and .FTZ, and invalid
  operations Inf-Inf, 0*Inf):
  - FADD/FMUL/FFMA: always +0x7fffffff, whatever the operands, payloads or modifiers.
  - HADD2/HMUL2/HFMA2 (F16_V2, and HFMA2 BF16_V2): each NaN lane is +0x7fff.
  - DADD/DMUL/DFMA: the first NaN register of Rb, Rc, Ra (DADD: Rc before Ra; DMUL: Rb before Ra),
    quieted, with its own sign and payload: `-`/`|.|` on it are ignored (as FRND.F64 and F2F).
    Signaling and quiet NaNs have no priority over each other. Without a NaN operand an invalid
    operation gives 0xfff8000000000000.
  - .SAT turns any NaN result into +0.
- Source `|x|` and `-x`: pure sign-bit operations, absolute value before negation.
  - DADD/DMUL/DFMA (2026-10-19, every operand form and modifier tuple, 64,022 shared-case
    scenarios): applied before exact rounding, so `-|+0|` is -0 and `|-Inf|` is +Inf. NaN results
    ignore them, as above.
  - FADD/FMUL/FFMA, FSET/FSETP and FMNMX (2026-10-20, every generated non-FMZ operand form and
    modifier tuple, 580,088 fp32 shared-case scenarios including negative Inf/NaN, signed
    subnormals and saturation boundaries): applied before rounding, FTZ, saturation and comparison.
    FMZ forms and FMNMX.XORSIGN were not part of the generated profile (see the next two entries).
- FMUL/FFMA `.FMZ` (2026-10-20, 22,125 raw `Cases.probe` observations (9,168 FMUL, 12,957 FFMA):
  every rounding mode, SAT, FMUL scales and immediates, every `|x|`/`-x` combination; synthetic
  words): `.FTZ`, except that a factor with a zero exponent (zero or subnormal, after the
  modifiers) makes the product +0, even times Inf or NaN, and `-0 * x` is +0. FFMA then computes
  `+0 + Rc` with FTZ, the rounding mode and SAT, so `0 * Inf + -0` is +0 under RN and -0 under RM,
  and a NaN Rc gives 0x7fffffff. Same rule as HMUL2.FMZ.
- FMNMX.XORSIGN: the modified operands are compared, and the result's sign is the XOR of the
  encoded signs before `|x|` and `-x`.
  - 2026-10-16 (shared native probes): compares the modified signed inputs, then replaces the
    numeric result's sign with their raw sign XOR; `.abs` source modifiers account for the
    magnitude comparison in the earlier PTX `min.xorsign.abs` probe.
  - 2026-10-20 (the `.FMZ` probe run, 12,288 observations over NAN/FTZ/min/max, `|x|`/`-x` on
    either source): the XOR of the modified signs fails 5,120 of them. A NaN result (two NaNs, or
    any NaN under `.NAN`) stays the canonical 0x7fffffff, without the XOR sign.
- FCHK.DIVIDE Pu, a, b (2026-10-13, fixtures in `tests/cases/fp32.py`) depends only on the biased
  exponents: `ea <= 24 or ea == 255 or eb <= 1 or eb >= 252 or ea - eb >= 127 or eb - ea >= 125`
  (signs, mantissas and |x|/-x ignored; 0 mismatches on 1.1M pairs including every boundary cell).
- FSET/HSET2/HSETP2 (2026-10-14, 68 fixtures in `tests/harness/scalar.py` `SETS`; ptxas
  `set`/`setp` carriers with a live Pp, patched per selector): all 16 comparisons, AND/OR/XOR
  against Pp and !Pp, FTZ, HSET2 `.BF` (0x3c00 per true lane) and `.BM` (0xffff), H1_H1/H0_H0
  swizzles with -x/|x|, and HSETP2 `.H_AND` follow the references with no mismatches across 128
  edge/random vectors each. `.H_AND` (no PTX form; ptxas uses PLOP3 for `and.pred`) gives
  Pu = (lane 0 && lane 1) bop Pp and Pv = !(lane 0 && lane 1) bop Pp. ptxas lowers
  `set.u32.f32` (FSET's BM) to FSETP+SEL; the md has only `FSET.BF`.
- DSETP.MIN/MAX (2026-10-19): not the constant false/true masks their nibble values (0, 15)
  suggest. Pu = cond bop Pp, where MIN is `totalOrder(Ra) <= totalOrder(Rc)` and MAX is
  `totalOrder(Ra) > totalOrder(Rc)` (so -0 < +0, subnormals count, `-|x|`/`-x` apply first),
  except that a NaN Rc makes both true and a NaN Ra makes both false (even for a NaN Rc). Pv ignores
  the combiner and Pp: it is 1 only when both inputs are NaN, else 0 (a preset Pv is overwritten).
  Probed over 13 values squared x MIN/MAX x AND/OR x Pp 0/1; the shared cases (all modifier forms,
  5,366) pass.
- FRND (2026-10-13, fixtures in `tests/cases/conversions.py`) rounds to an integral value keeping a
  zero's sign; an F32 NaN gives 0x7fffffff, an F64 NaN keeps its payload, quieted.
  - FRND.F64 NaN (2026-10-18): the result is the register's own NaN, quieted, with sign and payload
    untouched by `-` and `|.|` (`-R4` and `-|R4|` of 0x7ff81234 give 0x7ff81234, of 0xfff81234
    give 0xfff81234); non-NaN sources are negated/abs'd as usual. The earlier reference applied
    the modifiers before quieting, flipping the NaN's sign.

## Conversions

- F2I:
  - F2I.TRUNC.NTZ to S32 returns 0 for NaNs and clamps infinities/finite overflow (early fixtures;
    other NTZ formats/modes were then unsupported).
  - 2026-10-13 (fixtures in `tests/cases/conversions.py`): F2I saturates in every mode; a NaN gives
    0 with .NTZ (ptxas emits it for 32-bit destinations from F32/F16) and the destination's top bit
    otherwise (0x80000000, 0x8000000000000000, also for U64). .FTZ flushes a denormal input (CEIL of
    +denormal: 1 without, 0 with).
- I2F (2026-10-13) is exactly rounded in all four modes; an F16 result is zero-extended in Rd.
  I2F.S64's 32-bit immediate (2026-10-16, shared native probes) is the high word of its 64-bit
  source (`-1` encodes -2^32, producing F32 0xcf800000), rather than a sign-extended low word.
- F2F NaNs:
  - 2026-10-13: F2F between F32/F64 or F16/F64 keeps a NaN's sign and payload (aligned to the top
    of the fraction), quieted.
  - 2026-10-16 (shared native probes, correcting the 2026-10-13 fixtures): F32-to-F16 produces
    0x7fff and F16-to-F32 produces 0x7fffffff regardless of source sign/payload. F64-to-F16 and
    F16-to-F64 retain the quieted, aligned payload and sign, as do F32/F64 conversions. Packed F16
    arithmetic and F2FP kept their NaN-class comparison policy then; preserved MERGE_C lanes are
    compared exactly.
  - 2026-10-18: F2F with an F64 source or destination ignores `-` and `|.|` on a NaN and returns the
    register's own quieted NaN (as FRND.F64).
- F2FP NaN (2026-10-18): F2FP packs any NaN, of either sign, signaling or quiet, to +0x7fff in F16
  as well as BF16 and E6M9 (scalar and uniform, PACK_AB and MERGE_C); F2FP.TF32 turns any NaN into
  0x7fffe000, with or without RELU. The earlier references kept F16's NaN sign (0x7e00) and gave
  RELU TF32 0x7fffffff; manual cases had not covered those NaNs or compared them by class.

## MUFU

### Survey (2026-10-16)

Raw `MUFU.<op> R11, R2` patched into the carry kernel; `research/mufu/`, data in
`tests/refs/mufu_samples.json` and `mufu_survey.json`, fixtures `mufu_*` in `tests/cases/mufu.py`.
Every 32-bit input was run for COS, SIN, EX2, LG2, RCP, RSQ, RCP64H, RSQ64H, SQRT and TANH and
compared with the correctly rounded float64 result (ulp distance on the f32 bit pattern; on the
high word for the 64H ops).

- Bit-exact fraction and worst distance for normal inputs with normal results: RCP 86.8% / 1 ulp,
  RCP64H 93.1% / 1, RSQ64H 94.3% / 1, SQRT 83.1% / 1, RSQ 78.3% / 2, EX2 91.5% / 2, TANH 94.4% /
  about 130 (|x| from 2^-7 to 2), COS 87.8%, LG2 49.7%, SIN 4.8%.
- SIN and COS take their argument in turns (`MUFU.SIN(0.25) = 1.0`) and have an absolute error of
  up to 3.4e-7 (2^-21.5) across arguments from 2^-31 to 2^33, so their ulp distance explodes near
  zero crossings: for |x| below 2^-25 SIN returns +/-0 (sign kept), and SIN returns +/-0 at integer
  and half-integer arguments from 2^22 up; COS is near +/-1 there (thresholds and reduction checked
  separately on 2026-10-19, [below](#sincos-argument-reduction-2026-10-19)). LG2 is likewise
  absolute-error-limited near 1.0 (up to about 0.9M ulps in the two exponent buckets around 1).
- Subnormal inputs are flushed (RCP of 0x007fffff is +inf, LG2/RSQ/SQRT of a subnormal flush to the
  zero/inf results), NaNs come out as 0x7fffffff, and overflow and underflow go to inf and zero. The
  64H ops take and return the high word of a double.
- A single ulp bound therefore cannot describe SIN, COS, LG2 and TANH; the ops with tight bounds are
  RCP, RSQ, SQRT, EX2 and the 64H forms. Modelling this is tracked in #21. The `mufu_*` fixtures
  compare 1,462 inputs per op (specials, powers of two and neighbours, values around 1, large
  arguments, random bit patterns) with the recorded outputs. MUFU stays a runtime primitive; the
  SIN/COS CPU handler is [below](#bit-exact-reduced-angle-evaluator-2026-10-19).
- The half-precision forms (`mufu_fp16__*`, `research/mufu/fp16.py`) are much more regular:
  `.H0`/`.H1` select the half read, the result lands in Rd's low half with the high half zero, NaN
  results are 0x7fff, subnormal inputs are not flushed, and RCP and SQRT equal round-toward-zero of
  the exact result for every normal input (EX2, LG2 and RSQ with a handful of exceptions; TANH
  returns small inputs unchanged). Their fixtures and data are not in the repo yet (#21).

### SIN/COS argument reduction (2026-10-19)

**Semantics finding:** new observations (CUDA driver API 13.0) support sass2cu's proposed 25-bit
fixed-point reduction, including complement reflection. `research/mufu/sincos_reduction.py` issued
84,488 unique synthetic register-form probes through `Cases.probe` / `device_cases.observe`.
Inputs, raw outputs, instruction encodings, device/run identity and the analysis report are
committed under `research/mufu/sincos-reduction/`; its README gives GPU-run and CPU-replay
commands. These are discovery observations, not regression verification evidence. No
PTX/compiler mapping or other architecture was tested.

For finite x in turns, the supported model is:

1. `F = floor(abs(x) * 2^25) mod 2^25` (truncate, rather than round).
2. COS adds `2^23` modulo `2^25` and ignores x's sign.
3. Split F into quadrant `q = F >> 23` and `r = F & (2^23 - 1)`.
   In descending quadrants 1 and 3, replace r with `2^23 - 1 - r`, one step
   short of ordinary reflection.
4. Evaluate the same function as SIN at `r * 2^-25`. Negate for quadrants 2/3;
   SIN also applies x's sign, including signed zero.

The comparison uses the GPU's SIN output at that reduced argument, rather than an ideal
mathematical sine. All 36,248 finite target inputs per operation match this identity bit for bit
(including direct first-quadrant controls). The vectors select 4,096 offsets across a quadrant,
both input signs, all four quadrants, whole-turn translations, boundary neighbors and large
inputs. They are rounded to binary32 before deriving the expected coordinate. There are zero
mismatches in 1,280 within-cell comparisons across 768 groups (operation/sign/cell), 720 predicted
signed-zero checks, 32 zero/subnormal checks, and 20 nonfinite checks (all give `0x7fffffff`).
Ordinary reflection, measured against its own direct SIN baseline, disagrees on 14,866 SIN and
16,488 COS target inputs. Scheduling changes only control bits; the generated wrappers retain the
target's low 105 encoding bits verbatim.

The distinguishing cases include `SIN(0x32ffffff) = +0`, `SIN(0x33000000) = 0x34491200`,
`SIN(0x3effffff) = +0`, and `COS(0x3e7fffff) = +0`. The model explains the first nonzero SIN
input at 2^-25 turns and the zero-only SIN range for finite |x| >= 2^22, where binary32 spacing is
at least half a turn; at |x| >= 2^23 every input is a whole turn. COS at integer/half-integer
inputs is near +1/-1, respectively, rather than zero.

This finite experiment supports the reduction identity throughout the sampled quadrants,
including their interiors; it is not an exhaustive proof or evidence that the physical
implementation literally uses a bitwise complement. The final evaluator was initially left to the
GPU (the fixed-point follow-up below models it). Maximum residuals against ideal sine *after* this
reduction are 1.584e-7 for SIN and 1.571e-7 for COS on these vectors, not universal error bounds.

### Reduced-angle evaluator fit (2026-10-19)

`research/mufu/sin_fit.py` captured all 8,388,609 SIN outputs at `r * 2^-25` for integer
`0 <= r <= 2^23`, including the quarter-turn endpoint. The existing bulk MUFU runner
(`MUFU.SIN R11, R2` at 0x100 in the pinned CUDA 13.0.88 carry wrapper, variable-latency scoreboard
scheduling) agrees with all 16,262 distinct offsets observed previously through the independent
shared native-case harness. Repeated full captures have the same output checksum. Lossless
compressed outputs, the executed cubin, capture identity, fitted coefficients and replayable
analysis are in `research/mufu/sin-fit/`.

Fitting uses only those 16,262 earlier offsets. A separate 83,760-offset sample compares global
odd polynomials (degrees 3–15) and 16–512 aligned segments with quadratic/cubic fits. There is a
marked improvement from 32 to 64 quadratic pieces; larger tables and cubic pieces add little on
that validation set. The compact 64-quadratic model then matches 85.78% of the remaining
8,288,587 offsets exactly and 99.95% within one ulp (worst 6 ulps). Including training and
validation, its complete reduced-domain results are 85.76% exact, 99.91% within one ulp, worst 14
ulps, maximum absolute difference 5.960e-8. Ordinary sine (float64 then float32) on the same
complete domain is 28.49% exact, worst 699 ulps and maximum absolute difference 1.788e-7. These
describe the captured reduced domain, rather than an exhaustive test of argument reduction on all
binary32 inputs.

**None of these initial smooth fits is exact everywhere.** Exhaustive per-segment comparisons are
in `segments.json`: the best is segment 22 (121,874/131,072 exact, 92.98%, otherwise one ulp
away). Float64 Horner evaluation of fitted coefficients followed by float32 rounding is an
approximation; the hardware's internal coefficient representation, arithmetic and rounding remain
unresolved. The 64 pieces are a supported modelling hypothesis, not a recovered physical table.
The first segment's fitted linear gain over ideal sine is 4.165e-5, close to the hardware step
gain of 4.169e-5; the first 127 nonzero offsets are exactly proportional to the measured step. No
semantics, independent regression reference or verification evidence is added by this fit.

Residual plots and boundary comparisons (`research/mufu/sin_structure.py`, same date/device/data)
strengthen the aligned 64-segment hypothesis: on the same 101,864 validation offsets, a dense
64-piece fit is 82.62% exact, versus 31.37% with a half-segment shift; the earlier sparse fit is
82.65%. An aligned 128-piece fit reaches 82.93%. One- or two-grid-position shifts are
indistinguishable here; arbitrary nonuniform segmentations remain untested. The first four
residual curves show discontinuities at candidate boundaries, and the near-zero nonlinear
correction is a staircase. On segment 22, a linear-programming feasibility check over all 131,072
rounding intervals finds that no real quadratic with only one final nearest float32 rounding fits
them all: its minimum additional allowance is 0.297874 ulp (solver tolerance 1e-9 in ulp units).
This is numerical evidence for missing internal quantization or finer structure, not a formal
reconstruction of hardware arithmetic. Plots (`residuals.png`/`.svg`), alternative fit
coefficients and the detailed comparisons (`structure.json`) are in `research/mufu/sin-fit/`.

### Bit-exact reduced-angle evaluator (2026-10-19)

**Semantics finding:** replacing the ordinary square with a truncated sum of square partial
products and converting the final fixed-point value toward zero gives a compact model that matches
all 8,388,608 captured reduced offsets, with every one of the 64 segments exact.
`research/mufu/sin_quantization.py` recovers integer coefficients from 8,192 offsets per segment
(524,288 fitting positions), snaps them to fixed grids, and checks all remaining 7,864,320
coefficient holdouts. The arithmetic was discovered retrospectively from segment 0's captured
staircase, before applying it to the other segments.

For reduced offset `r`, let `segment = r >> 17` and `d = r & 0x1ffff`. Calculate the modified
square `I` by retaining individual diagonal terms `2^(2i)` only for `2i >= 19`, and combined
symmetric cross terms `2^(i+j+1)` only for `i+j+1 >= 19`, then adding them in units of `2^19`.
Equivalently, with `H = d >> 10` and `L = d & 1023`:

```python
I = 2 * H * H
for k in range(7):
    I += ((H >> k) & 1) * (L >> (8 - k))
```

Select integer coefficients `[a0,a1,a2]` from `quantized-model.json` and form
`N = (a0 << 12) + a1*d + a2*I`. Convert `N * 2^-37` to float32 toward zero. The first segment's
triple is `[0,25737,-14]`; its modified square is zero for all `d < 1024`. This explains both the
small-angle gain and the initially linear outputs, followed by the nonlinear staircase. All
arithmetic before conversion is integer arithmetic, with N fitting in 38 bits. These are
behavioral scales and a successful coefficient table, not measured physical register widths or a
table extracted from the binary.

Using the same coefficients but squaring normally before dropping low bits matches only 77.75% of
reduced outputs; changing final conversion to nearest-even matches 50.04%. Normalized versions of
this arithmetic fitted with 32/128 segments match 6.51%/45.70%; those comparisons do not exclude
every alternative parameterization. The 64-entry model also matches the quarter-turn endpoint
after folding, all 84,488 earlier shared-harness observations, and all 1,462 original SIN samples
and 1,462 COS samples, including signs/zeros/NaNs. Scalar enumeration independently checks the
fast square and integer float conversion.

`research/mufu/sin-fit/QUANTIZATION.md` explains the recovery and reproduction;
`quantization.json` has per-segment training/holdout results and arithmetic ablations, and
`quantization.png`/`.svg` show the corrected errors. This is an exact model of the captured SM89
reduced domain. Outer argument reduction has not been exhaustively checked on every binary32
input, and the physical circuit is not uniquely identified. Other architectures have not been
hardware verified.

The full-width SIN/COS CPU runtime now uses this model (`src/sass/emulate/include/sass/mufu.hpp`, a C++ port of the former `fp/mufu.py`). The
independent regression reference (`tests/refs/mufu.py`) enumerates square terms and rounds exact
rationals; production uses the fast high/low decomposition and integer float encoding. Runtime
tests compare every captured reduced grid point with `--exhaustive`, and the shared cases target
all 64 segment boundaries, retained-square thresholds, source modifiers and operand forms. MUFU
remains a runtime primitive; other MUFU operations and FP16 remain unresolved.

## Memory

- Implicit memory descriptor forms use ordinary global/local pointers (explicit ones below).
  Shared-memory X4 addressing scales the base register without changing its value.
- Generic addressing (2026-10-15, one-off S2R/cvta probes and the `generic` fixture in
  `tests/cases/memory.py`): the shared window is `[SR_SWINHI:SR_SWINLO, + SR_SWINSZ)` and the local
  window `[SR_LWINHI:SR_LWINLO, + SR_LWINSZ)`, each 16 MiB (`SR_*WINSZ = 0x1000000`; one run had
  bases 0x796c_59000000 and 0x796c_57000000, `SR_SMEMSZ` 0x400, `SR_LMEMHIOFF` 0xfff9c0).
  `cvta.shared`/`cvta.local` of 0 give the bases, which ptxas reads from `c[0x0][0x18]`/
  `c[0x0][0x20]` on SM75-SM89; SM90+ build the shared base from `SR_SWINHI` with a zero low word
  and read the local base from `c[0x0][0x20]` (SM90) or `c[0x0][0x2f8]` (SM100+). A generic
  `ST.E`/`ST.E.U8`/`LD.E`/`LD.E.S16` inside a window reaches that space at the address minus the
  base: LDS/LDL at the offset read what it wrote, and other generic addresses are global. Not
  checked: addresses inside a window but beyond the allocation, 32-bit (no `.E`) generic addresses,
  SM90+ cluster shared windows.
- Memory descriptors (2026-10-15, one-off probes in a scratch kernel and the `desc` fixture in
  `tests/cases/memory.py`): `desc[URx]` is PTX's 64-bit L2 cache policy. ptxas passes a
  `createpolicy` result or a policy operand unchanged into URx, and `c[0x0][0x118]`, the driver's
  default descriptor that every SM89 kernel loads, was 0. `createpolicy` values all set bit 60.
  Fractional ones put the primary priority in bits 57-58 (unchanged 0, evict_first 1, evict_last 2,
  evict_normal 3), a secondary evict_first in bit 56 and the fraction in bits 52-55
  (ceil(16f) - 1). Range ones set bit 59 and carry their window in the low bits. No descriptor
  changed a loaded or stored value. Descriptors with bit 59 clear never trapped: 40 random values,
  every single bit but 59, all 75 fractional `createpolicy` results, and 0. Of 34 with bit 59 set,
  17 trapped (CUDA_ERROR_ILLEGAL_INSTRUCTION), among them bit 59 alone and range tags with fraction
  bits; the rest, including `createpolicy.range` results, ran normally. The `desc` fixture runs
  LDG, STG and generic ST/LD (into shared, local and global memory) through bit-59-clear
  descriptors built from each thread's inputs. A descriptor is a uniform register that `R2UR`
  fills, so the GPU likely uses one lane's value per warp: the fixture establishes the accesses,
  not one descriptor per thread. What `R2UR` does with a value that differs across lanes is not
  established; the emulator, running threads alone, uses each thread's own. PTX rejects
  `.L2::cache_hint` on `.local`, so LDL/STL descriptor forms were not run.
  - Descriptor visibility (2026-10-16, shared native probes): LDG, generic LD into shared storage,
    and LDL with descriptor bit 59 alone trap with CUDA status 715 even when bit 101 is clear
    (`e_desc=noexp_desc`, descriptor omitted from disassembly). This also happens when the selected
    pair is UR8/UR9, independent of the wrapper's UR4/UR5 default descriptor. Bit-59-clear
    descriptor cases pass for both e_desc settings. The emulator now reads the selected descriptor
    in both forms and conservatively rejects range forms; this does not establish every range
    descriptor's behavior.
- LDG/LD `Pnz` and `Pu` (2026-10-15, one-off patched-LDG probes; the `pnz` fixture in
  `tests/cases/memory.py`): a false `Pnz` (after its `!`) makes `Rd` 0 and accesses no memory, so
  an unmapped address doesn't trap. A true one loads normally. The md also writes `Pnz` (it is a
  producer in the latency tables), but the register kept its value in every case: true or false,
  zero or non-zero data, any prior state. A `Pu` destination read 0 in every completed run: valid
  loads, zero-filled loads, and whatever value the predicate held before. An unmapped address still
  trapped with `Pu` set (CUDA_ERROR_ILLEGAL_ADDRESS). What sets `Pu` is unknown, so it stays opaque.

## Warps, threads and control flow

- Multi-thread forms (2026-10-14, cooperative fixtures, all lanes active): SHFL IDX/UP/DOWN/BFLY
  follow PTX shfl.sync exactly (524288 lane results with random b and c; UP's bound
  `(lane & seg) | (clamp & ~seg)` is a lower bound, the other modes check only `j <= bound`, so
  BFLY may leave the segment downward; b's bits above 4 and c's other bits are ignored), VOTE
  ANY/ALL/EQ and the ballot, and shared memory across BAR.SYNC between four warps.
- MATCH, REDUX and VOTEU (2026-10-14, cooperative fixtures in `tests/cases/threads.py`, 16 warps
  each, all lanes active) follow PTX `match.any/all.sync` (U32 and U64), `redux.sync` (add,
  and/or/xor, min/max unsigned and signed) and `vote.sync` ballot. ptxas emits VOTEU only for
  `activemask` (UPT, PT); the VOTEU fixtures hoist the predicate's LOP3 above it and patch Pp to P0
  and !P0.
- Divergent warps (2026-10-16, fixtures `div_*` in `tests/cases/threads.py`, kernels in
  `tests/kernels/threads_semantics.cu`): with only the lanes whose C bit 0 is set executing
  (patterns 0x55555555, 0x0000ffff, 0xaaaaaaaa, a single lane, ...), VOTE ballot/any/all and REDUX
  add/and/or/min range over the executing lanes, and the hardware activemask equals that set. SHFL
  (all four modes; shared cases in `tests/cases/threads.py`, observing Pu too): a valid source lane
  that is not executing the shuffle (idle at the reconvergence point or exited, even with a
  non-zero register) returns 0 with Pu true; an out-of-range source keeps the lane's own value with
  Pu false. In the compiled `__reduce_*_sync` kernels, the bare `REDUX` mnemonic is the AND
  reduction (`.SUM`, `.MIN`/`.MAX` with `.S32` for signed, `.OR`, `.XOR` are the others). ptxas puts
  `BRA.DIV UR, slow_path` (or `BRA.CONV UR, fast_path`) with UR = `__activemask()` before each
  `*_sync` intrinsic; until 2026-10-19 the Ghidra emulator stopped there.
- BRA.DIV/.CONV `[~]URb` (2026-10-19, `research/bra_div/probe.py`, 88 generated
  single-instruction probes, CUDA driver 13.0; regression cases `divergence_branches` in
  `tests/cases/control.py`): a lane is diverged when URb (inverted by `~`) names it and it is alive
  without executing the branch, e.g. waiting at a BSYNC; exited lanes never are, so with only
  exited inactive lanes CONV is always taken and DIV never. DIV is taken when some lane is
  diverged, CONV when none is, and neither when Pp is false; whether the observer lane is in URb
  doesn't matter. Tested with every inactive lane exited or every one waiting at a `BSSY`/`BSYNC`
  barrier; lanes parked at other barriers (WARPSYNC, BAR) are untested.
- LEPC (2026-10-14, one-off probe; no fixture): in a whole-program kernel whose function pointers
  come from `c[0x2]`, `LEPC R4` at offset 0xe0 minus the pointer to a callee at kernel offset 0x130
  gave -0x130 in every thread, i.e. the kernel's start, if `c[0x2]` holds absolute code addresses.
  Whether it does is not established, so LEPC stays opaque (see `docs/open.md`).

## Encoding

- Starred encoding literals (2026-10-16): `BITS_3_83_81_Pu=*7;`-style starred literals are ignored
  by the decoder but ptxas writes them. Plain `IMAD` with bits 83:81 = k < 7 clears `Pk` (checked
  for P0-P3; [above](#integer-and-predicate)); ptxas emitted 7 in 623 of 623 plain IMAD words in
  the repo's compiled kernels, and all 9 starred field/value kinds occurring in 10,152
  ptxas-emitted instructions matched the md literal every time. The encoder
  (`sass.assemble.encoding`) writes them in strict and permissive modes (issue #24: the old
  `pyencode` left them 0).

## SM90 runner validation (2026-10-22)

The local host has two NVIDIA H100 NVL devices (compute capability 9.0), driver
610.57.04. Validation uses device ordinal 0 and CUDA 13.0.88 fixture compilation.
The native assembler launch/ABI smoke test and all 1,024 compiled whole-kernel
fixtures pass on SM90, including arithmetic/conversions, carry chains, predicates,
CX and ordinary memory, shared-memory exchange, and warp collectives. These are
checks of the executed fixtures, not every legal encoding or modifier combination.

The same 1,462 recorded SM89 inputs for each of COS, SIN, EX2, LG2, RCP, RSQ,
RCP64H, RSQ64H, SQRT and TANH give bit-identical outputs on H100 NVL (14,620
sampled results; padding threads repeat the first input). The MUFU fixtures now run
on both SM89 and SM90, with separate hardware pass records. This does not establish
cross-architecture equivalence on all 2^32 inputs or validate the half-precision forms.

CX address reconstruction uses bits 0..50 in units of 64 bytes on SM90, while the
13-bit size field still counts 16-byte units. The ported CX boundary fixtures pass
at zero, small, high-bit and maximum representable sizes. Relocated VOTEU fixtures
require conservative producer/consumer scheduling: the old scheduling controls from
compiler-selected offsets were insufficient after hoisting the predicate producer.
Draining scoreboards with WAIT15_END_GROUP makes both ballot and inverted-ballot
fixtures agree with their unchanged references.

### Failures exposed by the first SM90 shared-case run (2026-10-22)

Semantics: VIADDMNMX is absent from the SM89 md and present on SM90. Its packed
S16x2/U16x2 B negation is lane-wise, before lane addition and min/max, not a 32-bit
negation followed by splitting. Raw H100 probes with A=0, B=1, C=0x7fffffff and
Pp=PT return 0x0000ffff for both packed formats, through register, bank-2 constant
and CX sources. The corresponding U32 and S32 forms return 0x7fffffff and
0xffffffff. The independent reference and native family now negate each lane.
The generated input corpus is unchanged.

Semantics/testing context: QSPC on H100 returns tag 0xa for a physical generic
shared address (shared bit 0x2 plus cluster-shared bit 0x8), for G/S/L queries;
its query predicate still identifies the requested space. SM89's corresponding
physical probes returned 0x2. The Hopper anchors now supply overlapping shared and
cluster-shared ranges; the reference and runtime permit this overlap and combine
the bits, while retaining rejection of other ambiguous provenance maps.

Testing/environment: nvcc 13.0.88 lowers the SM90 scalar IMNMX fixture to VIMNMX.U32
and several HADD2/HMUL2/HFMA2 fixtures to HFMA2.MMA. The constant-move patch uses `hfma2_mma__RRI` on SM90, corresponding to
`hfma2_mma__RRI_norelu` on SM89. Using ordinary `hfma2__RRI` during the initial port
incorrectly changed NaN canonicalization; the patch now preserves MMA. Target-execution checks
name the actual instruction; independent expected values are unchanged.

## Atomic branch integration (2026-10-25)

- Atomics (2026-10-16, 31 fixtures in `tests/cases/atomic.py`, kernels `tests/kernels/atomic_semantics.cu`; one thread per cell, 128 vectors each; references `tests/refs/atomic.py`): ATOMG integer ADD/MIN/MAX (S32/U32), INC/DEC, AND/OR/XOR/EXCH, CAS (32 and 64-bit), ADD/MIN/MAX/AND/EXCH at 64 bits, ATOMS ADD/MIN.S32/INC/EXCH/CAS, generic ATOM.E.ADD (global and shared cells), and RED ADD/MAX.S32 follow PTX `atom`/`red`: INC is `old >= b ? 0 : old + 1`, DEC `old == 0 or old > b ? b : old - 1`, and CAS stores Rc (PTX's new value) when the cell equals Rb (the compare value). F32 ADD (`.FTZ.RN`, ATOMG and RED) flushes denormal inputs and results; NaN results are FADD's 0x7fffffff. F16x2 ADD (`.RN`) doesn't flush; NaN lanes are 0x7fff. F64 ADD (`RED.E.ADD.F64.RN`) stores a NaN operand unchanged, signalling ones too, and Rb's when both are NaN; inf + -inf gives 0xfff8000000000000. Every fixture returned the cell's old value. Contention and ordering between threads are untested (`docs/open.md`).
