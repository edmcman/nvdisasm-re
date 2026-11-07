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
- [SETP aliases, FTZ widening and narrow conversions](#setp-aliases-ftz-widening-and-narrow-conversions-2026-10-29)

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
- Kernel argument sizes (2026-11-03, `tests/test_launch.py::test_aggregate_kernel_gpu`):
  the fixture runner passed 1-, 2-, 3-, 4-, 8-, 12- and 16-byte parameters in one compiled kernel
  on SM89. A weighted checksum of every parameter byte, including embedded zeros and high bits,
  agreed with the independent expected outputs and the equivalent lifted-emulator request for
  all 32 threads. SM89 and SM90 reference-emulator regressions also pass; hardware validation
  here covers SM89.
- Native probe setup (2026-10-16): variable-latency scheduling must cover constant-source ALU, FCHK
  and DSETP as well as loads. Assigning write/read scoreboards from the md scheduling fields and
  draining them resolves stale-output failures. Native local storage uses EIATTR_MIN_STACK_SIZE
  (0x12) and EIATTR_FRAME_SIZE (0x11), matching the compiled `sem_local` kernel's 1024-byte
  records; local addresses include `c[0][0x28]`. Constant bank 2 is per-kernel, while bank 3 uses a
  module-wide `.nv.constant3` section, matching ptxas output; `.nv.constant3.kernel` leaves bank-3
  inputs unreadable as intended data.
- Partial-form planner validation (2026-10-26, #80): the generated memory, int_alu, int_simd
  and fp16 selections passed 11,974 hardware-valid shared scenarios on SM89 at default
  sampling; 1,640 model-only scenarios reported explicit exclusions. Recorded outputs are in
  `tests/baseline/gpu_observations/SM89.jsonl`. This validates the tested words and operand
  rows, including ordinary LDG completion and aligned wide accesses; it does not establish
  unresolved output predicates, concurrent memory ordering or other architectures.
  The [audit report](partial-form-planning-2026-10-26.md) lists the operand contracts
  and software checks. After completing the migrated FLO/VABSDIFF4/HADD2 boundary
  corpora, the changed three families passed 7,999 shared scenarios again.
  No new SM90 hardware run was made for this change.

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
- I2I.SAT (2026-10-24, 84 `tests.probe` scenarios, then the shared `i2i_case` cases): `I2I.<U8|S8|U16|S16>.S32.SAT`
  clamps the S32 source to the destination range and writes the clamped value as 32 bits: S8/S16
  results are sign-extended (`I2I.S8.S32.SAT` of 0x80000000 is 0xffffff80), U8/U16 zero-extended. Rd's
  previous value doesn't matter.
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
- HFMA2.RELU and HFMA2.MMA.RELU (2026-10-24, 54 `tests.probe` scenarios, then the shared half-lane
  cases): `Rd = Pp ? relu(r) : r` per lane. RELU makes negative values, -0 and negative subnormals +0
  and keeps the canonical +0x7fff NaN; the RELU classes' predicate operand Pp enables it (Pp false or
  `!Pp` true gives the plain HFMA2 result). HFMA2.MMA gives the same values as HFMA2.
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
remains a runtime primitive. Reciprocal support added on 2026-11-03 is described below;
the other MUFU operations remain unresolved.

### Reciprocal precisions (2026-11-03)

`research/mufu/reciprocal_observe.py` collects raw autocase observations without computing
expectations. The SM89 RTX 4070 Laptop captures are in `research/mufu/reciprocal/`.
These discovery files alone earn no verification evidence; independent references and
precision-specific fixtures promote the established behavior.

Both RCP and RSQ were observed for all 65,536 F16 inputs with H0 and H1 extraction.
The other half contains different poison values in the two captures; selected results agree
for every input and occupy Rd[15:0], with Rd[31:16] zero. RCP matches precision-specific
round-toward-zero arithmetic exactly, with overflow to infinity and preserved subnormal
inputs. RSQ follows the same rule except for 15 positive finite inputs whose low 11 bits are
`0x713`: those outputs are one F16 ulp above the truncated result. NaNs and negative
nonzero RSQ inputs produce `0x7fff`; signed zeros produce signed infinities. The shared
integer runtime matches all four complete captures exactly.

F32 captures contain 8,962 inputs per operation: regular normalized-mantissa grids in
both exponent parities, every finite exponent boundary and its neighbors, and exceptional
values. The 64H captures contain 14,338 inputs per operation over the corresponding
double-high-word domains. The high-word contract is a 32-bit source interpreted as
`source << 32` in binary64, and a high-word destination; neither input nor output consumes
the neighboring register. F64 immediate encodings convert to this high-word contract.
Generated hardware cases cover the available register, immediate, constant, CX and uniform
forms, source modifiers, predicates, aliases and zero-register destinations.

The initial implementation used bounded integer division/square-root models. Investigation
of the existing SIN quantization notes then identified the same 15-bit retained-partial-product
square in reciprocal observations: ordinary squaring, or truncating after a complete square,
cannot satisfy all the observed rounding intervals. RCP has 128 mantissa segments;
RSQ has 64, with separate tables for the two exponent parities.

`reciprocal_fit.py` fits interval constraints and solves coefficient grids using exact integer
output-rounding bounds. Sparse fits initially matched their fitting inputs but missed independent
random captures; `reciprocal_refine.py` selects new autocase inputs where feasible coefficient
sets disagree, rather than consulting a reference. The resulting fixed-point representation
uses a 57-bit accumulator, 42 fractional constant bits, `57-local_width` linear bits and
40 quadratic bits. These widths describe a fitted realization, not recovered physical ROM
storage. F32 uses nearest-even output rounding; 64H keeps the high word with truncation.
Powers with a unit normalized significand use the separately observed exact result.

The shared runtime and independent rational reference reproduce the captured words exactly.
Complete normalized-domain observations are delta-encoded, with device identity, target form
and SHA-256 in each `*-full.json`. `reciprocal_verify.py` compares both the fitted model and
the actual C++ runtime against every captured output; `validation.json` records the domains
and mismatch counts. The complete RCP mantissa domain and both RSQ exponent parities
contain 25,165,824 F32 words and 3,145,728 64H words. All 28,311,552 observed words
have zero model or runtime mismatches. This establishes the normalized domains on SM89;
exponent transitions, exceptional inputs and operand forms are checked separately.

The hardware operations themselves remain approximations to mathematical reciprocal and
reciprocal square root. The earlier exhaustive survey's IEEE-reference distances—1 F32 ulp
for RCP, 2 for RSQ and 1 high-word ulp for 64H—are separate from model-versus-hardware
accuracy. Exact comparisons remain local to the modeled reciprocal forms.

F16's limit is zero ulps on the complete observed domain; it does not inherit an F32
tolerance. BF16 reciprocals remain explicit unresolved runtime errors. SM103's
SIMD MUFU classes permit EX2 only, not reciprocal operations; EX2 remains unresolved.
BF16 forms permitted by the machine descriptions are generated as error contracts and
do not acquire numerical verification credit. SM90 and the other architectures have
software support inferred from their descriptions, without new hardware verification.

## Memory

- ATOMS.POPC.INC.32 (2026-10-25, RTX 4070 Laptop GPU, SM89;
  [probe and raw observations](../research/popc_inc/README.md)): for a uniform shared
  address (`Ra=RZ`, UR2 plus offset), **every participating lane receives the same old
  counter value**, and memory becomes `old + popcount(participating_mask)` modulo 2^32.
  With counter 10 and lanes 0, 2 and 5, the register results are 10, 10, 10 and memory
  ends at 13: the `old + rank` hypothesis in #75 was wrong. Forty-four scenarios cover
  predication and exited lanes, full/sparse/single/zero masks, overflow, offsets
  0/+16/-16, destinations R8/RZ, and repeated executions. Four ordinary ATOMS.ADD
  controls return distinct old values, confirming the wrapper captures individual lanes.
  CUDA 13.0.88 ptxas emits the RZ form for discarded-result shared increments on
  SM80/SM89/SM90/SM100 at O1–O3; consumed results use ATOMS.ADD, SHFL and explicit rank
  arithmetic. The returning words were synthetic, not compiler-generated. Support for
  that form is deferred until a real use is found; the IR remains lane-scoped.
  Nonzero/nonuniform Ra, other architectures' return values and competing warps are
  untested. The capture's MEMBAR.ALL.CTA is separate from the target, so these probes
  establish values, not intrinsic memory-ordering guarantees or verification coverage.
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
  checked: addresses inside a window but beyond the allocation, 32-bit generic stores,
  SM90+ cluster shared windows. The 32-bit loads were investigated below.
- 32-bit generic LD (2026-10-25, synthetic SM89 probes, RTX 4070 Laptop GPU):
  `Ra + UR + offset` wraps modulo 2^32, then names the zero-extended generic address.
  `research/ld32.py` uses the shared generated wrappers and registered MAP_32BIT host memory
  whose CUDA device pointer is checked to equal its CPU pointer. All 19 observations in
  `research/ld32/observations-SM89.json` passed: positive/negative offsets, an immediate-offset
  overflow and a register-plus-uniform overflow, UR-only and absolute-immediate addresses,
  all seven ordinary load sizes, an address/destination alias, RZ, and a false Pnz.
  Nonzero R3/UR5 sentinels are ignored by the 32-bit R2/UR4 address operands and preserved
  unless they belong to the destination. The overflow probes read mapped global data at the
  wrapped address, distinguishing 32-bit addition from a 64-bit zero-extended sum.
  Truncated runtime shared/local generic pointers faulted (CUDA status 700), while paired `.E`
  controls read their initialized storage; the first probes and raw results are kept in
  `research/ld32/window-probes-SM89.json` and `window-observations-SM89.json`. These faults
  alone do not define memory-fault semantics. Together with the successful low-global probes,
  they exclude routing by low window words or treating the 32-bit address as a window offset.
  The shared regression reference is `tests/refs/memory.py:generic32`; cases include signed
  narrow values and window boundaries. A fixed registered mapping at `0x90000000` also
  distinguishes zero-extension from sign-extension: both a plain load at `0x90000100` and
  `0x10 + (-0x6fffff10)` read its global data successfully. The latter distinguishes a
  wrapped signed-offset underflow from a negative 64-bit address. This establishes SM89
  behavior, not SM90 hardware.
  CUDA 13.0.88 ptxas rejects `ld.u32 [b32]` in a 64-bit module and rejects 32-bit compilation.
  Explicit `cvt.u64.u32` followed by generic `ld.u32` emits `LD.E` with a zero high register
  (`tests/ptx/ld/generic_u32.ptx`); the non-`.E` observations are synthetic, not compiler output.
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
  - Ordinary LDG completion (2026-10-25, generated synthetic probes on SM89,
    RTX 4070 Laptop GPU, CUDA driver API 13.0): the nine reproducible scenarios in
    `research/ldg-predicate-probes.json` and
    `research/ldg-predicate-observations-SM89.json` clear Pu for completed loads.
    Both old predicate values, zero/nonzero data, zero-fill, Pu/Pnz aliasing,
    signed narrow loads, 64/128-bit loads and the four prefetch labels were exercised.
    Aliasing reads the original Pnz before clearing the shared predicate.
    A false instruction guard preserves Pu and the destination data register.
    The reference now models this ordinary-completion rule; it does not identify
    an exceptional condition that produces Pu=1. Other architectures use the same
    inferred rule; these probes establish hardware behavior only on SM89.

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
- BRA after a partial deferred BAR arrival (2026-11-06, SM89): the two synthetic
  `sync_deferred_branch_div`/`conv` cooperative fixtures have lanes 0–15 issue
  `BAR.SYNC.DEFER_BLOCKING 0, 32`, all lanes execute `BRA.DIV` or `BRA.CONV`
  with UR0 = `0xffff`, and lanes 16–31 subsequently issue `BAR.ARV 0, 32`.
  DIV is untaken in all 32 lanes; CONV is taken in all 32 lanes. This establishes
  these deferred-arrival sequences, not the behavior of BRA with participants
  parked at a blocking BAR or WARPSYNC. The pre-ordered emulator incorrectly
  split the two lane groups; the ordered body matches the GPU. The raw discovery
  observations are in `.cache/ordered-gate/deferred-branch-gpu-observation.json`;
  the retained independent expected outputs are in `tests/refs/control.py`.
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
changed the instruction class; that run did not establish a different NaN rule.
The patch now preserves MMA. Target-execution checks
name the actual instruction; independent expected values are unchanged.

## SETP aliases, FTZ widening and narrow conversions (2026-10-29)

Device scope: SM89, NVIDIA GeForce RTX 4070 Laptop GPU, loaded kernel module
580.178.04. CUDA initialized with locally extracted, checksum-verified
`libnvidia-compute-580` 580.178.04-1ubuntu1 libraries; system packages and the
running kernel were unchanged. Compiler-generated kernels used nvcc/ptxas
13.0.88 at `-O3`, without fast math. Compiled double-kernel inputs, outputs,
exact instruction words, disassembly and tool identities are retained in
`research/sass2cu-2026-10-29/`. Single-instruction regression inputs come from
the maintained autocase profiles; hardware outputs are recorded in
`tests/baseline/gpu_observations/SM89.jsonl`. Initial synthetic discovery
records are preserved in the local investigation archive and earn no
verification credit.

- **SETP output aliases:** Pu wins when Pu and Pv name the same predicate.
  Inputs are read before either result is written; disabled guards preserve the
  old predicate. Separate-output controls distinguished the two results.
  The initial 18,350 vectors covered ISETP with and without `.EX`, both UISETP
  classes, FSETP, DSETP (including MIN/MAX), and HSETP2 with both lane reductions.
  Comparisons, boolean operations, predicate inputs, integer signedness,
  floating unordered/constant comparisons and input/output aliases were varied.
  Semantics explicitly write Pv before Pu. SM89 has no UFSETP class; its shared
  implementation is supported by inference, not a UFSETP hardware observation.
- **F2F.FTZ.F64.F32:** F32 subnormal inputs flush to signed zero before widening.
  Normal values widen exactly. NaNs retain the original register sign and
  top-aligned payload and are quieted; absolute/negate modifiers do not change
  that NaN result. FTZ leaves NaNs intact. The initial 144 vectors crossed
  signed zeros, subnormal/normal boundaries, finite values, infinities and
  signed quiet/signalling NaNs with the source modifiers. Other FTZ conversion
  directions remain explicitly uninvestigated.
- **Ordinary half arithmetic:** The October 22 canonical-NaN observation is
  retained and supplemented with exact-bit HADD2/HMUL2/HFMA2 cases for each
  operand and lane, signed quiet/signalling payloads, mixed finite/NaN lanes,
  multiple NaNs, invalid arithmetic, saturation and broadcasts. Exact-mode
  expectations require canonical positive `0x7fff` per unsaturated NaN lane;
  SAT maps NaN results to positive zero. Readable mode
  retains its documented numerical contract and skips NaN payload assertions.
- **Compiled double fmin/fmax:** Each runtime-input kernel executed 64 pairs.
  The emitted sequence uses `DSETP.MIN/MAX.AND`, high-word FSEL, low-word SEL
  and guarded LOP3 quieting. A numeric operand wins against one NaN. Two NaNs
  return the second operand with its quiet bit set, preserving sign/payload.
  Mixed signed zeros return negative zero for fmin and positive zero for fmax,
  independent of order; two negative zeros remain negative. This is evidence
  for the compiled sequence, rather than one DSETP instruction in isolation.
- **Narrow F2I:** The initial 1,768 vectors covered S8/U8/S16/U16 from
  F16/F32/F64, rounding modes, saturation boundaries, NaNs and infinities.
  Signed numerical results extend through the full 32-bit destination;
  unsigned results are zero-extended. `.NTZ` produces zero for NaNs. Without
  `.NTZ`, the narrow destination's top-bit NaN sentinel is zero-extended even
  for signed destinations: S8 NaN gives `0x00000080`, whereas a numerical
  saturated minimum gives `0xffffff80`; S16 gives `0x00008000` versus
  `0xffff8000`. Narrow BF16 sources and predicate-result forms remain opaque.

Reference-backed SM89 validation passed 28,504 shared GPU scenarios across
int_alu, fp32, fp64, fp16 and conversions, plus the whole-kernel fixtures;
375 emulator-only scenarios were excluded.
An additional exhaustive run passed 83,524 shared fp16/conversion scenarios,
with 368 emulator-only exclusions.
After adding explicit HFMA2 zero-times-infinity inputs, the final exhaustive
fp16 rerun passed 54,472 shared scenarios, with 43 emulator-only exclusions.
Exclusions are opaque/error assertions or synthetic launch constants. Recorded outputs live
in `tests/baseline/gpu_observations/SM89.jsonl`; only passing reference-backed
cases and fixtures contribute verification evidence.

No SM75 GPU was used. New ptxas traces establish HADD2 conversion's packed
high-lane duplication and RZ conversion folding into HSET2/HSETP2 comparisons.
These are compiler evidence, documented in `docs/semantics.md`, and create no
SM75 GPU verification. Mixed HADD2/HMUL2/HFMA2 F32 inputs and SM75 two-input
HADD2.F32 now use compiler-backed inferred models; no new GPU observations
were made. Mixed-input FTZ/FMZ ordering and SM75 NaN bits remain unverified.
The follow-up SM89 shared fp16 run passed 8,484 scenarios with 43 emulator-only
exclusions after extending the SM75 comparison adapter. It verifies the
affected SM89 cases, not the SM75-only F32 selectors. Live compiler RE and
saved Ghidra annotations locate the differing HSET2/HMUL2 promotion decisions;
these remain compiler evidence and create no SM75 hardware claim.
Further compiler-only investigation found that default-rounding add/mul can
fold RZ conversion into mixed HADD2/HMUL2, and implicit mul/add can contract
to mixed HFMA2 in all three operand positions. Explicit `.rn` inhibits that
promotion. This corrects the scope of the earlier negative compiler findings;
it adds no SM75 hardware evidence. Standalone conversions plus half operations
support the inferred input rounding when the compiler preserves the PTX result.
## Half requests R1–R12 (2026-11-02, SM89)

Device: NVIDIA GeForce RTX 4070 Laptop GPU, CUDA driver API 13.0.
Compiler fixtures use nvcc/ptxas 13.0.88, `-O3`, without fast math;
the decoding oracle is nvdisasm 13.4.92. Discovery uses synthetic
autocase encodings and `device_cases.observe`, independently of expectations.
`research/half_precision_2026_11_02.py` reproduces the request forms and saves
instruction words, original inputs, raw outputs, tool paths and SHA256s.
The runtime `__half2float` fixture is compiler-generated, rather than synthetic.

The V16 domain is the supplied 28 signed values: zeros, `0001`, `03ff`,
`0400`, `0401`, `1c00`, `3c00`, `7bff`, infinities, quiet NaNs `7e00`,
`7e01`, `7fff`, and signalling NaNs `7c01`, `7dff`. Representative
two-source forms use all 784 ordered pairs. Three-source forms cover
every pair of positions with the remaining source in `{+0, 1, +inf, qNaN}`;
high lanes differ deterministically and duplicate vectors are removed.
Swizzles and sign modifiers use smaller samples. BF16 has a separate
24-value domain and rationally chosen witnesses.

These are discovery counts, not verification credit:

| Request | Observations | Established result |
|---|---:|---|
| R1 | 196 | HADD2.F32 H0_H0/H1_H1, FTZ/no-FTZ: every tested NaN sign/payload gives `0x7fffffff`. |
| R2 | 18,839 | Ordinary HADD2/HFMA2 FTZ flush signed input subnormals and exact results below minimum normal, before rounding. SAT, half swizzles, absolute/negate and `-RZ` agree with that rule. |
| R3 | 33,506 | MMA FTZ with SAT/RELU follows the same rule. HMUL2 FMZ still zeroes a zero/subnormal factor before multiplication, including multiplication by infinity. |
| R4 | 1,904 | Min/max register predicates, inversion, both predicate values, signed-zero/two-NaN pairs and source modifiers agree with the existing reference. |
| R6 | 17,178 | FMA midpoint witnesses require direct half rounding; overflow brackets, cancellation zeros and tiny-product/large-addend cases agree. |
| R7 | 36 | Ordinary HFMA2 `-RZ, RZ, hi, lo` preserves finite/zero/subnormal/infinity bits and canonicalizes every NaN lane to `0x7fff`. |
| R8 | 1,104 | Bank-2 H0_H0/H1_H1 comparison/min/max sources support negate, absolute and negate-absolute in the measured forms. |
| R9 | 17,251 | Discovery includes equal sources and in-place H1 widening; shared regression profiles additionally cover each destination alias and independently false P1 guarding equal HSETP2 outputs. |
| R10 | 32 | F2I.FTZ.F16.TRUNC.NTZ on H0 agrees bitwise on V16 and representable integer-boundary witnesses. |
| R12 | 17,533 | HFMA2.BF16_V2 and RELU are fused, RN once, preserve subnormals and return canonical positive `0x7fff` NaNs. RELU is enabled by Pp. |

R1 replaces an unmeasured `0x7fc00000` assumption; it is a widening
instruction observation, independent of F2F conversion behavior.
R3 distinguishes pre-rounding flushing: `0400 * 3bff` rounds to `0400`
without flushing but gives `0000` with FTZ/FMZ; the negative witness gives
`8000`. `1c00 * 1c00` also gives a subnormal result from normal inputs.
R6's `(3c01, 3bff, 0009)` gives `3c01`, whereas a binary32 intermediate
would give `3c00`; `(3c02, 3bff, 000f)` gives `3c01` rather than `3c02`.
Both signs, and sums/FMA results bracketing ±65520, are included.
R12's exact rational reference rejects input flushing on 2,593 observations;
tiny addends on either side of a BF16 midpoint also distinguish binary32
intermediate rounding.

R5 changes harness bindings: retained affected constants now use bank 2,
and immediate/uniform HADD2 cases no longer carry unused memory.
R7's missing ordinary constant move was a planning gap: three-modifier
interaction coverage did not select the entire required modifier
conjunction. Explicit anchors now keep it in the shared plans.
The shared GPU suite records reference-backed results in
`tests/baseline/gpu_observations/SM89.jsonl`; raw discovery earns no evidence.
The exhaustive SM89 verification passed 540 tests in 653.48 seconds:
313,861 hardware scenarios, including the complete representative domains and the
compiler-generated runtime widening fixture. The selection also contains
326 emulator-only cases: 324 use driver-owned launch constants and two
assert errors or opaque semantics. Reproduce with the existing private
driver-library environment and
`nice uv run pytest tests/test_cases_on_gpu.py tests/test_refs_on_gpu.py -m gpu --require-gpu --family fp16 --family conversions --exhaustive -q`.

The final full-form audit added 113 explicit SM89 anchors. Regeneration
changed the selection of 1,528 forms; an exhaustive follow-up through the
same shared GPU harness passed all 74,336 scenarios, with no emulator-only
cases, in 199.68 seconds (17 class-level tests). That audit checked each
requested complete form against both the shared plan and its GPU evidence;
covering each three-modifier interaction is insufficient for that purpose.

A subsequent anchor review released 160 SM89 specifications. The regenerated
plans preserve all existing three-way requirement coverage, while min/max's
source modifiers, broadcasts and predicate inversion are audited separately.
Original requested forms remain checked against their recorded reference-backed
observations; discovery captures receive no credit. The revised default SM89
half selection passed 10,223 hardware scenarios in 34.67 seconds (68 tests),
with one emulator-only opaque/error assertion. This changes the regression
selection, not the numerical findings above.

Hardware claims above apply to the measured SM89 forms. Corresponding
semantics on other architectures are inferred where the machine descriptions
permit them. HADD2/HFMA2 FMZ and unresolved mixed-F32 flushing remain guarded.

### Focused half request verification (2026-11-02)

The subsequent request-by-request review replaced all 451 request-specific
modifier-plan anchors with focused autocase input profiles. Their 548
declared checks deduplicate to 545 exact SM89 scenarios. All passed bitwise
reference-backed GPU verification as part of 13,184 default half/conversion
hardware scenarios (203 tests, 35.62 seconds). The device and private-driver
setup are unchanged. Instructions originate from the maintained adapters
and native assembler; recorded observations retain their words, inputs and
outputs. Generic three-way modifier coverage remains unchanged, including
BF16 as a format selector.

R10 now includes ten additional neighboring half values around integer
thresholds. For F2I.FTZ.F16.TRUNC.NTZ.H0, inputs
`3bff,3c00,3c01,3fff,4000,4001` produce integers `0,1,1,1,2,2`;
their sign-bit partners produce `0,-1,-1,-1,-2,-2`, encoded in
two's-complement 32-bit outputs. Together with V16 and the existing
half-integer inputs, the focused conversion domain has 42 values.

R12's focused BF16 domain contains 48 vectors, each checked with ordinary
FMA and RELU enabled/disabled by its predicate. Added overflow brackets
`7f7f * 3f80 + c`, for `c=7aff,7b00,7b01`, produce
`7f7f,7f80,7f80`; sign-mirrored inputs produce
`ff7f,ff80,ff80`. Added zero-times-infinity and opposed-infinity
witnesses produce canonical positive NaN `7fff`. Existing exceptional,
subnormal, fusion and rounding observations remain valid; reducing routine
case generation does not change their numerical findings or verification
scope.

### Future SM75 hardware questions (2026-11-02)

These are supplied hypotheses, not new numerical observations. SM75-only
encodings must run on SM75 hardware, not SM89:

| Form | Inputs | Half-rounding hypothesis | Alternative to distinguish |
|---|---|---|---|
| HMUL2.F32 H0_H0/H0_H0 | `3c01`, `3c01` | `3f804000` | Exact product `3f804008` |
| HFMA2.F32, three H0_H0 sources | `3c01`, `3c01`, `+0` | `3f804000` | `3f804008` |
| Two-source HADD2.F32 | `7bff`, `7bff` | Infinity `7f800000` | Finite sum `47ffe000` |
| Two-source HADD2.F32 | `3c00`, `1000` | `3f800000` | `3f801000` |
| HADD2 with F32 and H0_H0 sources | `3f801800`, `1000` | `3c003c00` | `3c013c01` |
| HADD2.FTZ with F32 and H0_H0 sources | `35800000`, `0400` | `04000400` | `04100410` |

Also test SAT/FTZ with F32-result multiply/FMA; FTZ with an F32 source
in each multiply/FMA position; mixed-source signs, constants and high
lanes in HMUL2/HFMA2/HSET2/HSETP2; widened NaNs and ordinary constant-move
NaNs; and the full half corpus with default contraction enabled.
SM89 observations do not settle those SM75 questions.

### Multidimensional launches and partial warps (2026-11-03)

The 43 `launch_geometry` fixtures passed on the SM89 NVIDIA GeForce RTX 4070
Laptop GPU (CUDA driver API 13.0). Three launch shapes check all six TID/CTAID
coordinates, blockDim/gridDim.xyz and lane IDs against independently enumerated
block-major, X-fastest coordinates:

| Grid | Block |
|---|---|
| (2, 3, 1) | (3, 2, 1) |
| (2, 1, 3) | (2, 3, 2) |
| (1, 2, 2) | (1, 1, 3) |

Four cooperative fixtures launch grid (2, 2, 1) with blocks (1, 1, 1),
(17, 1, 1), (3, 11, 1) and (3, 3, 5). Ballots contain exactly the existing
lanes whose input is odd; shuffles read lane 0 of each block-local warp.
WARPSYNC with a full member mask completes for partial warps, as do block
barriers around shared-memory exchanges. Every block's inputs differ, checking
shared-memory isolation. The fixture patches ptxas's converged-sync NOP into
WARPSYNC; on SM89 it uses immediate mask `0xffffffff`.

The SM90 fixtures have independent reference expectations and run in the CPU
engines; this run adds no SM90 hardware observation.

### Reconvergence and deferred barriers (2026-11-03)

The nine `sync_*` fixtures in `tests/harness/synchronization.py` passed on the SM89
RTX 4070 Laptop GPU. They exercise divergent and nested BSSY/BSYNC, ID reuse,
predicated membership, exited participants, ordinary and deferred BAR generations,
independent IDs with 32-thread counts, register/immediate ID and count operands,
and a shared-memory exchange combining reconvergence, deferred BAR and MUFU.RCP.
Each fixture asserts that its target instructions were visited in emulation.
The BSSY-target fixture establishes that BSYNC falls through to its next instruction
even when the recorded target is farther away. Exited lanes do not prevent convergence
or a default-count block barrier from completing.

`research/deferred_barrier.py` compiles the divergent timing kernels in
`tests/kernels/deferred_barrier.cu`, verifies the compiler's DEFER_BLOCKING encoding,
then compares patches retaining the original scheduling controls except where named.
`research/deferred-barrier-SM89.json` records code, patches, tool versions/hashes and
eight raw repetitions per variant. One warp delays its arrival by 200,000 clock ticks.
With DEFER_BLOCKING, the earlier warp's subsequent global-timer sample precedes the
delayed arrival by about 91–92 microseconds. Replacing it with ordinary BAR.SYNC
removes that lead. Assigning a read scoreboard to the deferred BAR and waiting for it
in the timer's request bits does not force the timer to wait.

Inserting LDS, MUFU.RCP, S2R or BRA before the timer removes the lead even without
an explicit dependency wait; replacing LDS with a fixed MOV preserves it. The timing
consumer's data operand is zeroed in every variant so that a loaded value cannot be
mistaken for elapsed time. These observations establish blocking at subsequent
decoupled instruction issue on these SM89 forms, rather than at every following
instruction or solely at an explicit scoreboard wait. The runtime applies the machine
description's decoupled classification to other instructions by inference.

Python, native and Ghidra run the fixtures under round-robin, reverse and reproducible
seeded orders, including partial warps and invalid/incompatible waits. The form sharing
one physical register for both BAR ID and count is explicitly unresolved; reductions,
arrival-only forms, BREAK and BMOV remain unsupported. This run adds no SM90
hardware evidence and does not establish full YOLO replay.
## Additional opcode investigation (2026-11-03, SM89)

The observations and tool hashes are retained in `research/additional-opcodes/`.
The GPU is an RTX 4070 Laptop GPU. Compiler probes use CUDA 13.0.88 ptxas;
decoding uses CUDA 13.4.92 nvdisasm. Other architectures' implementations are
inferences from their corrected machine descriptions, not hardware verification.

- `ATOMG`: 384 integer discovery cases distinguish ADD, signed MIN, unsigned MAX,
  INC, DEC, EXCH, and CAS, including destination aliases with the address/source
  registers and RZ sinks. Ordinary completion clears Pu. Returned values and
  memory updates agree with `tests/refs/atomic.py`'s integer rules.
- Floating atomic ADD: FP32 flushes input and result subnormals, rounds RN-even,
  and produces positive `0x7fffffff` for NaN/invalid results. Packed FP16 retains
  subnormals and produces `0x7fff` independently per half. FP64 retains
  subnormals; it preserves a NaN operand's bits without quieting signaling NaNs,
  preferring the source value over old memory when both are NaNs. Opposite
  infinities give `0xfff8000000000000`. The 136 initial observations and 56 FP64
  follow-up observations cover signed zero, ties, overflow, cancellation, and
  exceptional values. These rules have independent references.
- `SAFEADD.64` is not ordinary integer ADD: 48 discovery observations include
  changed returned bits and memory tag bits. Its encoding, update, and returned
  predicate require further investigation; it remains opaque.
- `LDSM.16.M88/MT88`: row-owning lanes provide shared addresses; each lane
  receives two halfwords per matrix. Direct and uniform-address observations
  agree with the mapping in `tests/refs/matrix.py`. The adapter snapshots the
  address operand of every lane before destination writes. Packed/expanded
  modes remain unresolved.
- `BAR.RED` and `B2R.RESULT`: POPC returns the number of true input predicates.
  AND/OR return `0xffffffff` for true and 0 for false. Pu is result != 0.
  Every participant arrives even when its reduction predicate is false.
  Thirteen cooperative regression fixtures cover predicate inversion, 64
  participants, sinks, and barrier reuse. Their GPU passes are recorded.
  BAR/WARP were unresolved in this initial investigation; the 2026-11-04
  cooperative discovery below establishes their state encoding.
- `BRX` without a depth modifier uses next PC + a 64-bit register pair + the
  signed immediate byte offset. Three cooperative GPU fixtures distinguish
  positive/negative targets and Pp inversion. INC/DEC depth forms are unresolved.
- Async copies: ptxas lowers cp.async issue/commit/wait to LDGSTS,
  LDGDEPBAR (with an assigned write scoreboard), and DEPBAR.LE. False Pnz
  zero-fills in both ordinary and ZFILL forms. Dependency waits and encoded
  scoreboard waits make completed copies visible in shared memory. The
  cooperative regressions cover 4/8/16-byte copies, two blocks, thresholds 1/0,
  empty groups, scoreboard 3, deferred barriers, and packed uniform operands.
  In the packed form URn is the 32-bit shared address component and URn+1 is
  the high word of the cache policy; adding the entire UR pair as an address is
  incorrect. The tested nonzero policy high word is `0x10000000`.
- `HMMA.16816.F32` with F16 inputs: 60 raw autocase observations establish
  lane ownership for coordinate/identity matrices and retention of accumulator
  fragments. Sixteen products `1 * 2^-24` added to accumulator 1 give
  `0x3f800008`; sixteen products split equally between +1 and -1 with accumulator
  `2^-24` give +0. Repeated scalar FMA and an exact dot rounded only once cannot
  both explain these results. The numerical reference was unresolved at this
  stage; later probes below establish the direct F16/F32 model. Sparse/indexed
  forms and other formats remain unresolved. Discovery earns no verification credit.

Further autocase probes on 2026-11-03:

- All 1,102 hardware-runnable atomic/matrix shared cases passed their independent
  references. Four atomic contention fixtures also passed: 32/64-bit ADD within
  one warp and across two blocks. Each returned old value selects an output slot,
  so the comparison checks unique, contiguous old values without assuming a
  hardware thread order.
- Twenty-four LDGSTS observations explicitly capture P0 and shared bytes after
  commit/wait. P0 retains its original value for both predicate inversions,
  ordinary/ZFILL, and 4/8/16-byte copies. This supports an identity write to Pnz,
  matching the md's read/write role. The dependency-aware `async` device adapter
  now permits referenced single-instruction regression captures.
- `hmma-grouping-SM89.json` contains 85 raw observations. Two half-ulp products
  at K=0 and K=1, 2, or 4 increment accumulator 1 by one FP32 ulp; placing them
  at K=0 and K=8 or 15 loses both. Products +1 at K=0 and -1 at K=1 through 7
  retain accumulator `2^-24`; placing -1 at K=8 through 15 loses it.
  Alternating +1/-1 also retains it. These observations distinguish the two
  eight-term halves. They do not establish the precision or rounding of each
  half's sum.
- `hmma-rounding-SM89.json` adds 103 raw observations. With one product
  `A * 2^-24`, A = 0.5, 1, or 1.5 leaves accumulator 1 unchanged; A = 2.5,
  3, or 3.5 increments it by one FP32 ulp. The same increments occur with
  accumulator `1 + 2^-23`. This rules out round-to-nearest-even for these
  positive additions and agrees with truncation. NaNs in A, B, or C and
  opposite infinities produce `0x7fffffff`; a minimum FP32 subnormal
  accumulator survives zero products, while negative-zero products with
  a negative-zero accumulator produce positive zero. Negative additions,
  internal precision, overflow, and other matrix forms remain unestablished.
- `async-packed-SM89.json` contains 32 raw LDGSTS.32 observations across
  UR6/UR7 and UR12/UR13, ordinary/ZFILL, predicate inversion, and true/false
  P0. Every zero packed policy traps with CUDA status 715; every policy
  `0x10000000` completes normally. This validity check precedes Pnz's
  zero-fill decision. Unlike ordinary 64-bit cache-policy operands, the
  packed high-word form cannot use zero as its policy. Other packed policy
  values and hardware beyond SM89 are unverified.

HMMA model refinement on 2026-11-03:

- `hmma-alignment-SM89.json` distinguishes two factorizations of product 2.25.
  With four additional products `2^-24`, `1.5 * 1.5` gives `0x40100001`,
  whereas `1 * 2.25` gives `0x40100000`. Products remain unnormalized during
  alignment. Sixteen products `±0.5 * 2^-24` with accumulator 1 are all discarded.
  This establishes individual truncation with one extra alignment bit rather
  than an exact eight-term sum.
- `hmma-model-SM89.json` retains 628 raw observations, including sixteen seeded
  matrices across every lane, cancellation/rounding inputs, half subnormals and
  maximum finite halves, signaling NaNs, zero times infinity, infinities and
  signed zero. All 2,512 outputs match the candidate integer model. The independent
  reference now quantizes exact rational products at the common exponent and
  truncates the summed result; it does not use the implementation's integer formula.
  The inputs are promoted to the `hmma_forms` autocase profile; these raw files
  themselves still earn no verification credit.
- The direct `HMMA.16816.F32` F16 form uses consecutive K=0..7 and K=8..15
  blocks, each including its incoming accumulator, with one extra fractional
  alignment bit and truncation after normalization. NaN/invalid results are
  `0x7fffffff`; an exactly zero sum is positive zero. Other selectors are guarded.
  The implementation also supports other architectures by inference from
  [Khattak and Mikaitis, Accurate Models of NVIDIA Tensor Cores, v3](https://arxiv.org/html/2512.07004v3):
  SM80/86 use the same eight-term model, while SM90 and Blackwell use one
  sixteen-term block with two extra alignment bits. No non-SM89 hardware
  verification is claimed. SM75's different shapes and selectors remain opaque.

Whole-warp HMMA fixtures on 2026-11-04:

- All three direct F16/F32 source/destination alias fixtures pass the independent
  reference on SM89. An immediately following vector store captured unchanged
  upper destination registers; two fifteen-cycle NOP slots before the store
  resolve the fixture hazard. This establishes a valid probe schedule, not an
  exact latency or scoreboard-completion model.
- The planner now admits explicitly constrained partial-primitive operand
  adapters. HMMA uses UPp=UPT where that operand exists; other accumulator
  predicate values retain unresolved semantics and are not verified by these cases.

Dense TF32 and HMMA operand controls on 2026-11-04:

- On the RTX 4070 Laptop GPU (SM89), `HMMA.1684.F32.TF32` uses one four-product
  block; `HMMA.1688.F32.TF32` uses two consecutive four-product blocks. Each
  block includes its incoming accumulator, leaves products unnormalized, aligns
  with one extra fractional bit, truncates each aligned term, then normalizes
  and rounds the sum toward zero. The lower thirteen input bits are ignored,
  including NaN payload bits: a NaN whose entire payload is in those bits becomes
  infinity. Retained NaNs and invalid operations produce `0x7fffffff`; zero
  sums produce positive zero. Accumulator subnormals survive, finite overflow
  produces infinity, and overflowing intermediate products can cancel within
  a block. The two `hmma-tf32-168*-SM89.json` discovery files retain the mapping,
  cancellation, exceptional-value and seeded-matrix observations.
- `hmma-tf32-precision-SM89.json` and `hmma-tf32-alignment-SM89.json` distinguish
  the smallest alignment quantum when C is zero. Thirteen precision probes and
  nineteen cancellation probes establish a minimum alignment exponent of
  **-132**, hence a quantum of `2^-156`, on this device. This differs from the
  **-133** reported for A100, L40S, RTX Ada 1000, H100 and B200 in
  [Khattak and Mikaitis, v3](https://arxiv.org/html/2512.07004v3).
  The reference retains the published floor on other architectures and the
  observed floor on SM89. Neither this device's result nor the model verifies
  every other SM89 device. SM80/86's four-term grouping and SM90/Blackwell's
  eight-term grouping with two extra bits are specification/paper inferences,
  not local hardware verification.
- The 178 observations in `hmma-controls-SM89.json` establish that UPp gates
  only the initial C input, with inversion honored. Disabled C contributes
  zero even when its registers contain NaNs. Negating Ra or Rb negates the
  corresponding matrix entries; negating both preserves the product.
  Predicate probes cover F16/16816 and TF32/1684; negation probes cover those
  shapes and TF32/1688. These discovery files earn no verification credit.
  Their inputs are promoted into independent rational references and the
  `hmma_forms` / `hmma_tf32_forms` autocase profiles.
- Exhaustive dense-form regression exposed 168 negative-underflow cases where
  IEEE round-toward-zero packing returned negative zero but SM89 HMMA returned
  positive zero. All 672 output words differ only in that sign bit. HMMA clears
  the sign of a result that becomes zero during final normalization/truncation,
  as well as of an exactly zero aligned sum. The matrix reference and TF32
  implementation apply this operation-specific rule; shared IEEE packing retains
  its ordinary signed-zero behavior.
- CUDA 13.0.88 `ptxas` emits the two TF32 shapes from the retained
  `tests/ptx/hmma/tf32_1684.ptx` and `tf32_1688.ptx` `mma.sync.aligned` probes.
  Compiler emission establishes encoding, not the accumulation model.

Dense BF16 and physical source aliases on 2026-11-04:

- SM89 `HMMA.16816.F32.BF16` uses the packed F16 lane/register ownership
  with BF16 operands. It performs two consecutive eight-product blocks, one
  extra alignment bit, truncation of aligned terms and round-toward-zero
  normalization. Products remain unnormalized through each block, permitting
  overflowing products to cancel. Subnormal inputs and accumulators, infinities,
  signaling/quiet NaNs, finite overflow and zero signs are included in the
  independent rational reference and autocase profile.
- `research/additional-opcodes/hmma-bf16-SM89.json` retains 704 autocase-derived
  discovery observations with no harness errors. Eight precision probes
  distinguish a minimum alignment exponent of -132: q=1..6 produce the least
  positive F32 subnormal, while q=7..8 produce zero. Other architectures retain
  the published -133 floor and inferred grouping/precision; local hardware
  verification covers SM89 only. Discovery records themselves earn no credit.
- The promoted SM89 shared suite passes 5,892 cases across F16/16816,
  BF16/16816 and TF32/1684/1688, including all source-negation and accumulator
  predicate-inversion combinations. Lane-distinguishing coordinate and identity
  inputs also cover seven legal source-overlap placements, plus destination
  overlaps, RZ sources/sinks and predication. The strict encoder rejects
  misaligned spans: F32 C requires four-register alignment; overlapping B can
  begin at R18 because its span requires two-register alignment.
- Twelve whole-warp SM89 GPU fixtures pass, with each of the four shapes writing
  separately or over either source register group. Python/native cooperative
  fixtures additionally exercise round-robin, reverse and seeded schedules.
- CUDA 13.0.88 `ptxas` emits `HMMA.16816.F32.BF16` for the retained
  `tests/ptx/hmma/bf16.ptx` instruction
  `mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32`. Compiler traces pin
  version and hashes separately from numerical hardware evidence.
- Final exact/readable Ghidra matrix validation passes 106 tests and 99,880
  selected shared cases over all ten architectures in 408 seconds. Architecture
  coverage is distinct from the SM89-only numerical hardware evidence.

Control discovery on 2026-11-04:

- CUDA 13.0.88 ptxas inserts `YIELD` and `CCTL.IVALL` between an acquire GPU
  load and its consumer in the bounded loop retained as
  `research/additional-opcodes/control-acquire.ptx`. The producer uses a release
  GPU store. Compiler lowering alone does not establish either instruction's
  state transitions.
- `nice uv run python -m research.control_observe --out PATH` reproduces 32 SM89 launches with
  producer threads 0, 15, 31 or 63 and blocks of 64 or 128 threads. All consumers
  observe one with the original YIELD, a NOP replacement, a false Pp operand,
  or a false instruction guard. Each polling loop has a 1024-iteration cap.
  `yield-scheduling-SM89.json` pins compiler and image hashes and retains these
  outputs. The test is inconclusive about distinguishing scheduler behavior;
  YIELD and CCTL remain unresolved and these observations earn no verification
  credit.
- The revised probe records the number of polling loads in
  `yield-poll-counts-SM89.json`, separately from those original captures.
  All 32 launches execute the polling path: each consumer loads one or
  three times, and every producer has zero polling loads. The four YIELD
  variants produce identical per-lane flags and counts for each launch
  geometry. This confirms activity on the path containing YIELD in the
  original kernel but still does not distinguish its scheduling behavior.
  The compiler, PTX source and original/patched instruction words are pinned;
  no semantics or verification credit is inferred from these observations.

Dense F16 tensor outputs on 2026-11-04:

- `HMMA.16816.F16` uses the same A/B lane ownership as the F32-output form.
  Each lane's four C/D half values occupy two packed registers, in the same
  matrix-coordinate order as the four F32 registers. The retained 738 raw
  observations in `research/additional-opcodes/hmma-f16-output-SM89.json`
  establish layout, staging, rounding boundaries, exceptional values and
  accumulator predicates. They are synthetic instruction probes, without
  verification credit.
- SM89 performs K=0..7 and K=8..15 as separate stages, rounding C back to
  F16 with RN-even after each stage. Products are unnormalized and terms are
  truncated on the 24-significand-bit plus one-extra-bit alignment grid.
  The aligned sum rounds directly to F16; an intermediate F32 RZ result
  incorrectly loses information needed for the half rounding.
- With C=1, products `1 * 2^-11` and `2^-24 * 1` produce `0x3c01`.
  Rounding through F32 instead predicts `0x3c00`. Replacing the second
  product with `2^-24 * 0.5` gives `0x3c00`, distinguishing the alignment
  grid from an exact dot. Half-ULP pairs within one stage combine; pairs
  straddling K=7/8 round independently. Seeded full-range finite half
  inputs distinguish four-product stages and scalar half FMAs.
- NaNs become positive `0x7fff`, infinities propagate unless an invalid
  product or opposing infinities produce NaN, and exact or underflowed
  zeros are positive. Input and accumulator half subnormals are preserved
  before alignment and final rounding.
- The promoted matrix regression passes 7,108 SM89 shared cases across
  all forty supported dense selector forms and fifteen whole-warp fixtures.
  The F16-output fixtures overwrite each source group in turn. The retained
  PTX probe `tests/ptx/hmma/f16_output.ptx` uses
  `mma.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16`; compiler emission
  remains separate from numerical hardware evidence.
- Other architectures' F16 output staging is inferred from their existing
  tensor grouping models and these SM89 observations, not numerically
  hardware-verified here. Legacy SM75, other shapes, sparse metadata and
  indexed-register forms remain unresolved.

Dense eight-product tensor forms on 2026-11-04:

- SM89 `HMMA.1688.F32`, `HMMA.1688.F32.BF16` and `HMMA.1688.F16`
  use two packed A registers and one packed B register per lane. The row,
  column and half ownership follows PTX `mma.m16n8k8`. F32 C/D occupies
  four registers; F16 C/D occupies two packed registers.
- A single eight-product stage uses the same unnormalized products and
  one-extra-bit alignment grid as the corresponding `16816` stages.
  F32 normalization truncates; F16 rounds directly RN-even. Pair probes
  at K=0 and K=4 distinguish the eight-product stage from two four-product
  stages. BF16 retains SM89's previously established minimum alignment
  exponent of -132.
- The retained raw discovery captures contain 686 F16/F32, 704 BF16/F32
  and 725 F16/F16 observations, with no device errors. They match the
  independently referenced candidate model but earn no verification credit.
  Source profiles omit K indices outside the shorter matrix.
- Promoted regression passes 9,964 SM89 shared cases across all 64
  supported dense selector forms and 24 whole-warp source-overlap fixtures.
  This includes source negation, accumulator predicates and their inversion.
  SM90+ precision remains inferred from the published models; these SM89
  runs do not numerically verify other devices.

Dense E6M9 tensor inputs on 2026-11-04:

- SM89 `HMMA.1688.F32.E6M9` and `HMMA.16816.F32.E6M9` use the same packed
  lane/register ownership as F16/BF16. E6M9 inputs have one sign bit, six exponent
  bits with bias 31, and nine fraction bits. Exceptional values follow the
  existing tensor NaN/infinity model.
- The 1688 instruction performs one eight-product stage; 16816 performs two.
  Unnormalized products align with C on the one-extra-bit grid, then each stage
  truncates to F32. Cancellation, product normalization, rounding-boundary and
  lane-distinguishing cases agree with the independent rational reference.
- Retained discovery contains 686 and 699 observations respectively, with no
  device errors or reference mismatches. The files retain actual operand groups
  and lane register inputs, with shared contexts deduplicated. Reproduce their
  reference comparison with `nice uv run python -m research.matrix_observe
  --check-capture research/additional-opcodes/hmma-e6m9-1688-SM89.json`
  and the corresponding `16816` file.
- These synthetic SASS observations earn no verification credit and do not
  establish PTX compiler emission. Other architecture staging remains inferred;
  they are not hardware-verified by the SM89 captures.
- Full-exponent random inputs and explicit format boundaries add 523 discovery
  observations per shape, also with no errors or reference mismatches. These
  include the minimum subnormal, largest subnormal, minimum normal, maximum
  finite value and zero-times-infinity. Actual inputs are retained in the
  corresponding `hmma-e6m9-*-wide-SM89.json` files.
- Promoted SM89 matrix regression passes 11,844 shared cases covering all 80
  direct dense HMMA selector combinations, plus 30 whole-warp alias fixtures.
  Hardware evidence remains SM89-specific. The other supported architecture
  forms pass independent reference and exact/readable Ghidra checks. Native
  arithmetic and cooperative fixtures were checked on SM89; the cooperative
  scheduler fixtures also pass on SM90.

B2R cooperative discovery on 2026-11-04:

- Initial BAR, WARP and RESULT reads return zero. `WARPSYNC` leaves WARP zero.
  Completed ordinary `BAR.SYNC` sets RESULT to its actual participant count,
  including partial warps (17, 33 and 45 threads). Later ordinary barriers
  overwrite earlier reduction results.
- WARP returns RESULT's low eleven bits. True AND/OR reductions return
  RESULT=0xffffffff and WARP=0x7ff; mixed AND returns zero, and mixed OR returns
  the true encoding. POPC returns the number of true lane predicates.
- Pending BAR state is
  `tag << 28 | expected_warps << 20 | accumulated_threads << 8 | arrived_warps`.
  The expected field is the explicit encoded count in warps, or zero for an
  implicit count. Tags are 8 for SYNC/ARV, 9 for POPC, 10 for AND and 11 for OR. Staggering
  arrivals in both orders distinguishes arrival counts from warp identities.
  `b2r-pending-values-SM89.json` distinguishes the true-thread accumulator
  from an arrival counter: POPC counts 0, 1, 2, 15, 16, 17 and 31 increment
  it in units of 256. SYNC, ARV, POPC and OR count true inputs; AND counts
  false inputs.
  Completion clears the selected BAR state.
- Retained whole-kernel sources and actual outputs are in
  `research/additional-opcodes/b2r-*-SM89.json`. Reproduce with
  `nice uv run python -m research.barrier_state_observe --extended --out /tmp/b2r.json`
  and `--partial-warps` for mixed reductions and partial CTAs. These are discovery
  observations and earn no verification credit. `b2r-exited-SM89.json`
  establishes that ordinary results exclude exited threads, including whole
  exited warps and partially exited warps. Overlapping generations remain
  unestablished.
- `b2r-arrival-results-SM89.json` establishes result delivery. An earlier
  nonblocking ARV retains its prior RESULT even after the BAR state clears;
  the completing warp receives the result. Blocking and deferred barriers
  deliver results to all waiting participants.

Cache-control compiler discovery on 2026-11-04:

- Pinned ptxas 13.4.92 emits CCTL.D.PF1 for global L1 prefetch and CCTL.D.PF2
  for global L2 prefetch. On SM80 and newer, L2 evict-last prefetch emits PML2;
  applypriority L2 evict-normal emits DML2, and discard L2 emits RML2.
  SM75 rejects the latter PTX operations. These mappings establish compiler
  intent, not numerical or cache-state verification.
- Full instruction streams distinguish apparently missing CCTL operations:
  local L1 prefetch emits CCTLL.PF1, and the PTX 9.4 32-byte valid-address
  prefetch emits a destination-sink LDG.U8 on SM90 and newer. The uniform
  prefetch probes emit CCTL.D.PF1; they do not establish cache-selector U.
- Sources, compiler hashes and decoded words are retained in
  `research/additional-opcodes/cache-controls-13.4.json` and
  `cache-standalone-13.4.json`. Reproduce using
  `PTXAS=/tmp/additional-cuda-13.4/ptxas nice uv run python -m research.cache_controls_trace
  --out /tmp/cache.json --sources /tmp/cache-sources`, adding `--standalone`
  to omit the preceding global load.
- PTX 9.4 specifies prefetch and priority operations as cache effects, while
  discard permits unstable indeterminate values in the selected 128-byte
  range. RML2 therefore cannot be treated as an ordinary prefetch or an
  unconditional no-op. RML2 remains unimplemented.

Cache-control value validation on 2026-11-04:

- `cache-controls-SM89.json` retains compiler-generated before/after volatile
  global-load observations for six cache operations, three repetitions each.
  Reproduce with `PTXAS=/tmp/additional-cuda-13.4/ptxas nice uv run python
  -m research.cache_controls_observe --out /tmp/cache-controls.json`.
  These discovery observations earn no verification credit.
- Twelve independent cooperative regression kernels pass on SM89: ordinary
  CCTL.D.PF1/PF2/PML2/DML2 with P0, inverted P0 and an always-true predicate.
  Every kernel executes its target and compares 64 outputs across two CTAs.
  The requests preserve memory values; this does not establish physical
  cache residency, eviction timing or cache performance.
- The implementation records explicit requests completed under the existing
  SC model. Its ordinary 32-bit address behavior is inferred from the address
  encoding; hardware probes use paired E addresses. Other architectures have
  compiler/specification evidence, not these SM89 observations.
- The generated numeric-definition CUDA test passes for all 1,072 discovered
  signatures, plus wide/mixed-argument ABI regressions, across ten architectures
  and both lowering modes. It took 652 seconds with competing cold validation
  work. This checks generated functions against independent numeric expectations;
  it does not grant HMMA-instruction hardware evidence.

Whole-cache invalidation on 2026-11-04:

- `cache-invalidation-13.4.json` pins eighty successful compiler probes across
  ten architectures, varying only the polling load's semantics and scope.
  GPU/system acquire loads emit CCTL.D.IVALL; the corresponding relaxed loads
  do not. Both emit YIELD. Reproduce with the pinned compiler selected through
  `PTXAS`: `nice uv run python -m research.cache_invalidation_trace --out
  /tmp/invalidation.json --sources /tmp/invalidation-sources`.
- A one-block warmed-payload probe does not distinguish the original CCTL
  from a NOP replacement or a false instruction guard. The cross-block probe
  does: consumers cache zero and announce readiness through an atomic counter;
  the producer waits for every consumer, writes 42, then publishes a release
  flag. Consumers acquire the flag before reloading the cached payload.
  All warm-up values are zero, every consumer observes the release flag, and
  no bounded loop exhausts its limit.
- Across three repetitions of sixteen and thirty-two blocks, all 4,416
  consumers read 42 with CCTL.D.IVALL. Every consumer reads stale zero with
  NOP or a false guard instead. Producer outputs remain 42. The retained
  `cache-invalidation-cross-SM89.json` includes source, image hashes, patched
  words, readiness/flag values and polling counts. Reproduce with
  `nice uv run python -m research.cache_invalidation_observe --cross-block
  --out /tmp/invalidation-cross.json`. Discovery captures earn no credit.
- Three independent SASS regression fixtures pass on SM89, each comparing
  all 64 lane outputs after a two-block handoff. The invalidation is
  unconditional, enabled by an inverted producer predicate, or enabled by
  the acquired flag. These fixtures omit YIELD and establish this cache form
  separately from unresolved scheduling behavior.
- The SC runtime records the invalidation and advances an internal generation
  for each enabled request. Logical memory already exposes the latest stores;
  physical cache contents, weak-memory outcomes and timing are not simulated.
  Other cache selectors and whole-cache operations remain unresolved.
