# Instruction semantics

Value semantics for the generated languages: how families are written, how they reach SLEIGH,
the exact and readable floating-point lowerings, per-opcode behavior and evidence, the runtime
ABI, and how it is built and verified.

Contents:
[1. Framework](#1-framework) ·
[2. SLEIGH lowering](#2-sleigh-lowering) ·
[3. Exact and readable floating point](#3-exact-and-readable-floating-point) ·
[4. Integer](#4-integer) ·
[5. FP32](#5-fp32) ·
[6. FP64](#6-fp64) ·
[7. Packed FP16](#7-packed-fp16) ·
[8. Conversions](#8-conversions) ·
[9. Control flow](#9-control-flow) ·
[10. Memory and special registers](#10-memory-and-special-registers) ·
[11. Runtime primitives](#11-runtime-primitives) ·
[12. SM107](#12-sm107-2026-10-14) ·
[13. Runtime ABI](#13-runtime-abi) ·
[14. Build and verification](#14-build-and-verification)

Reading notes:
- Hardware evidence is SM89 (RTX 4070 Laptop GPU) and, since 2026-10-22, SM90 (H100 NVL);
  `docs/hardware-facts.md` records what it established. Everything on other architectures is emulator-verified against independent references
  (`tests/refs/`), PTX/CUDA documentation or ptxas lowering, never GPU-measured.
- "All nine architectures" below predates SM107 (added 2026-10-14; ten architectures, 20 languages
  since). `processor/SASS/semantics/coverage.json` is authoritative for what is native where.
- Phase labels (Phase 5, 6a-6c) and dates mark when a piece landed and anchor its evidence.

## 1. Framework

### Families (`src/sass/semantics/families/`, 2026-10-12)
- A family is a Python function over the md-decoded instruction (`@family('IMAD', 'UIMAD')`,
  `src/sass/semantics/family.py`) returning `outputs(Rd=..., Pu=...)` of `sass.semantics.bitvec`
  bitvector expressions (widths in bits, `extract`/`concat`). One definition serves
  `sass.semantics.lifter.lift` (Ghidra-free semantics, evaluated by `sass.semantics.evaluate`) and the
  SLEIGH backend `src/sass/sleigh/lower.py`.
- Every opcode with semantics is a family (Phase 6c, 2026-10-13). `src/sass/semantics/operations.py`
  only routes a class to its family (`emit`) and `dispatch`es the native body behind decode-time
  selector patterns, with the opaque call for the rest (see
  [routing](#routing-fallbacks-and-status)).
- Modules: integer (`integer.py`), FP32/FP64/packed-half/packed-pair floating (`floating.py`),
  conversions FRND/I2F/F2F/F2I (`convert.py`), F2IP (`f2i.py`), F2FP except stochastic rounding
  (`f2fp_rs*`, opaque) (`f2fp.py`), control flow (`control.py`), memory and special registers
  (`memory.py`), runtime-executed operations (`runtime.py`); UP2UR is in `integer.py`, HFMA2.MMA in `floating.py`.
- Static answers are Python values that SLEIGH enumerates; operand values are expressions. The
  static API:
  - `i.select_fork(name)`: a selector's label, one family body per label.
  - `i.select_lookup(name, {label: expr})`: the value a selector chooses, as one decode-time choice
    instead of a fork per label (FSETP's comparison, shared by Pu and Pv). `i.select_lookup_const`
    is its constant case (label -> number tables). `i.select_in(name, labels)` forks on label
    membership. `i.select_bits` builds decode-time bit-selection tables (PRMT).
  - Modifier bits `i.operand_negated/absolute/inverted/notted`, `i.is_zero`, `i.has_operand`,
    `i.static` (immediates of at most 8 bits), `i.static_low(name, bits)` (only the low bits of a wide
    immediate: UP2UR's 7-bit predicate mask; SLEIGH constrains a sub-field).
  - `a if c else b`, `and`/`or`/`not` are rewritten so they work on both static and dynamic values.
- A family that never asks about a selector, modifier bit or defaulted numeric operand models only its
  md default: any other value is unmodelled (opaque). Selectors without an md default
  (`/ONLY64:size`, `/XORONLY:xor`) must be asked. `i.ignore(name)` marks an operand irrelevant to an
  encoding (ISETP's Pr without .EX). Operands with bracketed parts (C, CX, A, DESC) are values, not
  selectors, even where the type names an md enum.
- Runtime primitives (2026-10-22, replacing Phase 6c's `primitive()`, which passed every decoded field
  to `sass_prim_OPCODE` and let the runtime re-decode them): `src/sass/semantics/primitive.py` builds
  `bitvec.primitive(name, w, *args)` nodes, values a family uses like any expression
  (`outputs(Rd=primitive.mufu("SIN", x))`), and synchronization effects. The name is only the operation
  (the mnemonic, plus MUFU's function); selectors a primitive still needs are constant arguments (their
  encodings); everything computable from values is native (SHFL's lane arithmetic, VOTE's modes,
  MATCH.ALL, ELECT's leader, QSPC's address). See [section 11](#11-runtime-primitives). Fixed registers
  (`bitvec.reg('UP', k, 1, element=1)`) lower to their names.
- Floating point in the IR: `fadd`/`fmul`/`ffma` (rounding mode, FMUL's 2^k scale),
  `flt`/`fle`/`feq`/`fnan`, `fconv`, exactly rounded (`sass.semantics.fp.softfloat`) in
  `lift`/`evaluate`. FTZ (branch-free), SAT and min/max NaN/zero rules are family IR
  (`floating.py`). The SLEIGH lowerings are in [section 3](#3-exact-and-readable-floating-point).
- `concat` (2026-10-16) stores all operands directly, most significant first, with width equal to their
  summed widths. `concat(x)` returns `x`; zero operands raise `TypeError`. Explicitly nested concats
  remain nested so lowering can preserve shared intermediates and avoid extra widening of sub-byte
  parts. Extraction simplifies slices contained in any one operand; slices crossing operand
  boundaries remain extracts.

### Operand values (2026-10-07)
- `src/sass/semantics/operands.py` (`Builder`) preserves exact register effects for all classes and
  builds operand values independently of assembly display.
- Encoded immediates retain their size, sign and scale; multi-field operands and inverse tables use the
  same md rules as the decoder. Floating immediates carry bits, including F64 high-word immediates.
- Modifiers are independent inputs; families apply them in the consuming type's context.
- Register operands use exact md spans and true/zero-register behavior.
- Constant operands load from `cbank` at `(bank << 32) | uint32(offset)`; LDC forms calculate an
  address before loading. Constant-bank widths: see [SLEIGH lowering](#2-sleigh-lowering).
- CX (`cx[URx][offset]`) is a bounded constant-buffer load on SM75-SM90 (the shared provider described
  under [BMSK](#bmsk-and-the-cx-bounded-load)). Other CX forms, descriptors, attribute memory and
  unencoded stateful operands use `sass_operand_value`: a context provider must supply their values;
  the implementation does not invent their address layouts.
- The md's `PR` operand (P2R/R2P) is a 7-bit value, P0 the low bit. `Predicate_vimnmx` (VIMNMX.pred,
  P0-P6, no PT) is a P-file register type; its Pu/Pv semantics are unknown (opaque).
- Special registers are a register file at offset 0x3000 named from the md `SpecialRegister` enum
  (`SR_CTAID.X`; `_64` views for CS2R.64). Emulation seeds them by name like any register; unseeded
  ones read 0.
- Spaces: `ram`, `cbank` (bank << 32 | uint32(offset)), `shared`, `localmem`. The identifier `local` is
  reserved in SLEIGH.
- Latency-only implicit resources are annotations, not guessed fixed registers.

### Routing, fallbacks and status
- Encodings a family doesn't model go to the opaque call. The runtime-primitive fallback
  (`fallback='prim'`) is gone with the implicit ABI (2026-10-22): the F2I/I2F forms that used it, F2I's
  8/16-bit destinations and the CX 64-bit sources, had no case or evidence and are opaque.
- Unknown conventions remain opaque: SM107/SM120 extra IMNMX predicates, extended memory and the
  descriptors of opcodes without a family, branch-stack CALL/RET depth changes.
- Every opaque fallback of a class with an md `BRANCH_TYPE` keeps its control flow
  (`Builder.flow`): direct targets (RSImm relative, UImm absolute) and returns use
  `taken = call(...); if (taken == 0) goto <untaken>; goto/call/return`, indirect targets use
  `dest = call(...); goto/call [dest]`. This matters because SM90+ spell BRA defaults
  `nocond__div_conv`/`nodepth_bra`: before it (Tier B), only native BRA/CALL/RET/EXIT branched and every
  SM90+ BRA was opaque without a goto. Guards on default selectors use the md default
  (`sass.sleigh.gen.default_value`). `test_pcode_dataflow` also checks Ghidra's flow type against
  `BRANCH_TYPE` per class. Opaque control-flow variants retain md branch properties in the manifest;
  they need a handler to execute their control effects.
- Status meanings (`coverage.json`, per class):
  - `native`: an override has a supported class shape. Selector guards can still choose an opaque
    fallback, so it does not mean every encoding in the class has value semantics.
  - `primitive`: native semantics that use runtime primitives (a row when its outputs use one, a
    class when all its modelled rows do); the runtime computes them from their arguments or rejects
    a missing context or an unmodelled approximation.
  - `opaque`: the md's register effects, registers and immediates are passed without asserting a
    result. Rejected class selectors are reported as opaque.
- Coverage files: `--coverage` writes a detailed manifest `sass_smXX_coverage.json` beside each `.sla`
  (operand inventory, input order, output widths for every span variant, native selector guards).
  They are generated, not committed; the runtime needs none (primitives carry their arguments).
  [`coverage.json`](../processor/SASS/semantics/coverage.json) is the committed compact ledger for all
  architectures. Verification evidence is per class, derived from the tests into
  [`evidence.json`](../processor/SASS/semantics/evidence.json): SM89/SM90 GPU fixture targets and
  shared cases, `tests/ptx` probes and reference emulator cases (`tests/evidence.py`), each with the
  forms it ran. A class without modifiers (MOV R,R, IABS, S2R) records its one, empty form
  (`"modifiers": []`, `[[]]`) since 2026-10-23; before, it recorded only class-level evidence, which
  read as "no form ran". `sass.provenance.evidence(arch, word)` looks a word's class and form up:
  `{kind: architectures}`, reference and ptxas from the word's own architecture, gpu from every
  architecture whose GPU ran the same class and form (an SM80 word can be backed by SM89 hardware).
- `uv run sass-census SMxx...` ranks classes in the real corpus by static occurrence and
  native/primitive/opaque status (each word's status is its family's decision for that encoding, via
  `lifter`; ~2 min per arch on 32 cores).

## 2. SLEIGH lowering
- Merging: the rows of static answers are merged by anti-unification (one key at a time, default rows,
  rows the default reproduces absorbed, choices reused across outputs). Outputs merge jointly, so a
  decode-time choice can select whole bodies (one constructor computes a shared constant load once).
  `cost` (`lower.py`) = generated nodes + 8 x operations on the longest execution path, per class.
  A subtable exporting a register exports the register itself: later outputs are captured before the
  first write.
- In SLEIGH, operand modifier bits (|x|, -x, ~x, !P) are decode-time choices of the operand value,
  except on loaded operands (c[], cx[]), whose load each table would repeat. With `select_lookup` this
  keeps the enumeration small (FFMA has 6 modifier bits).
- md CONDITIONS are enforced for family classes (except conditions on scheduling hints alone);
  `sass-compare-ghidra` expects those words rejected. CONDITIONS that read a span field (sz, e:
  register range/alignment rules such as "sz=64 -> Rd even") are checked per span variant
  (`Gen.conditions(k, domain)`, `Gen.span_variants(domains=True)`), with the span fields narrowed to the
  variant's values, not in the root pattern: at the root they multiplied the global decision tree
  (SM89 10 -> 76 MB with the memory families).
- Native selector checks are decode-time constraints, with separate opaque constructors. Encoded
  scalars use anchored constructor actions (`inst_start * 0 + expression`): unanchored computed
  exports crash Ghidra 12.1's native compiler. Register-view aliases avoid redundant snapshots;
  multi-output calculations capture inputs before writes.
- Constant folding: expressions over constants and plain immediate fields are computed while decoding
  (`sass.sleigh.lower.Fold`: a constructor action exports the constant; comparisons and `ite` are
  carries of 64-bit masked sums), so the HFMA2.MMA constant move is one COPY. A `c[][]` operand's
  address is a decode-time constant (`Gen.immediate_text`, shared with `Fold`), so its load is one
  LOAD. Float immediates with a `convertFloatType` rule fold too when every encoding of the format
  selector picks the same layout (`Gen.float_formats`; F64 supplies the high word, F16/E8M7/E6M9 are
  the packed bits). SM90 declares half immediates as `F64Imm` converted to F16 or E8M7 by `ofmt`
  (SM89 declares `F16Imm`); until 2026-10-22 they were not folded, so the SM90 HFMA2.MMA constant
  move took 22 run-time operations and the immediate forms of HMNMX2/HMUL2/HSET2/HSETP2 and F2F
  upconversions recomputed their immediate's NaN tests at run time.
- Known zero registers: a register the constructor can only encode as RZ/URZ/PT is that constant in
  the family IR, so it folds like lift's: pseudo-op variants pass the operand values their pattern
  fixes (`Builder.known`, from `Gen.pseudo_variants`), and IMAD.MOV `Rd, RZ, RZ, Rc` is `Rd = Rc`.
  Where a register may or may not be RZ, no zero choice is made: it would shorten only the RZ path,
  and the cost counts the longest. Encoded RZ/URZ inputs become constants before constructing
  expressions, avoiding run-time zero extension, concatenation with zero and shifts by zero in raw
  P-code.
- Decode-time choices (PRMT and similar): a single-use choice exports its zero-extended result when
  needed, so byte slices do not need a COPY solely to cross a subtable boundary. A choice with a
  constant arm exports the constant itself (no COPY), and a concatenation chain is one full-width OR
  of shifted parts, constant parts merged and zero parts dropped (an inner chain of sub-byte parts
  stays nested when widening them would cost more zexts: I2IP.24). Separate bit-slice choices hide
  their constants from the expression combining them, so `constant_slices` gives each row one more
  choice: when every bit-slice choice of an immediate picks a constant arm and the whole output then
  folds to a constant, that constant (`PRMT R3, R2, 0x7654, RZ` is one COPY of 0; 2026-10-16). This
  adds one constructor per affected row, not a product over slices.
- Unused operand values and unreachable subtables are omitted; shared value tables are evaluated only
  when used. `sass-build-languages` rejects unused-temporary diagnostics.
- Compact p-code: immediate/register MOV emit one operation, ordinary IMAD/IADD3 two, LOP3 XOR with a
  discarded predicate output three (regression fixtures assert these). LOP3 truth tables simplify to
  the boolean expression for the selected table; discarded predicate results generate no calculation.
  Dataflow checks still require exact reads/writes, adjusted for unused boolean/constant-comparison
  inputs and byte-width store sources; opaque fallbacks retain full md reads.
- Constant-bank widths come from md ISRC_<slot>_SIZE. For a `cx[][]` operand, whose md size is its
  64-bit header's, the width is the same operand's width in the class group's `c[][]` form (IMAD.HI/.WIDE's
  64-bit addend, SM89 GPU 2026-10-17). Known md errors are corrected at load (`src/sass/md/fixes.py`,
  applied by `parse.load_classes`; `src/sass/md/data/` keeps the md as shipped):
  - SM75/SM80 F2F.F32/F16.F64's `c[][]` source is 64 bits, not the md's 32 (ptxas converts an `.f64`
    parameter in place, `tests/ptx/f2f/`, 2026-10-17).
  - SM75/SM80 IMAD.HI/.WIDE `c[][]` addends are 64 bits, not the md's 32 (their `cx[][]` forms and
    SM86+ say 64; ptxas adds 64-bit pointer parameters with `IMAD.WIDE.U32 R2, R2, R3, c[0x0][0x160]`,
    2026-10-14).

## 3. Exact and readable floating point
- Two languages (2026-10-08) per architecture: `SASS:LE:64:smXX` (exact) models hardware results;
  `SASS:LE:64:smXX_readable` writes the plain formula for decompiler reading. They decode, display and
  have the same register dataflow; only floating-point value semantics differ. The ELF opinion lists
  only the exact languages; choose the readable one explicitly.
- Selecting a mode: `Gen(arch, fp)`, `sass.sleigh.gen.variant(arch, fp)`, `sass.sleigh.gen.MODES`;
  `--fp=exact|readable` on `sass-gen-sleigh`/`sass-build-languages` (both built by default). Every
  Ghidra test takes the lowering (`pytest -m ghidra --fp readable|all`,
  `uv run python -m tests.semantics ARCH readable`, `sass-compare-ghidra --fp readable`).
- `src/sass/sleigh/lower.py` (`Lower.float_op`) holds both lowerings.
- Readable: native-width `f+`/`f*` (FFMA is `a f* b f+ c`, one operation per operator plus a copy). It
  treats every rounding mode as round-to-nearest and drops FTZ/FMZ flushing, fused single rounding and
  the FMNMX NaN/±0 rules (including .NAN) and the arithmetic NaN result (Ghidra's NaN bits); families write readable FFMA/scaled FMUL as their unfused
  operations. SAT, absolute and negate remain. So encodings unmodelled in the exact lowering (HADD2/HFMA2
  .FTZ/.FMZ and the like) are native here. Its reference is the unfused RN softfloat formula, compared
  bit for bit.
- Exact, round-to-nearest: at native width, except that FFMA and FMUL scaling go through the doubled
  format. RN FFMA computes `a*b` exactly in fp64 and narrows the fp64 sum (`float2float(fp64 a*b + c)`):
  deliberate double rounding, rare 1-ulp differences from hardware at midpoints.
- Exact, directed rounding (.RM/.RP/.RZ): fp32 via fp64, fp64 via binary128. s = RN_wide, plus the exact
  residual (a TwoSum error for sums, zero for products, which are exact in the wide format), RN
  narrowing, then ±1 on the encoding by the residual's sign. RM zero-sign fix: an exact cancellation is
  -0 unless both addends are +0. The corrected result is exact, and it also undoes the Ghidra underflow
  error below. SM89 GPU fixtures cover .RZ/.RM/.RP for FADD/FMUL/FFMA/DADD/DMUL/DFMA.
- NaN results (2026-10-22, issue #49): the IR ops `fadd`/`fmul`/`ffma` are the GPU's arithmetic, so a
  NaN result is `bitvec.sass_nan` (hardware-facts "Arithmetic NaN results"): +0x7fffffff (F32), +0x7fff
  (F16), and for F64 the first NaN of operand 1 onward then operand 0 (Rb, Rc, Ra), quieted, else
  0xfff8000000000000. F64 propagates the register as read, so the FP64 families pass the unmodified
  sources as the ops' optional `nans`. The Python evaluator applies it after softfloat (which stays
  IEEE with generic NaNs); the exact lowering ends `float_op` with a branch-free select (`sass_nan`).
- Ghidra 12.1.4 limits:
  - Its emulator flushes results strictly between half and one minimum subnormal (2^-150, 2^-149 for
    fp32) to zero instead of rounding to the minimum subnormal (`FloatFormat.getEncoding`, the `n < 0`
    branch). The p-code itself is IEEE, so the RN tests exclude that band, for FP32, FP64 and FP16.
  - Its binary128 `f<` orders NaN operands, so the directed-rounding code guards with `nan()`.

## 4. Integer

### Native set and uniform aliases
- Native overrides cover ordinary MOV, IABS, SEL, IMAD low/high/wide, IADD3, LOP3, SHF, LEA
  low/high/extended, IMNMX, ISETP including `.EX`, plus the opcodes below. Mixed ordinary/uniform
  register forms (`URb`/`URc`) of IMAD, IADD3, ISETP and LEA use the same calculations, and UIADD3,
  UIMAD, USHF, ULEA, UISETP, ULOP3, USEL and UMOV share their families.
- These mixed-register forms, uniform aliases, PRMT, FSEL and ordinary FMNMX ("step 2") pass
  value/dataflow tests on all nine architectures.
- Ordinary 32-bit IADD and VIADD are native; packed/saturating VIADD and its carry modes stay opaque.
- R2UR is native for Pu = PT only.
- Wide views containing the hardwired zero slot remain opaque (writing ordinary backing storage for a
  zero-register component would be wrong), except views starting at RZ/URZ itself, which keep all-zero
  semantics. Uniform wide views follow the same rule.

### IMAD, IADD3, LEA and carries
- IMAD.HI forms the full 64-bit product plus a 64-bit addend, including the low-word carry, and keeps the
  high word; IMAD.WIDE writes the whole sum. CUDA's `mad.hi` lowering places its scalar addend in the
  upper register of a pair, and the whole-kernel oracle executes that compiler lowering directly. An
  SM89 probe verifies the 64-bit constant source path; SM75/SM80 constant forms also read 64-bit
  addends (the md's 32 is an md error, see [constant-bank widths](#2-sleigh-lowering)).
- A narrow (32-bit, non-pair) HI addend is added, with the carry-in predicate, to the product's high
  word before the 32-bit result and carry-out are computed. Exact/readable fixtures cover both carry-in
  values at the wrap edge.
- IMAD.X adds one predicate; HI/WIDE carry indicates unsigned overflow of the full 64-bit product and
  pair addend, including signed product bit patterns. A negated addend enters as `~C + 1`, so `-0`
  carries (SM89 GPU); for a narrow addend the same rule applies at 2^32 without hardware evidence.
- IADD3.X adds both predicate inputs to the complemented 32-bit operands. Ordinary negation retains its
  33-bit +1, including the carry from negating zero. IADD3 predicate outputs indicate a sum of at least
  2^32 and 2^33; the first output wins if both destinations name the same predicate.
- LEA computes the shifted low/high word before applying negate/complement and adds its predicate for
  .X.
- These operations capture input predicates before writing overlapping outputs. IADD3/LEA encodings
  modifying both Ra and Rb retain opaque fallbacks. Discarded carry outputs keep the arithmetic compact.
- Carry GPU fixtures: [section 14](#carry-fixtures).

### ISETP, IMNMX and packed min/max
- ISETP `.EX` compares the high words and uses `Pr` for the low-word result when those words are equal,
  then applies the selected AND/OR/XOR combiner with `Pp` to produce `Pu`/`Pv`. Direct register,
  uniform, immediate and ordinary constant-bank forms are native; CX and SM120 ISETP64 forms stay
  opaque. SM75's shared `EX`/`noex` selector is handled separately at decode time so ordinary ISETP does
  not read `Pr` or compare high words. Shared GPU probes (2026-10-16) corrected its constant F/T
  comparisons (`docs/hardware-facts.md`).
- IMNMX: SM107/SM120's extra predicate operands/results (Pu/Pv/Pq) remain opaque.
- VIMNMX implements PT=min and !PT=max for S/U32, S/U16x2 and SM120's S/U8x4 formats with register,
  uniform-register, immediate and SM75-SM90 CX B sources. Signed RELU forms clamp each selected lane at
  zero; unsigned RELU encodings are invalid according to the md. Non-PT predicate outputs remain opaque.
  CuLifter's per-lane behavior has independent exact/readable emulator fixtures on SM100 and SM103, and
  VIMNMX also on SM120; no GPU verification (these instructions are newer than SM89).
- VIADDMNMX adds B modulo each selected lane width, then applies the same predicate-selected min/max
  against C. S/U32 and S/U16x2 with R, uniform-register and immediate B/C forms on SM100 and SM103. The
  encoded B negation is applied before lane extraction, matching CuLifter's operand materialization.
  Signed RELU clamps each result lane at zero; unsigned RELU encodings are md-invalid. Independent
  exact/readable emulator fixtures cover lane overflow, signed comparisons, predicate polarity, B
  negation, RELU and all source shapes. No GPU verification.
- VIMNMX3 uses Pp to select three-way min for PT and max otherwise, per lane, for
  S32/U32/S16x2/U16x2. R, URb and immediate B forms on SM100 and SM103. Signed RELU clamps each result
  lane at zero; unsigned RELU encodings are md-invalid. Independent lane-wise fixtures pass in exact and
  readable mode; no hardware result.
- VISET: see [SM107](#12-sm107-2026-10-14).

### PRMT
- Native byte selection for all seven modes: IDX (including sign replication), F4E, B4E, RC8, RC16, ECL
  and ECR, in SASS source order: data A, selector, data B (SM89 GPU).
- Immediate selectors use independent decode-time byte-selection tables (`i.select_bits`), including
  sign replication; register selectors keep dynamic selection. Lowering details in
  [section 2](#2-sleigh-lowering).
- UPRMT is PRMT's IDX mode (`/IDXOnly("IDX"):idx`).

### PLOP3
- PLOP3/UPLOP3 (`integer.py`): Pu and Pv both look their bit up in their truth table
  (`imm8 >> (4p+2q+r)`): enumerating Pu's 256 tables into boolean expressions cost ~30 KB of SLEIGH per
  class (SM90 was near the `.sla` cap) and both jointly 65536 rows. If Pu and Pv alias, Pu wins (SM89).
  Register-source classes stay opaque.

### BMSK and the CX bounded load
- BMSK emits a 32-bit mask from position and width. Instruction bit 75 selects clamp (0, `BMSK`) or wrap
  (1, `BMSK.W`). Wrap masks both counts with 31; the mask still ends at bit 31. Clamp returns zero for a
  position of at least 32 and saturates large widths at the end of the word. Zero width returns zero in
  both modes. Register, uniform-register, immediate and ordinary constant-bank inputs are supported.
- CX (`cx[URx][offset]`) has native P-code on SM75-SM90. Its 64-bit uniform-register pair holds a
  bounded constant-buffer header, and the offset is in bytes. SM75-SM89 decode the low 45 bits as
  base/16 and the high 19 bits as size/16; SM90 decodes the low 51 bits as base/64 and the high 13 bits
  as size/16. The load uses `ram` at the decoded GPU virtual address and returns zero outside the
  region. The layouts come from Mesa's `nvk_bindless_cbuf` definitions; SM89 hardware validates
  addressing and bounds. SM90's layout and zero-on-out-of-bounds behavior are emulator-tested, with
  hardware validation pending.
- The shared provider materializes CX values for supported scalar and packed emitters on SM75-SM90.
  Direct GPU evidence is BMSK-only (plus the per-opcode CX probes noted under F2IP, I2IP and P2R/R2P);
  other consumers need opcode-specific probes before their hardware behavior is established.
- Evidence: dedicated SM89 whole-kernel GPU/reference/p-code comparisons cover 1,504 inputs per mode;
  emulator tests cover both languages on all architectures. The BMSK CX GPU fixtures
  (`tests/cases/bmsk.py`) supply a separate manually patched SASS oracle for CX counts, region sizes and
  offsets: 8,192 GPU/reference/P-code comparisons pass in each language variant, and 446 more focused CX
  cases pass in each of the ten SM75-SM90 languages. The oracle does not claim PTX-generated CX.

### BREV, POPC, FLO, SGXT
- BREV and POPC are native 32-bit operations for register, uniform-register, immediate and ordinary
  constant-bank sources where present.
- FLO supports 32-bit sources with `.S32` and `.SH`/default mode: the default result is the highest
  set-bit index, `.SH` returns the leading-zero shift, and zero returns `0xffffffff` in both modes. Its
  predicate destination is supported only when encoded as `PT`; other FLO selectors stay opaque.
- SGXT supports `.C`/`.W` with `.S32`/`.U32`: keep the low n bits of A and extend them from bit n-1
  (sign or zero), with n = `min(B, 32)` for `.C` and `B & 31` for `.W`; n = 0 gives zero (SM89 GPU,
  2026-10-16, which corrected an earlier byte-select model; `docs/hardware-facts.md`).
- CX operands are materialized on SM75-SM90 where those classes exist.
- Tests compare BREV against a per-bit reversal reference, POPC against `bit_count`, FLO against the bit
  length/leading-zero definitions, and SGXT against an independent bit-by-bit extension.

### IDP
- Native guarded semantics for `IDP.4A` and `IDP.2A.LO/.HI` with R, URb, ordinary constant-bank and
  SM75-SM90 CX B forms. `.4A` multiplies four independently signed or unsigned byte pairs; `.2A`
  multiplies two signed or unsigned 16-bit A lanes by consecutive signed or unsigned bytes from B's low
  or high half. Both accumulate into C modulo 2^32, including Rc negation.
- Independent reference fixtures cover all selector and signedness combinations, overflow, Rc negation
  and source/destination overlap on all nine architectures in both languages. Eight SM89 PTX `dp2a`
  kernels lower to the expected SASS selectors and pass 128 whole-kernel GPU/P-code/reference
  comparisons apiece.

### VABSDIFF4
- S8/U8 register, uniform, immediate, ordinary constant-bank and SM75-SM90 CX operands for `SIMD_MRG`
  and `ACC`; non-PT Pu destinations remain opaque. `SIMD_MRG` packs four absolute byte differences and
  ignores C. `ACC` adds the four differences as a scalar, then adds C modulo 2^32. CuLifter's CPU
  handler matches these rules.
- A whole-kernel SM89 oracle patched one LOP3 per fixture to the target VABSDIFF4 encoding; 128 GPU,
  emulator and independent-reference comparisons passed for each fmt/reduction pair. This is patched
  SASS evidence, not a claim that ptxas emits those words for a PTX vabsdiff4 instruction.

### I2IP
- S32 inputs packed to signed or unsigned 2-, 4-, 8- and 16-bit lanes. SAT clamps each input to its
  destination range; SATRELU clamps signed inputs to zero through the signed maximum. The no-SAT form
  keeps the low destination bits.
- For 2- and 4-bit packing, the supported `.28`/`.24` extraction selectors place the two converted inputs
  in the high and low lane; the remaining high bits come from C (unshifted, SM89 GPU). The 8-bit and
  16-bit forms accept H0/H1: H1 selects C[31:16] for the preserved upper half of an 8-bit result; C is
  unused for 16-bit results.
- The SM89 GPU oracle compares all eight signed/unsigned widths, plus patched no-SAT and SATRELU selector
  cases, against an independent reference and Ghidra p-code. The SM89 CX probe covers 40 selector/source
  combinations and passes 5,120 GPU/reference and exact/readable P-code comparisons per mode. CX inputs
  are materialized on SM75-SM90; unsupported selector combinations and source shapes stay opaque.

### P2R, R2P and UP2UR
- P2R and R2P support `.B0`-`.B3` with immediate, R, UR and `c[][]` masks on all nine architectures, and
  CX masks (`cx[URb][offset]`, the bounded load shared with BMSK) on SM75-SM90.
- P2R replaces the bits of the selected byte of its base that the mask's low byte selects with
  `P0`-`P6`; mask bit 7 clears bit 7 (there is no P7), higher mask bits are ignored, and the other base
  bits are kept (SM89 GPU, 2026-10-16; `docs/hardware-facts.md`).
- R2P reads the selected source byte and updates only the selected predicates, preserving the others;
  its mask bits above 6 are ignored.
- Evidence: exact and readable emulator cases cover every selector and mask source, empty, sparse, full
  and bit-7 masks, a CX mask past the descriptor's size (it reads 0), all-ones and mixed bases, and
  Rd == Ra. Focused dataflow checks pass for all 18 exact/readable languages, with 24/24 native cases
  per language. The patched SM89 whole-kernel GPU probe passed 2,304 GPU/reference/exact-p-code
  comparisons across 18 selector/mask cases. 44 more P2R fixtures with nonzero bases establish the merge
  rule, and 53 cover the R, UR, `c[0x0][0x0]` (blockDim.x) and CX mask forms of both opcodes.
- UP2UR's immediate-mask `.B0` form is native on all nine architectures. It merges like P2R: each bit
  `i` of the mask's low byte replaces bit `i` of `URa` with `UPi`, bit 7 is cleared (there is no UP7),
  and higher mask bits are ignored (SM89 GPU, 2026-10-16; CuLifter's handler ORs instead). The p-code
  specializes the immediate mask so it reads only the selected uniform predicates. Register-mask UP2UR
  and non-B0 selectors stay opaque.
- UP2UR evidence: exact/readable emulator fixtures cover empty, sparse, full and high-bit masks and a
  false guard. A GPU fixture (`tests/cases/predicates.py`) patches UP2UR into a compiled SM89 kernel
  wrapper; nine masks across 128 threads passed 1,152 GPU/reference/exact-p-code comparisons with base
  0x100, and 12 more with all-ones and mixed bases (including URd == URa) established the merge. The PTX
  lowering probe did not emit UP2UR, so this result comes from patched SASS.

## 5. FP32

### FADD, FMUL, FFMA, FSETP
- Native exact p-code (`f+`, `f*`, `f<`, `nan`, ...) for every rounding mode, .FTZ (denormal
  inputs/results flushed to signed zero), .SAT and FMUL scale (computed in fp64, exact). Unknown
  selectors are opaque.
- .FMZ (FMUL/FFMA/UFMUL/UFFMA, native since 2026-10-20 after SM89 probes): both factors become +0 when
  one has a zero exponent (`product_inputs`), so the product is +0 even against Inf or NaN and FFMA
  adds Rc to it (`docs/hardware-facts.md`). FTZ and FMZ share one body through `select_in` (a fork on
  label membership, then a decode-time FTZ/FMZ choice); a plain `select_fork` made SM90 exact
  16,848,482 bytes, over the limit. That is a size-for-speed override, not a merge fix: the merger sees
  the sharing, but a choice subtable can't reuse values, so the shared body recomputes the zero test
  per factor (on the unflushed factors, so not their flushes), and `cost` rightly prefers separate FMA
  tables. FTZ/FMZ forms pay those operations (FTZ `ffma__RRR_RRR` 48 -> 65); plain forms keep their
  body.
- Source `|x|`/`-x` (2026-10-20): generated absolute/negated-source forms are enabled for FADD/UFADD,
  FMUL/UFMUL, FFMA/UFFMA, FSET, FSETP/UFSETP and FMNMX. The case adapter applies the independent
  reference's absolute value before negation, then evaluates the arithmetic/comparison/selection rule.
  Profiles add negative infinity and NaN, signed subnormals and saturation boundaries. These are
  reference-backed cases for the existing native semantics; the complete fp32 family passed on SM89.
- Hardware fixtures: FADD.FTZ and FFMA.SAT have dedicated fixtures, plus the directed-rounding set
  ([section 14](#whole-kernel-gpu-fixtures)).

### FSEL, FMNMX, FMNMX3
- FSEL selects the modified floating input, with optional .FTZ.
- FMNMX: predicate true selects min, false max. Ordinary min/max, including .FTZ: a numeric input wins
  over NaN, two NaNs yield `0x7fffffff`, and equal signed zeros yield -0 for min and +0 for max.
  - `.NAN`: either NaN input produces canonical `0x7fffffff`; numeric inputs still use the ordinary
    min/max and signed-zero rules.
  - `.XORSIGN`: takes the XOR of the raw source sign bits before the absolute/negate source modifiers,
    selects min/max using the modified signed operands, and applies the combined sign to a non-NaN
    result; NaN results bypass the sign update (shared GPU probes 2026-10-16 corrected the comparison;
    the 2026-10-20 probe with `|x|`/`-x` confirmed the existing semantics).
  - Predicate-result `.IS_A` forms and min/max predicate outputs stay opaque (unresolved).
  - The SM89 GPU checks compare FSEL and FMNMX bits exactly, including `.FTZ`, `.NAN`, `.FTZ.NAN` and
    `.XORSIGN`; FSEL.FTZ and other architectures have emulator coverage.
- FMNMX3 is native on SM100/SM103 for P0-P6/PT with optional inversion, all `FTZ`/`noftz` and
  `NAN`/`nonan` selectors, and RRR, RUR and immediate-B forms. PT selects three-way minimum; !PT
  maximum. Exact mode follows the PTX three-input min/max rules: default min/max selects the numeric
  input when paired with one NaN, `.NAN` returns canonical `0x7fffffff` if any input is NaN, and equal
  signed zeros select -0 for min and +0 for max. `.FTZ` flushes subnormal inputs to signed zero.
  Readable mode keeps CuLifter's ordered comparison formula. Emulator fixtures cover both predicate
  directions and inversion, all selector combinations, NaNs in each input position, signed zeros,
  infinities and subnormals. Emulator-verified against the PTX specification, not hardware-measured
  (no SM100+ GPU).

### FSET, HSET2, HSETP2 (2026-10-14, from CuLifter's handler list)
- They reuse FSETP's comparison (`float_test`) and its `cond bop Pp` combiner.
- FSET writes 1.0f or +0 (`.BF` is its only md form).
- HSET2 compares each lane and writes 1.0 in the lane format (`.BF`: 0x3c00, SM90+ BF16_V2 0x3f80) or
  0xffff (`.BM`), else 0.
- HSETP2 writes lane 0's result to Pu and lane 1's to Pv; with `.H_AND` it ANDs the lanes and writes
  `both bop Pp` and `!both bop Pp`, like FSETP's Pu/Pv.
- Lanes are widened exactly to binary32 before comparing, after F16 FTZ flushing; the md forbids FTZ
  with BF16.
- Every non-ALTERNATE class is native on SM75-SM120 (register, uniform, immediate, constant and
  SM75-SM90 CX sources). The `fset_simple`, `*_noBop` and SM100+ `*_fixed` classes are ALTERNATE.

### FCHK
- Native (`floating.py`): Pu = the division needs the slow path, from the biased exponents alone (SM89
  GPU).

### SM120 uniform floats (2026-10-14)
- UFADD/UFMUL/UFFMA/UFSETP/UFSEL/UFRND/UF2I/UI2F/UI2FP register the scalar families
  FADD/FMUL/FFMA/FSETP/FSEL/FRND/F2I/I2F (operand names resolve to URa, UPp, ...). The uniform twins'
  unmodelled encodings (F16/BF16 UFRND, UF2I's 8/16-bit destinations) are opaque, like the scalar ones'.

## 6. FP64
- DADD/DMUL/DFMA are native in every rounding mode with 64-bit register pairs, uniform inputs, high-word
  immediates, constant-bank loads and source absolute/negate modifiers. DFMA uses a 16-byte binary128
  intermediate: the product is exact, but under round-to-nearest the wide sum followed by narrowing can
  double-round in rare midpoint cases. Directed rounding uses the exact residual correction
  ([section 3](#3-exact-and-readable-floating-point)). The Ghidra underflow-band exclusion applies.
  Dynamic constant operands still need an explicit operand provider.
- DSETP shares the ordered/unordered FP32 comparison logic and combines complementary results with
  AND/OR/XOR, for every comparison selector. MIN and MAX are not the constant masks their nibble values
  suggest: they order Ra against Rc by totalOrder (MIN: Ra <= Rc, MAX: Ra > Rc), true for a NaN Rc and
  false for a NaN Ra, and Pv is the uncombined both-NaN flag (SM89 GPU, 2026-10-19,
  `docs/hardware-facts.md`).
- DSETP coverage (2026-10-19): generated reference cases include absolute/negate combinations on both
  sources across register, uniform, high-word immediate, constant-bank and bindless constant forms.
  Focused cases distinguish adjacent FP64 mantissas and the subnormal/normal boundary, and cover
  input/output predicate aliasing, inverted predicate inputs, discarded PT outputs and false
  instruction guards. `docs/hardware-facts.md` reports the shared cases (all modifier forms, 5,366)
  passing on SM89.
- SM89 hardware fixtures cover all four opcodes.

## 7. Packed FP16

### HADD2, HFMA2, HMUL2 (2026-10-13 families)
- HADD2/HFMA2 F16_V2 under RN: H1_H0/H0_H0/H1_H1 selection, per-lane absolute/negate, SAT, packed
  register/uniform/constant inputs and separate lane immediates (high first in FORMAT). HFMA2 widens to
  binary128 for an exact finite product and sum and narrows once; HADD2 widens to fp64. Both results are
  captured before writing an overlapping destination. A low-half broadcast (H0_H0) reads only two bytes
  in native p-code, with or without |x|/-x (applied to the half).
- Exact HADD2/HFMA2 F16_V2 leave .FTZ/.FMZ opaque; readable ignores them (also on SM100+ `hfma2_mma`
  classes). BF16/E6M9 arithmetic formats, F32/H0_NH1 inputs and RELU stay opaque.
- Swizzles and HMNMX2's per-format NaN test/order are `select_lookup` values (forking on them made SM90
  18.75 MB).
- HADD2.F32 is native on SM86/89/90/100/101/103/120: it adds the selected low half lanes from `-RZ` and
  the second operand, rounds to binary16 and widens that result to F32; FTZ flushes the inputs and the
  binary16 result. The second-source H1_H0/H0_H0/H1_H1 selectors, absolute/negate, FTZ/no-FTZ,
  SAT/no-SAT, and available register, uniform, immediate and constant-bank forms are covered. SM89 GPU
  fixtures confirm the register form for all 48 selector/modifier combinations (128 inputs each); RC
  forms are emulator-tested. SM90 CX is emulator-tested; other CX forms use the shared bounded-load
  provider where their md classes exist.
- HMUL2 multiplies each selected pair after widening to fp32, which holds the exact finite binary16
  product, then narrows once. F16_V2 supports FMZ, FTZ and no-flush modes, with SAT and no-SAT. FTZ
  flushes subnormal inputs and results, keeping their signs. On SM89, FMZ produces canonical +0 when
  either selected input has an all-zero exponent, including zero times infinity (GPU-checked). SAT clamps
  the result to [0, 1]. Register, uniform, immediate and constant-bank sources are covered where the md
  provides them. BF16_V2 and SM107's mixed formats: see [SM107](#12-sm107-2026-10-14).
- The Ghidra minimum-subnormal limitation also applies to FP16.

### HFMA2.MMA
- `HFMA2.MMA Rd, -RZ, RZ, imm_hi, imm_lo` under nofmz/nosat is native (`families/floating.py`). Hardware
  shows it preserves finite bits, signed zero, infinities and denormals, while canonicalizing each NaN
  half to `0x7fff`. The transform is evaluated from the encoded immediates at decode time, so the move
  is one COPY.
- 2026-10-22: the SM90+ `hfma2_mma__RRR` and `hfma2_mma__RIR` F16 forms share the binary16
  HFMA2 arithmetic implementation: two independent fused multiply-adds in the exact lowering,
  unfused in the readable lowering. `.SAT` clamps each lane; `.FTZ` flushes inputs and results to
  signed zero in the exact lowering. BF16, FMZ, predicate-output/RELU and other arithmetic source
  forms remain opaque. The existing packed input corpus supplies regression cases; its values,
  seeded sequence and operand pairings are unchanged.

### HMNMX2
- Native for materialized F16_V2, BF16_V2, E8M7_V2 and E6M9_V2 operands: register, uniform, immediate
  and ordinary constant-bank forms where present in the md; CX sources use the shared bounded-load
  provider on SM75-SM90. E8M7_V2 aliases BF16_V2's encoded format.
- `Pp` selects min when true and max when false. Per-lane H1_H0/H0_H0/H1_H1 selection and source
  absolute/negate modifiers are honored. F16 `.FTZ` flushes inputs and results to signed zero;
  BF16/E8M7 and E6M9 do not expose FTZ. `.NAN` canonicalizes NaN results to `0x7fff`. `.XORSIGN`
  compares magnitudes and applies the XOR of raw source signs. `.IS_A` is covered below.
- RRR/RUR/RCR require both absolute flags; RIR has only the register source's absolute flag, and its
  immediate keeps its sign.
- The SM89 whole-kernel oracle covers 19 HMNMX2 fixtures with 128 vectors each; exact/readable semantic
  matrices cover SM75-SM120.
- SM90+ `IS_A` predicate-output classes are native for supported source forms with `nan=nonan`; the
  `.NAN` selector stays opaque until its interaction with source flags is known. `Pu` and `Pv` report
  whether source A supplied the low and high lane results, based on NVIDIA patent US 12,554,493 and the
  `IS_A` selector. Exact/readable emulator and dataflow checks cover 45 cases on SM90 and 27 on SM100,
  SM101, SM103 and SM120. Not GPU-verified: SM89 cannot execute the SM90+ form, and the patent does not
  independently establish every tie/NaN detail. The default two-NaN path's A flag is modelled but not
  measured.

### Fixtures
- Seven GPU fixtures patch only an arithmetic word for lane/modifier/MMA testing, then execute the same
  cubin on GPU and emulator. FP16 has ordinary/saturating and broadcast/modifier fixtures; MMA move
  constants include signed zero, negative signaling/quiet NaNs, denormals and infinities.

## 8. Conversions

### FRND, I2F/I2FP, F2F, F2I (`convert.py`, 2026-10-13)
- FRND (F32/F64), I2F/I2FP (8/16/32/64-bit sources with bsel/hsel, F16/BF16/F32/F64 destinations), F2F
  (F16/F32/F64) and F2I (F16/BF16/F32/F64 to S/U 32/64) are exact in every rounding mode on `fpformat`
  (`int_parts`, 64-bit `round_parts`; `arith` picks W=64 for F64).
- Rounding modes, destination formats, signedness and NTZ are decode-time lookups
  (`fpformat.chosen_rounding`, shared with F2FP; `fpformat.round_up` is the one round-up rule): forking
  per label made SM86 exceed 16 MiB.
- F2F/F2I/I2F/FRND with a half/byte source read only that slice (checked by `part_selector` in
  `tests/harness/dataflow.py`, used by `test_pcode_dataflow`).
- .FTZ flushes a denormal input (FRND, F2I; SM89). F2F .FTZ and its BF16/E8M7/E6M9 formats are
  unmodelled.
- NaN results follow SM89: FRND F32 gives 0x7fffffff, F64 keeps the quieted sign/payload. F2F
  canonicalizes NaNs between F16 and F32; other pairs (F16/F64, F32/F64) keep the register's quieted
  sign/payload, ignoring -/|.| (shared GPU probes 2026-10-16). I2F's 64-bit immediate is the high word of
  its source (2026-10-16).
- F2I: NaN gives 0 with .NTZ, else the destination's top bit (SM89, also U64); infinities and overflow
  saturate to the destination limits. The readable lowering takes every mode as TRUNC. 8/16-bit
  destinations and predicate outputs are opaque. Absolute clears the source
  sign bit before negate toggles it; patched SM89 GPU fixtures verify absolute, negate and combined
  forms against an independent integer reference (128 whole-kernel comparisons per form). The
  F32-to-S32 `.TRUNC.NTZ` form was the first hardware-verified one (NaN to zero, infinities and overflow
  clamped).

### F2IP
- Native for packed F32-to-S8/U8 conversion with round-to-nearest-even, NTZ, no RELU and materialized
  32-bit inputs, including CX loads on SM75-SM90. Each input is rounded and saturated independently;
  NaNs become zero and infinities saturate by sign. The first input occupies bits 8-15, the second bits
  0-7, and the low 16 bits of C occupy bits 16-31. Other destination/source formats, rounding selectors
  and modifiers stay opaque; F2IP with C `.H1` (`extract`) is opaque (the earlier emitter ignored it and
  kept C[15:0]).
- The SM89 GPU oracle verifies the packed byte order and edge behavior for directly materialized
  sources. The SM89 CX probe covers 16 S8/U8 forms and passes 2,048 GPU/reference and exact/readable
  P-code comparisons per mode.

### F2FP: arithmetic core (`fpformat.py`)
- Values are exact `Parts` (sign, sig, lsb, top); `round_parts` rounds once into any `Dest`
  (`general=True` for scaled parts: an exact left shift where no rounding is needed); `widen` is exact;
  UE8M0 scales are exponent offsets (`Parts.scaled`), not float multiplies. Exhaustive checks against
  `tests/refs/fpformat.py`: 0 mismatches.
- `widen` is static per format (re-bias normals; subnormals shift onto the destination's grid or, where
  it makes them normal, take one constant-shift code per leading bit), and unscaled upconverts choose each
  format's whole widening at decode time.
- FTZ/FMZ are not selector operands in the inspected SM89 F2FP md classes.
- SM86-SM90 UF2FP name formats as one `dfmt.sfmt` selector; `uf2fp_merge_c__URURI` (immediate C, no
  `extract`) stays opaque.
- Stochastic rounding (`f2fp_rs*`), the UE8M0 (E8) downconvert destination and the UE5M3 upconverts
  stay opaque.

### F2FP: F32 to F16/BF16/TF32/E6M9
- `PACK_AB` F32-to-F16 and F32-to-BF16 for RN and RZ, including RELU, SATFINITE and their combination,
  with R/UR, immediate and ordinary constant-bank sources. The first source converts into bits 31-16 and
  the second into bits 15-0. F16 conversion uses integer rounding, including subnormals and overflow;
  BF16 uses its F32 high half and ties-to-even, with NaNs canonicalized to `0x7fff`.
- F32-to-TF32 `PACK_B` supports RN/RZ and RELU with R/UR, immediate and ordinary constant-bank sources;
  TF32 keeps ten fraction bits and rounds the discarded 13 according to RN/RZ. ptxas emits
  `F2FP.TF32.F32.PACK_B` for the RN PTX probe on SM90, but SM89 cannot execute it; TF32 hardware
  validation is pending.
- F32-to-E6M9 `PACK_AB` is native for RN/RZ with RELU, SATFINITE and their combinations on
  SM86/SM89/SM90, where the md exposes that destination. E6M9: one sign bit, six exponent bits with bias
  31, nine fraction bits. RN uses ties-to-even; RZ truncates overflow to max finite; SATFINITE clamps
  infinity and overflow to signed max finite; RELU maps negative outputs, including negative zero, to
  positive zero and leaves NaNs canonical at `0x7fff`. Exact and readable emulator suites pass on all
  three architectures, including R/UR/immediate/direct-C sources where present. A patched SM89 GPU
  probe covered RRR, RIR immediate and RCR constant-bank forms: 13,824 packed-lane comparisons across
  eight selector combinations (RN/RZ, RELU, SATFINITE) matched an exact-rational reference, and 1,536
  whole-kernel lanes matched P-code. The probe includes 576 lanes per selector/source form and checks
  underflow and overflow boundaries, random values, infinities, NaNs and signed zero.
- `MERGE_C` is native for F32-to-F16/BF16/E6M9 `PACK_AB` classes, with RN/RZ, RELU, SATFINITE and
  their combinations. The converted value occupies the low 16 bits; H0/H1 selects the low/high half of
  the merge source, which is copied into the result's high 16 bits. R/UR, immediate, ordinary
  constant-bank and SM75-SM90 CX forms are supported where present. SM89 direct GPU probes established
  H0/H1 placement for F16 and confirmed H0 for BF16 and E6M9; exact and readable suites exercise the
  ordinary source forms on all nine architectures (SM89 cannot execute the SM100 selector forms).
- Evidence: immediate and ordinary constant-bank sources pass exact/readable emulator fixtures; SM89 GPU
  probes exercise F16 PACK_AB immediate and ordinary constant-bank sources (128 vectors each); other
  source combinations have emulator coverage only. The SM89 oracle confirms the four F16 and BF16 RN/RZ
  forms plus RELU, SATFINITE and combined RELU/SATFINITE for both formats (128 whole-kernel vectors per
  form). Exact/readable semantic and dataflow suites pass on all nine architectures.

### F2FP: 8-bit and 6-bit formats (E4M3, E5M2, E3M4, E2M3, E3M2, UE8M0)
- F32-to-E4M3/E5M2 `PACK_AB_MERGE_C` is native for RN, SATFINITE, no RELU, and R/UR, immediate and
  ordinary constant-bank inputs, with H0/H1 selection of the carried C half. E5M2 infinity clamps to max
  finite; NaNs use `0x7f` with the source sign. The SM89 oracle confirms H1 C merging in the in-place
  `Rd=Rc` form for both formats against independent references and P-code (128 whole-kernel vectors per
  format); distinct-destination merge results and H0 hardware behavior are unverified.
- F16/BF16 and F32 downconversion to E4M3/E5M2 is native for RN, SATFINITE, optional RELU and H0/H1
  merge selection. The ordinary R/UR/immediate forms pass 11,040 exact and readable cases per mode on
  SM100/SM101/SM103 and SM120; SM120's six uniform-register `UF2FP` forms are included in its 14,592
  cases. Dataflow passes for all ten ordinary forms per mode on SM100/SM101/SM103 and all sixteen
  ordinary plus uniform forms on SM120. NaN sign and payload are compared by class because F16 widening
  may canonicalize them. These emulator results do not establish SM100+ hardware behavior; other 8-bit
  destination formats and rounding/saturation selectors and stochastic rounding stay opaque.
- `F16.E4M3/E5M2 UNPACK_B` is native for RN with optional RELU and R/UR, immediate and ordinary
  constant-bank sources, with H0/H1 selection of the source half. RELU maps negative results, including
  negative zero, to positive zero per lane; NaNs keep canonical positive `0x7fff`. Finite E4M3 values
  are normalized exactly; E5M2 finite values and infinities widen by shifting the encoding. Both formats
  canonicalize every NaN to positive `0x7fff`, as established by exhaustive SM89 byte probes: the four
  unpack probes compare H0 and H1 conversions for E4M3 and E5M2 across all 256 source bytes (128
  whole-kernel vectors per form), and four RELU probes patch only the RELU selector bit and compare GPU,
  P-code and an independent per-lane reference for 128 vectors per form, covering every source byte in
  both extracts and both formats.
- E3M4 (SM100/SM101/SM103/SM120) widens to F16 for RIR/RRR/RUR sources with RN, UNPACK_B and optional
  RELU. An [independent E3M4 format reference](https://github.com/tjthereal/fp8/blob/main/READMEFP8.md)
  describes sign, 3-bit exponent (bias 3), 4-bit fraction, subnormals and the reserved NaN encoding.
  Exhaustive fixtures check all 256 source bytes in both selected halves and both RELU settings: 3,072
  cases per mode on each architecture. Dataflow passes for all three source classes in both modes.
  Emulator-verified from format evidence, not GPU-measured.
- `F16.E2M3/E3M2.UNPACK_B` (SM100/SM101/SM103/SM120) widens both padded 6-bit lanes to F16 for
  RIR/RRR/RUR sources, with RN and optional RELU. The reference matches CUDA's host FP6 conversion for
  all 64 encodings per format. The SM100 full suites include 4,608 exact and readable cases; the
  targeted SM101/SM103/SM120 matrix has 1,536 cases per mode and arch. Hardware validation pending.
- SM100 UE8M0 `UNPACK_B` to BF16 is native for RIR/RRR/RUR with H0/H1 selection. All 256 source bytes
  pass exact/readable emulator fixtures in both lanes on SM100/SM101/SM103/SM120. Its reference follows
  CUDA's host UE8M0-to-BF16 conversion, including `0x00` to the `2^-127` BF16 subnormal and `0xff` to
  canonical `0x7fff`. No compatible GPU for hardware validation.

### F2FP: 4-bit formats (E2M1, E0M3)
- E0M3 is interpreted as a sign bit plus a 3-bit integer magnitude (-7...7, with signed zero).
- `E2M1.UNPACK_B` (SM100) widens the two nibbles of a selected source byte to F16 for RIR/RRR/RUR
  sources, with RN and optional RELU. B0-B3 select the byte; all 256 byte values pass exact/readable
  emulator fixtures on SM100/SM101/SM103/SM120. The reference follows PTX's documented E2M1-to-F16x2
  packing order. E0M3's unpack: all 256 byte values pass exact/readable emulator fixtures and RIR/RRR/RUR
  dataflow checks on SM100/SM101/SM103/SM120; hardware validation pending (SM89 only).
- F16/BF16-to-E2M1/E0M3 downconvert is native for the SM100/SM101/SM103/SM120 `UNPACK_B_MERGE_C`
  classes in RIR/RRI/RRR/RRU/RUR forms. The B input produces the low output byte, C the next byte, and
  Rd receives the packed 16-bit result. E2M1 follows CUDA's `cuda_fp4.hpp` RN-even conversion, signed
  maxnorm saturation and NaN-to-positive-maxnorm rules. E0M3 finite inputs round to nearest-even
  magnitude and saturate at 7; NaNs canonicalize to positive 7 before RELU. The E2M1 matrix passes 1,560
  exact/readable fixtures per architecture; the expanded E2M1/E0M3 matrix passes 4,320 exact and
  readable fixtures per architecture, covering RIR/RRI/RRR/RRU/RUR, F16/BF16, threshold neighbors, edge
  encodings, RELU and deterministic random inputs. E0M3 hardware behavior is unverified.
- F32-to-E2M1/E0M3 is native for the SM100/SM101/SM103/SM120 `PACK_AB_MERGE_C` classes in
  RIR/RRI/RRR/RRU/RUR forms. A and B produce the high and low nibbles of Rd[7:0], and C supplies the
  preserved upper 24 bits. E2M1 uses RN-even conversion with signed maxnorm saturation; E0M3 uses signed
  integer magnitudes 0-7 with RN-even rounding. Both support SATFINITE and optional RELU. The F32 FP4
  matrix passes 1,440 exact and 1,440 readable cases per architecture, including source forms, threshold
  neighbors, special values and deterministic random inputs; all five forms pass dataflow checks in each
  mode. PTX 9.0 compilation with CUDA 13.0.88 confirms the E2M1 lowering to
  `F2FP.SATFINITE.E2M1.F32.PACK_AB_MERGE_C`. No hardware execution validation (SM89); E0M3 is
  format-derived.

### F2FP: scaled conversions
- Scaled upconverts model only `iswzC=H0` (scales Rc[15:0], low lane low byte): ptxas puts the PTX scale
  pair there independent of `extract_B` and never emits H1/B0-B3
  (`research/semantics-migration/f2fp_scaled_ptx.md`; the earlier emitter took the scales from the
  `extract_B` half).
- `f2fp_4b_upconvert_scale__` is native for RIR/RRI/RRR/RRU/RUR on SM100/SM101/SM103/SM120. It selects
  one byte from the FP4 source, converts its low/high nibbles to BF16, then multiplies each lane by the
  corresponding low/high UE8M0 byte of the second source. The E2M1 rule follows the PTX ISA; E0M3 uses
  its sign-plus-magnitude integer grid with the same per-lane scaling. RN, SATFINITE and optional RELU
  are implemented. All 256 scale bytes, FP4 codes, B0-B3 selectors, both formats, RELU settings and
  source forms pass 20,480 exact/readable fixtures per architecture, with focused dataflow checks for
  both formats and all five forms. Emulator-only (no SM100+ GPU).
- `f2fp_8b_upconvert_scale__` is native for RIR/RRI/RRR/RRU/RUR on SM100/SM101/SM103/SM120. E5M2, E4M3,
  E2M3, E3M2, E3M4 and signed S2F6 bytes widen to BF16, then multiply by the matching UE8M0 byte. H0/H1
  selects the source pair and matching scale pair; RN, optional RELU and optional SATFINITE are
  implemented. E5M2/E4M3/E2M3/E3M2/S2F6 follow the
  [PTX conversion specification](https://docs.nvidia.com/cuda/parallel-thread-execution/#data-movement-and-conversion-instructions-cvt),
  including S2F6's signed 8-bit value times 2^-6 and the PTX conversion-then-scale order; E3M4 uses the
  format reference above. Each of the six formats, five source forms, H0/H1, RELU and SATFINITE
  settings passes 61,440 exact/readable emulator cases per architecture. Each lane sweeps all 256
  source-byte and all 256 scale-byte values; the matrix does not enumerate every source/scale
  cross-product. Dataflow/control-flow checks pass 5/5 classes per mode on all four architectures. SM89
  cannot execute SM100 SASS. The PTX 9.2 compile probe is `tests/ptx/f2fp/f8_scaled_upconvert.ptx`;
  installed CUDA 13.0 ptxas supports PTX 9.0 and rejects the PTX 9.2 version and scaled modifier, so it
  supplies no compiler-lowering evidence.
- Scaled downconverts (`scaled_pair`): see [SM107](#12-sm107-2026-10-14).

## 9. Control flow
Family `families/control.py` (Phase 6a).
- `outputs(flow=goto|call|ret(target, cond))`. `i.branch_target(name)` is a direct target (SLEIGH: the
  branch-target subtable, `goto`/`call` without brackets; lift: the absolute address, RSImm relative to
  the next instruction); other targets are values (`call [x]`, `return [x]`).
- The lifter normalizes a constant-true flow condition to `None`; false and dynamic conditions remain
  explicit. Flow travels through row merging as pseudo-outputs (`__flow_target`/`__flow_cond`).
  `evaluate.execute` sets `State.pc`, so the lifted tests check `__counter`.
- Modelled:
  - BRA, including .DIV/.CONV with a `[~]URb` lane mask (not without one, nor with a uniform predicate).
    Their divergence test reads the warp's waiting lanes (alive but not executing the branch) from the
    runtime op `sass_warp_waiting`, supplied by the warp context or the cooperative emulator as live
    lanes not at the branch's PC, and failing without one (`Instruction.environment`).
  - EXIT (.NO_ATEXIT too).
  - CALL.NOINC: absolute/relative immediate, c[][] and absolute register targets (the earlier emitter
    treated relative offsets as absolute).
  - RET.ABS/REL.NODEC; RET.REL is next + Ra + offset.
  - NOP.
- Branch-stack changes and reconvergence-sensitive variants need additional state. Fallback control flow:
  [routing](#routing-fallbacks-and-status).

## 10. Memory and special registers
Family `families/memory.py` (Phase 6b).
- `outputs(stores=[store(space, addr, value, cond=None)], effects=[effect(name, args...)])`; a store
  with `cond` is skipped when it is 0. Stores and effects merge as pseudo-outputs (a store's width
  follows sz, so `Merger.bodies` splits rows whose output widths differ).
- `i.encoding(name)` passes a selector's raw value (the memory-ordering controls `sass_memory_order`
  validates). `i.special(name, w)` reads the special register a SpecialRegister field selects (SRZ 0).
  `i.special_register(name)` reads a named one (`SR_SWINLO`) whatever the encoding.
- Families: LDG/STG/LDS/STS/LDL/STL/LDC/LDCU, ULDC c[][] and S2R/CS2R/S2UR (plain copies of named
  special registers; SRZ reads 0). Uniform address parts `Ra_URb`/`Ra_URc` add to the address.
- SM100+'s LDCU joins LDC: `URd = c[bank][URa + offset]` for `ldcu_const_RCR_`; CX, `ur_offs`, 256-bit
  and `.TEXUNPACK` forms stay opaque.
- Generic LD/ST (2026-10-15) route a 64-bit address inside the shared window (`SR_SWINHI:SR_SWINLO`,
  `SR_SWINSZ` bytes) to `shared` at its offset from the base, else inside the local window (`SR_LWIN*`)
  to `localmem`, else to `ram` (SM89 GPU, `docs/hardware-facts.md`). A load is a choice between the
  three, a store three stores under exclusive conditions. The environment supplies the window registers
  (the whole-kernel emulator seeds `sass.launch.WINDOWS` and the matching bank-0 constants).
  32-bit (no `.E`) generic addresses stay opaque.
- Descriptors: every class with a descriptor operand (`*_memdesc_*`) passes one `sass_memory_descriptor`
  effect: the selected URx (`Ra_URb`/`Ra_URc`) for both values of SM89's `e_desc` and every SM90+ form.
  SM89 GPU shared-case probes (2026-10-16) show that `noexp_desc` hides the descriptor in the printed
  text but still reads it: bit 59 alone traps with either setting. The access itself ignores the
  descriptor (SM89 GPU); the runtime rejects range-form descriptors (bit 59), whose traps are
  unpredictable.
- A load's `Pnz` input zero-fills (`Rd = Pnz ? mem : 0`, SM89 GPU). The md also writes `Pnz`, and the
  load writes it back unchanged, as on the GPU (like R2P's masked-off predicates).
- Opaque: dual registers, predicate results (`Pu`, see `docs/open.md`), non-IA modes, SM75 `.ZD` (no
  default, meaning unknown; the earlier emitter ignored it), indexed-register memory, descriptors of
  ATOM/RED/LDGSTS, extended/uniform memory, unsupported address modes and 256-bit variants.

## 11. Runtime primitives
- `src/sass/semantics/primitive.py` names the primitives families use (2026-10-22): `mufu_<fn>`, the
  warp's `lane`, `active`, `waiting`, `read_lane`, `ballot`, `match`, `redux_<op>`, QSPC's
  `address_space`, and the synchronization effects `bar`/`bssy`/`bsync`/`break`/`warpsync`/`bmov`.
  SLEIGH calls a node as `local t = sass_prim_<name>(args)`; `evaluate.execute(semantics, state,
  runtime=...)` and Ghidra's emulator both run it through `Runtime.call`, so the Ghidra-free tests
  evaluate primitive cases with the case's context. Arguments are the values the family passed and
  nothing else: the runtime needs no class, manifest or label tables.
- `src/sass/semantics/runtime.py` (test-only; Ghidra's language never loads it) implements them by name.
- Warp operations use an explicit 32-lane context (values, predicates, active mask and lane index;
  cooperative kernels gather it per instruction, each lane's operand over its md span). A collective's
  lane argument is the raw operand, since the context holds the other lanes' raw values, so transforms
  come after (VOTE's `!Pp` is `active & ~ballot`; signed REDUX MIN/MAX are their own operations). The
  families are native around these: SHFL computes its source lane and bound and reads it with
  `read_lane` (a valid source that isn't executing gives 0, SM89); VOTE's modes compare the ballot with
  `active`.
  - VOTEU is VOTE into URd/UPu.
  - MATCH.ANY gives the active lanes holding the lane's value (U32, or U64's register pair); MATCH.ALL
    gives the active mask and Pu = 1 when they all agree (else 0, 0).
  - REDUX reduces Ra over the active lanes into URd (SUM/AND/OR/XOR, MIN/MAX signed with `.S32`).
  - ELECT's leader is the lowest active lane of URa's mask (`~URa` with its invert bit, FLO's
    `leading_zeros`); Pu = this lane
    leads and URd = the leader's lane id, following PTX `elect.sync`. ELECT's `Pp` form and
    `.IGNOREKILL` are opaque.
- Synchronization: BAR/BSSY/BSYNC/BMOV/BREAK/WARPSYNC are effects `sass_prim_<op>(enable, inputs...)`
  (Pp or 1, then multi-valued selectors' encodings and operand values in md order; B0-B15 are not in the
  register model yet, so barrier registers are encoded arguments). They need an explicit
  synchronization event context and acknowledgement; recording an acknowledged event is not a
  warp-scheduling implementation. BMOV to Rd reads `bmov(cbu_state, ...)`: the handler's value for the
  state's encoding. The cooperative Ghidra kernel scheduler (`ghidra_emulate.run_kernel`) does schedule
  BAR.SYNC and WARPSYNC (2026-10-22): a WARPSYNC lane waits until every live lane its mask names waits
  at a WARPSYNC with the same mask, following CUDA's `__syncwarp` contract (exited lanes are not waited
  for; disagreeing masks or a mask omitting the lane raise). Its modelled forms are the immediate,
  register and uniform-register masks without `.EXCLUSIVE`, and SM90's `.ALL`; `.EXCLUSIVE`,
  `.COLLECTIVE`/`.REL` and constant-bank masks raise. ptxas emits `WARPSYNC Ra` (Ra = `VOTE.ANY`'s
  active mask) before warp collectives in divergent SM90 code.
- QSPC computes the generic address natively and asks the environment for its space
  (`address_space`, a tag; shared and shared::cluster may both match). It needs
  `context["address_spaces"]`, a mapping from `global`, `local` and `shared` to half-open
  address intervals; SM90+ also accepts `shared_cluster`. `S` queries CTA shared memory; SM90+'s `D`
  queries cluster-visible shared memory, corresponding to PTX `isspacep.shared::cluster`. `Pu` reports
  whether the address belongs to the queried space. `Rd` reports a one-hot input-space tag: global `1`,
  shared `2`, local `4` (SM89 GPU probes). `Rd` with D stays opaque until checked on SM90 hardware. The
  emulator cannot infer generic-pointer provenance from an integer address; missing mappings, unmapped
  addresses or overlapping ranges raise `UnsupportedSemantics`.
- MUFU is the primitive `mufu_<fn>` (`Rd = sass_prim_mufu_sqrt(Rb)`; the family applies the source
  modifiers, the 16-bit forms' half select and Rd's zero high half). The CPU runtime implements
  full-width SIN/COS with the recovered SM89 integer model (`fp/mufu.py`, 2026-10-19): fixed-point turn
  reduction, 64 integer coefficient triples, retained square terms and final float32 rounding toward
  zero. The model is generated wherever those forms exist; hardware evidence is SM89 only. The other
  functions and the 16-bit forms raise until a hardware model exists.
- CPU arithmetic references use the
  [NVIDIA PTX arithmetic definitions](https://docs.nvidia.com/cuda/parallel-thread-execution/) as
  reference conventions; GPU checks are needed to establish SASS equivalence.

## 12. SM107 (2026-10-14)
SM107 is its own ISA, not an alias: the SM103 md plus SM120's CONSTANTS and IMNMX shape (Pu/Pv/Pq,
`FMT_flo`), minus E0M3/E3M4, plus UE5M3 and 56 classes no other architecture has. No SM107 GPU: nothing
here is hardware-verified. The evidence is ptxas 13.4.92 (`nvidia-cuda-nvcc` 13.4 wheel; 13.3 has no
sm_107) lowering PTX for `sm_107a`, the PTX ISA 9.4 text (docs.nvidia.com, §9.7.5 mixed precision,
§9.7.10.24 `cvt`), and CUDA 13.4's `cuda_fp8.hpp` host conversions. ptxas encodes its mnemonic strings,
so a `strings` search for opcodes finds nothing: probe it with PTX instead. The probes: `research/sm107/`
(`probe.py PTXAS candidates.txt`, output in `results.txt`).
- Packed binary32 pairs: one family for FADD2/FMUL2/FFMA2 (SM100+) and FHADD2/FHFMA2/FHMUL2
  (`floating.py`, `pair_lanes`). ptxas lowerings of f32x2 PTX fixed the source lanes: a pair is
  (lane 0 = Rn, lane 1 = Rn+1); `.LO_HI` swaps; `.F32` and immediates (32 encoded bits, `F32Imm(64)`
  notwithstanding) broadcast; `|x|` applies to both lanes; lane 0 is negated by the `-` bit and lane 1 by
  `-` XOR NEGA `.NP`, after the swizzle (`-R.NP` = negate lane 0, `R.NP` = lane 1, `-R` = both; also for
  uniform registers). `.NP` on a broadcast has no ptxas form (it copies the register instead) and stays
  opaque; `.FMZ` stays opaque.
- PTX 9.4 mixed precision (sm_107f) lowers 1:1:
  - `add/sub{.rn,.rz,.rm,.rp}.f32x2.{f16x2,bf16x2}.f32x2` -> FHADD2 `a16b`
    (`d[0:31] = convert(a[0:15]) + c[0:31]`);
  - `fma.rnd.f32x2.{f16x2,bf16x2}.f32x2.f32x2` -> FHFMA2 (one fused rounding);
  - `add/sub.rz.ftz.f16x2.f32x2.f32x2` / `.rz.bf16x2` -> FHADD2 `d16b`;
  - `mul.rz.ftz.f16x2.f32x2.f32x2` / `.rz.bf16x2` -> FHMUL2 (`d[0:15] = convert(a[0:31] op c[0:31])`, RZ
    output).

  ptxas requires `.ftz` for the F16x2 result and the md forbids it for BF16x2; PTX's `.ftz` for half
  types is "flushes subnormal inputs and results to sign-preserving zero" (stated for neg/min/max, not
  restated in §9.7.5), modelled as flushing the binary32 inputs and the F16 result. RZ to binary32 then
  RZ to the half format is RZ once. NaN results follow F2FP's F16/BF16 packs (inferred).
- HMUL2 on SM107 names all three formats in one operand (`Dfmt.Afmt.Bfmt`, `F16x2.F16x2.F16x2` =
  `F16_V2`); PTX `mul.f16x2.f16x2.bf16x2` / `mul.bf16x2.bf16x2.f16x2 d, a, c` lower to the mixed forms (no
  rounding modifier or `.ftz` accepted) with `d = a * convert(c)`: c rounded (RN) to d's format, then the
  product rounded RN. BF16_V2 (SM86+) and the mixed forms are native without SAT/FTZ/FMZ.
- VISET (`integer.py`): PTX `set.<cmp>.u32.{s32,u32}` (defined as 0xffffffff/0) lowers to a bare
  `VISET.BM.<cmp>.{S32,U32}` whose result is stored as is, so `.BM` is an all-ones/zero mask;
  `vset2/vset4` lower to per-lane `.S32` VISETs followed by `& 1`. ptxas never emits the packed
  16x2/8x4 formats; they apply the same rule per lane.
- F2FP scaled downconverts (`scaled_pair`): ptxas puts PTX cvt's scale factor in Rc's high half:
  `.scaled::n2::ue8m0` (S2_6) as `IMAD.U32 Rc, s, 0x10000` (A's scale Rc[31:24], B's Rc[23:16]; the
  unscaled S2_6 form passes 0x7f7f0000), and SM107's `.scaled::n1::ue8m0` (`SCALE_BY_C`) as
  `IMAD.SHL.U32 Rc, s, 0x1000000` (Rc[31:24] for both). The S2_6 family read Rc[15:8]/Rc[7:0] before
  2026-10-14. Rd[31:16] = Rc[15:0], MERGE_C's half (the unscaled forms' default H0), is inferred: ptxas
  always passes zero there. The SM107 forms round RN/RZ to E4M3/E5M2/E2M3/E3M2/UE5M3 like the unscaled
  downconvert.
- UE5M3 (PTX 9.4 `.ue5m3x2`; ptxas `F2FP.UE5M3.F32.PACK_AB_MERGE_C`): `cuda_fp8.hpp`'s host conversion
  drops the sign first, bias 15, largest finite 0xfe, NaN 0xff, no infinity; overflow gives 0xfe under
  SATFINITE, RZ or RM, else 0xff; Inf 0xfe only under SATFINITE. Native in the plain and scaled
  downconverts; the UE5M3 upconverts (`F2FP.F16.UE5M3.UNPACK_B`, scaled to BF16) stay opaque.
- `.PZO` (PTX cvt: "if the result of the conversion produces -0.0, it will be forced to +0.0") is native
  in the 8-bit downconverts; for S2_6 it changes nothing (no -0).
- Opaque with md dataflow, as their analogues everywhere: USYNCS, SYNCSU, SYNCS phasechk_pv, UCCTL,
  UBLKL2CCTL, UTMAL2CCTL, UTMASTG/UTMAREDG URc, UTRACEEVENT, LDTM sparsify, `__HIR0X1C0/1D0`; IMNMX's
  Pu/Pv/Pq forms (SM107, SM120); the E8 (UE8M0) downconvert destination.

## 13. Runtime ABI
- Opaque: `sass_opaque_OPCODE(inputs...)`, where inputs are the register operands and decoded immediates
  in operand order. No raw word (nothing consumed it) and no SM (it comes from the language id).
  Modifiers, scheduling hints and reserved bits are not arguments; they remain in the instruction bytes
  at the call's address.
- An opaque call with several outputs returns one byte bundle, first output in the low bytes, which
  preserves overlapping source/destination values. All inputs are evaluated before assigning any
  output. Sinks discard writes to zero/true registers and register-bank overflow views.
- Primitive: `sass_prim_<name>(args...)` (`src/sass/semantics/primitive.py`), a value of the node's
  width, or a synchronization effect (no result) whose first argument enables it. The arguments are
  the values the family passed, nothing else.
- `Runtime(sm, context=None, handlers={name: callback})` permits environment implementations;
  callbacks receive `(arguments, output_bytes, context)`. Without a handler, unknown operations raise.
- The JSON-line Ghidra harness accepts named special-register values in `registers`,
  `context.operands` keyed by `class.operand` (for `sass_operand_value`), `context.warp` and
  `context.synchronization`. Memory initialization is explicit.

## 14. Build and verification

### Commands
```sh
export SASS_SLEIGH_OUT=/tmp/sass-semantics
nice uv run sass-build-languages --coverage --output /tmp/sass-semantics
# Finish compilation before parallel Ghidra tests. The build command writes
# compiled files atomically, checks the 16 MiB decompressed size limit, and
# rejects unused-temporary diagnostics.
uv run sass-coverage /tmp/sass-semantics --output processor/SASS/semantics/coverage.json
nice uv run pytest                                   # Python tests, incl. softfloat vs libm and lifted semantics
nice uv run pytest -m ghidra --arch all --fp all     # disassembly, dataflow, semantics, emulated GPU fixtures, coverage
nice uv run pytest -m gpu --require-gpu              # references vs the SM89 GPU
uv run python -m tests.semantics SM89 readable --family f2fp   # test_pcode_semantics outside pytest
```
To refresh all committed coverage files (manifests, evidence, `coverage.json`, report) without Ghidra,
use `uv run sass-refresh-coverage` (AGENTS.md). Run language builds and GPU tests outside the sandbox;
CUDA device access fails inside it. `NVCC` selects the toolkit. Without a GPU, `pytest -m gpu` skips;
`--require-gpu` fails. `uv run python -m tests.observe FAMILY` runs a family's fixtures and prints raw GPU
outputs without asserting.

### Ghidra emulation
- `src/sass/ghidra_bridge/ghidra_emulate.py` runs decoded p-code through Ghidra's `PcodeEmulator` (its
  real byte executor) and a userop library. It checks actual register values, memory and control flow,
  not a Python reimplementation of the instruction dispatcher. `tests/cases/*` cover values, memory,
  predicates, branches, execution limits and strict fallbacks (`test_pcode_semantics`).
- `GHIDRA_PY` selects the pyghidra Python interpreter. In a restricted environment, set
  `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` to writable scratch directories.

### Whole-kernel GPU fixtures
- The scalar GPU fixtures (`tests/harness/scalar.py`) compile SM89 kernels via the CUDA Driver API
  (ctypes) and execute their actual `.text` through Ghidra's normal instruction stepping, from the
  prologue through EXIT, comparing 128 random/edge input vectors per kernel (four blocks of 32),
  including NaNs, infinities, denormals, signed zero and integer boundaries. Target mnemonics must
  execute in the emulator (decoded with pydecode).
- Since 2026-10-14 every fixture has an independent reference: `test_refs_on_gpu` checks it against the
  GPU and `test_fixtures_emulated` checks the emulated kernel against it, so only the former needs a GPU.
- Results: 96 kernels, all 12,288 output comparisons passed on the RTX 4070 Laptop GPU (an earlier count,
  7,680, covered 60 kernels). The `isetp_ex` PTX 64-bit comparison compiles to a low-word ISETP followed
  by `ISETP.GE.U32.AND.EX`; all 128 vectors matched.
- Launch layout: the cubin's PARAM_CBANK and KPARAM_INFO records supply parameter offsets and sizes. For
  these SM89 kernels, bank 0 parameters begin at 0x160, with the two 64-bit pointers at 0x160 and 0x168.
  Actual constant sections are loaded into `cbank`; launch dimensions and named TID/CTAID special
  registers are seeded. A fresh emulator per thread isolates local memory. Shared-memory fixtures use
  cells private to each thread, so no cross-thread communication is modelled. Kernel PC bounds and an
  instruction budget reject fallthrough and infinite loops. The general Ghidra loader, compiler
  prototype, warp scheduler and concurrent memory model remain separate (Phase 6) work.
- Coverage: integer arithmetic, IMAD low/high/wide, LOP3, SHF, LEA, predicates/selects, PRMT,
  HADD2/HFMA2 and the MMA constant move, DADD/DMUL/DFMA/DSETP, FADD/FMUL/FFMA, FSEL, FMNMX, FSETP,
  F2I.TRUNC.NTZ, I2FP, moves, uniform/ordinary constant loads, global/shared/local loads and stores, and
  predicated branching with BSSY/BSYNC. FADD.FTZ and FFMA.SAT have dedicated fixtures. Directed rounding
  has eighteen: .RZ/.RM/.RP for each of FADD/FMUL/FFMA/DADD/DMUL/DFMA, each with edge vectors whose
  residual lies below the wide format's precision, exact cancellations and overflow. FSEL and FMNMX
  min/max have dedicated fixtures, including FMNMX.FTZ and mixed NaN/signed-zero/denormal pairs.
- Mode-specific fixtures replace one arithmetic word in a compiled kernel to request SASS modes
  directly; scheduling bits and compiled prologues remain intact. The manifest records these patches.
- Comparison: floating results are bit-exact except that two NaNs compare equal regardless of
  payload/sign (independently for each packed half); FSEL/FMNMX and MMA move constants are compared
  bit-exactly, including NaN canonicalization. FTZ and saturation have no additional numeric tolerance
  (`tests/README.md`, "Comparison tolerances").
- Outside this hardware validation: MUFU operations other than full-width SIN/COS, warp collectives,
  device calls/returns, barrier reductions and cross-thread memory ordering. The loop fixture explicitly
  acknowledges BSSY/BSYNC events; this verifies its per-thread result without asserting a warp scheduler.

### Carry fixtures
- `tests/cases/carry.py` adds 210 patched SM89 whole-kernel variants (26,880 comparisons) for IADD3, LEA
  and IMAD carry chains, including aliased IADD3 outputs, predicate inversion, negate/complement, shift
  boundaries and signed/unsigned full-width overflow. Independent integer references and actual Ghidra
  execution must both match the GPU. The probe checks compiler slot/register assignments before
  replacing eight reserved BAR slots and a placeholder arithmetic instruction; compiled addressing and
  EXIT remain. Other architectures are checked in the emulator, without hardware evidence.

### Shared GPU cases (2026-10-16)
- The exact-mode single-instruction cases also run through native GPU wrappers; see `tests/README.md`
  ("GPU tests", "Hardware evidence and probes"). These broader probes corrected SGXT's bit-field rule,
  ISETP.EX's constant F/T comparisons, FMNMX.XORSIGN's signed comparison before sign replacement, I2F's
  high-word 64-bit immediate, F16/F32 F2F NaN canonicalization, and descriptor reads under
  `noexp_desc`. F16/F64 F2F preserves quieted NaN payloads. Hardware verification remains SM89-specific;
  other architectures keep guarded native generation and independent reference checks.
