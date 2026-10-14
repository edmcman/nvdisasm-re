# Instruction semantics

## Semantics families (`src/sass/semantics/families/`, 2026-10-12)
- Families are Python functions over the md-decoded instruction (`@family('IMAD', 'UIMAD')`, `src/sass/semantics/family.py`), returning `outputs(Rd=..., Pu=...)` of `sass.semantics.bitvec` bitvector expressions (widths in bits, `extract`/`concat`). One definition serves `sass.semantics.lifter.lift` (Ghidra-free semantics, evaluated by `evaluate`) and the SLEIGH backend `src/sass/sleigh/lower.py`; `operations.emit` uses a family when one covers the class. Integer families are migrated (`src/sass/semantics/families/integer.py`), the FP32/FP64/packed-half floating families (`src/sass/semantics/families/floating.py`), F2I/F2IP (`src/sass/semantics/families/f2i.py`) and every F2FP form except stochastic rounding (`f2fp_rs*`, opaque) (`src/sass/semantics/families/f2fp.py`).
- Static answers (selectors `i.select_fork`, one family body per label, modifier bits `i.operand_*`, `i.is_zero`, `i.has_operand`, `i.select_lookup_const` label->number tables, `i.static` for immediates of at most 8 bits) are Python values that SLEIGH enumerates; operand values are expressions. `a if c else b`, `and`/`or`/`not` are rewritten so they work on both.
- A family that never asks about a selector, modifier bit or defaulted numeric operand models only its md default: any other value is unmodelled (opaque). Selectors without an md default (`/ONLY64:size`, `/XORONLY:xor`) must be asked. `i.ignore(name)` marks an operand irrelevant to an encoding (ISETP's Pr without .EX). Operands with bracketed parts (C, CX, A, DESC) are values, not selectors, even where the type names an md enum.
- md CONDITIONS are enforced in SLEIGH for family classes (except conditions on scheduling hints alone); `sass-compare-ghidra` expects those words rejected.
- The md's `PR` operand (P2R/R2P) is a 7-bit value, P0 the low bit. `Predicate_vimnmx` (VIMNMX.pred, P0-P6, no PT) is a P-file register type; its Pu/Pv semantics are unknown (opaque).
- UPRMT is PRMT's IDX mode (`/IDXOnly("IDX"):idx`). Constant-bank widths come from md ISRC_<slot>_SIZE, with known md errors corrected at load (`src/sass/md/fixes.py`, applied by `parse.load_classes`; `out/` keeps the md as shipped): SM75/SM80 IMAD.HI/.WIDE `c[][]` addends are 64 bits, not the md's 32 (their `cx[][]` forms and SM86+ say 64; ptxas adds 64-bit pointer parameters with `IMAD.WIDE.U32 R2, R2, R3, c[0x0][0x160]`, 2026-10-14). A `c[][]` operand's address is a decode-time constant (`Gen.immediate_text`, shared with `Fold`), so its load is one LOAD.
- F2FP on `src/sass/semantics/fp/fpformat.py`: values are exact `Parts` (sign, sig, lsb, top), `round_parts` rounds once into any `Dest` (`general=True` for scaled parts: an exact left shift where no rounding is needed), `widen` is exact, and UE8M0 scales are exponent offsets (`Parts.scaled`), not float multiplies. Exhaustive checks against `tests/refs/fpformat.py`: 0 mismatches. Scaled upconverts model only `iswzC=H0` (scales Rc[15:0], low lane low byte): ptxas puts the PTX scale pair there independent of `extract_B` and never emits H1/B0-B3 (`research/semantics-migration/f2fp_scaled_ptx.md`; the earlier emitter took the scales from the `extract_B` half). SM86-SM90 UF2FP name formats as one `dfmt.sfmt` selector; `uf2fp_merge_c__URURI` (immediate C, no `extract`) stays opaque. `widen` is static per format (re-bias normals; subnormals shift onto dst's grid or, where dst makes them normal, take one constant-shift code per leading bit), and unscaled upconverts choose each format's whole widening at decode time. F2I accepts .FTZ (toward zero every |x| < 1 gives 0, so FTZ cannot change the result) and falls back to its primitive; F2IP with C `.H1` (`extract`) is opaque (the earlier emitter ignored it and kept C[15:0]).
- Control flow (`src/sass/semantics/families/control.py`, Phase 6a): `outputs(flow=goto|call|ret(target, cond))`; `i.branch_target(name)` is a direct target (SLEIGH: the branch-target subtable, `goto`/`call` without brackets; lift: the absolute address, RSImm relative to the next instruction), other targets are values (`call [x]`, `return [x]`). Flow travels through row merging as pseudo-outputs (`__flow_target`/`__flow_cond`). `evaluate.execute` sets `State.pc`, so the lifted tests check `__counter`. Modelled: BRA (not .DIV/.CONV, uniform target/predicate), EXIT (.NO_ATEXIT too), CALL.NOINC (absolute/relative immediate, c[][] and absolute register targets; the earlier emitter treated relative offsets as absolute), RET.ABS/REL.NODEC, NOP.
- Memory and special registers (`src/sass/semantics/families/memory.py`, Phase 6b): `outputs(stores=[store(space, addr, value)], effects=[effect(name, args...)])`; stores and effects merge as pseudo-outputs (a store's width follows sz, so `Merger.bodies` splits rows whose output widths differ). `i.encoding(name)` passes a selector's raw value (the memory-ordering controls `sass_memory_order` validates); `i.special(name, w)` reads the special register a SpecialRegister field selects (SRZ 0). LDG/STG/LDS/STS/LDL/STL/LDC, ULDC c[][] and S2R/CS2R/S2UR are families (uniform address parts `Ra_URb`/`Ra_URc` add to the address); dual registers, explicit descriptors, predicate results, non-IA modes and SM75 `.ZD` (no default, meaning unknown; the earlier emitter ignored it) stay opaque.
- md CONDITIONS that read a span field (sz, e: register range/alignment rules such as "sz=64 -> Rd even") are checked per span variant (`Gen.conditions(k, domain)`, `span_variants(domains=True)`), with the span fields narrowed to the variant's values, not in the root pattern: at the root they multiplied the global decision tree (SM89 10 -> 76 MB with the memory families).
- Runtime-executed encodings (Phase 6c): a family raises `primitive()` (`sass_prim_*` behind the same selector guards; lift status `primitive`, value checks left to the emulator tests). `i.static_low(name, bits)` asks only the low bits of a wide immediate (UP2UR's 7-bit predicate mask; SLEIGH constrains a sub-field); fixed registers (`sem.reg('UP', k, 1, element=1)`) lower to their names.
- Conversions (`src/sass/semantics/families/convert.py`, 2026-10-13): FRND (F32/F64), I2F/I2FP (8/16/32/64-bit sources with bsel/hsel, F16/BF16/F32/F64 destinations), F2F (F16/F32/F64) and F2I (F16/BF16/F32/F64 to S/U 32/64) are exact in every rounding mode on `fpformat` (`int_parts`, 64-bit `round_parts`; `arith` picks W=64 for F64). Rounding modes, destination formats, signedness and NTZ are decode-time lookups (`fpformat.chosen_rounding`, shared with F2FP; `fpformat.round_up` is the one round-up rule): forking per label made SM86 exceed 16 MiB. F2F/F2I/I2F/FRND with a half/byte source read only that slice (`test_pcode_dataflow`'s `part_selector`). PLOP3/UPLOP3 (`integer.py`): Pu's truth table is enumerated, Pv looks its bit up (`vimm8 >> (4p+2q+r)`; enumerating both was 65536 rows); register-source classes stay opaque. FCHK (`floating.py`), R2UR (Pu=PT only), RET.REL (next + Ra + offset) are native; BMOV/BREAK/WARPSYNC are runtime primitives (`families/runtime.py`) (B0-B15 are not in the register model yet).
- `i.select_lookup(name, {label: expr})` returns the value a selector chooses as one decode-time choice instead of branching the family per label (FSETP's comparison, shared by Pu and Pv); `i.select_lookup_const` is its constant case. In SLEIGH, operand modifier bits (|x|, -x, ~x, !P) are likewise decode-time choices of the operand value, except on loaded operands (c[], cx[]), whose load each table would repeat. Both keep the enumeration small (FFMA has 6 modifier bits).
- Floating point in the IR: `fadd`/`fmul`/`ffma` (rounding mode, FMUL's 2^k scale), `flt`/`fle`/`feq`/`fnan`, `fconv`, exactly rounded (`sass.semantics.fp.softfloat`) in `lift`/`evaluate`. The SLEIGH lowering keeps the earlier strategies (RN at native width, FFMA/scaling via the doubled format, directed modes via TwoSum residual and a 1-ulp step); the readable language uses the plain formula, and families write readable FFMA/scaled FMUL as their unfused operations. FTZ (branch-free), SAT and min/max NaN/zero rules are family IR (`src/sass/semantics/families/floating.py`).
- SLEIGH backend: the rows of static answers are merged by anti-unification (one key at a time, default rows, rows the default reproduces absorbed, choices reused across outputs); outputs merge jointly, so a decode-time choice can select whole bodies (one constructor computes a shared constant load once). Cost = .sla size + 8 x executed operations. A subtable exporting a register exports the register itself: later outputs are captured before the first write.

## Operand values and instruction semantics (2026-10-07)
- `src/sass/semantics/operands.py` preserves exact register effects for all classes, and materializes typed immediate/table/modifier/constant-bank values for supported operations. CX/descriptors/attribute/stateful operands require an explicit `sass_operand_value` provider.
- Every opcode with semantics is a family (`src/sass/semantics/families/`, Phase 6c, 2026-10-13); `src/sass/semantics/operations.py` only routes a class to its family and `dispatch`es the native body behind decode-time selector patterns, with the opaque call or the family's runtime primitive (`fallback='prim'`, or `primitive()` rows such as QSPC and the warp/synchronization/MUFU family in `src/sass/semantics/families/runtime.py`) for the rest. Unknown conventions remain opaque: ISETP.EX, SM120 extra IMNMX predicates, descriptor/extended memory, branch-stack CALL/RET depth changes.
- FADD/FMUL/FFMA/FSETP are native p-code with .FTZ/.SAT/FMUL scale; only .FMZ uses `sass_prim_*` (`dispatch(b,'prim')`). RN FFMA is `float2float(fp64 a*b + c)`: double rounding, rare 1-ulp differences. Ghidra 12.1.4's emulator rounds results in (2^-150, 2^-149) to zero instead of 2^-149 (`FloatFormat.getEncoding` `n < 0` branch); tests exclude that band.
- `src/sass/semantics/runtime.py` (test-only; Ghidra never loads it) supplies strict CPU primitives; `src/sass/semantics/fp/softfloat.py` uses exact rational IEEE rounding. MUFU requires a hardware-specific handler. Warp operations use explicit 32-lane context (cooperative kernels gather it per instruction); synchronization events require acknowledgement and do not implement a scheduler. Barrier reductions require a handler.
- Special registers are a register file at offset 0x3000 named from the md `SpecialRegister` enum (`SR_CTAID.X`; `_64` views for CS2R.64); S2R/CS2R are plain copies and SRZ reads 0. Emulation seeds them by name like any register; unseeded ones read 0.
- Spaces: `ram`, `cbank` (bank << 32 | uint32(offset)), `shared`, `localmem`. The identifier `local` is reserved in SLEIGH. The Phase 5 test harness reads parameter offsets from cubin PARAM_CBANK/KPARAM_INFO records; these SM89 pointer kernels use bank 0 base 0x160. The general loader/compiler prototype remains Phase 6 work.
- Detailed per-class coverage manifests are generated beside each `.sla`; committed `processor/SASS/semantics/coverage.json` summarizes all architectures. Native status records a candidate override with selector guards, not all-encoding coverage.
- Every fallback (opaque or primitive) of a class with an md `BRANCH_TYPE` keeps its control flow (`Builder.flow`): direct targets (RSImm relative, UImm absolute) and returns use `taken = call(...); if (taken == 0) goto <untaken>; goto/call/return`, indirect targets use `dest = call(...); goto/call [dest]`. Tier B had silently dropped this (only native BRA/CALL/RET/EXIT branched), and SM90+ spell BRA defaults `nocond__div_conv`/`nodepth_bra`, so every SM90+ BRA was opaque without a goto. Guards on default selectors use the md default (`operations.default`). The dataflow test (`test_pcode_dataflow`) also checks Ghidra's flow type against `BRANCH_TYPE` per class.
- `uv run sass-census SMxx...` ranks classes in the real corpus by static occurrence and native/primitive/opaque status (each word's status is its family's decision for that encoding, via `lifter`; ~2 min per arch on 32 cores).
- Opaque ABI is `(inputs...)`: registers and decoded immediates in operand order; no raw word (nothing consumed it; the bytes are at the call's address) and no SM (comes from the language id). Primitive ABI is `(class ordinal, decoded values...)`; every primitive class has a single span variant, and the runtime rejects one that doesn't. Multiple destinations return one byte bundle, preventing duplicate stateful calls and preserving overlapping source/destination values.
- `src/sass/ghidra_bridge/ghidra_emulate.py` executes actual p-code with Ghidra's `PcodeEmulator`; `tests/cases/*` cover values, memory, predicates, branches, execution limits, and strict fallbacks (`test_pcode_semantics`). The scalar GPU fixtures (`tests/harness/scalar.py`) run whole SM89 kernels through EXIT; their references are checked against the GPU via ctypes CUDA Driver API (`test_refs_on_gpu`) and the emulated kernels against the references (`test_fixtures_emulated`). All 7,680 output comparisons passed (128 per kernel; FSEL/FMNMX checked bit-exactly; other floating NaN payload/sign differences normalized). Device access works outside the sandbox. Architecture-wide hardware coverage remains incomplete.
- Native selector checks are decode-time constraints, with separate opaque constructors. Encoded scalars use anchored constructor actions (`inst_start * 0 + expression`): unanchored computed exports crash Ghidra 12.1's native compiler. Register-view aliases avoid redundant snapshots; multi-output calculations capture inputs before writes.
- Expressions over constants and plain immediate fields are computed while decoding (`sass.sleigh.lower.Fold`: a constructor action exports the constant; comparisons and `ite` are carries of 64-bit masked sums), so the HFMA2.MMA constant move is one COPY. A register the constructor can only encode as RZ/URZ/PT is that constant in the family IR, so it folds like lift's: pseudo-op variants pass the operand values their pattern fixes (`Builder.known`, from `pseudo_variants`), and IMAD.MOV `Rd, RZ, RZ, Rc` is `Rd = Rc`. Where a register may or may not be RZ, no zero choice is made: it would shorten only the RZ path, and the cost counts the longest. Unused operand values and unreachable subtables are omitted; `sass-build-languages` rejects unused-temporary diagnostics.
- Compact p-code regression fixtures assert one operation for immediate/register MOV, two for ordinary IMAD/IADD3 and three for LOP3 XOR with discarded predicate output. Dataflow checks still require exact reads/writes, adjusted for unused boolean/constant-comparison inputs and byte-width store sources; opaque fallbacks retain full md reads.

- FP64 DADD/DMUL/DFMA are native for RN with pair/uniform/immediate/constant operands and absolute/negate flags. DSETP shares ordered/unordered comparison logic, including complementary predicates and AND/OR/XOR. Directed rounding and DSETP MIN/MAX stay opaque. DFMA uses binary128: exact product, rare possible double rounding on the sum/narrowing. FP64 has the same Ghidra minimum-subnormal underflow band exclusion. SM89 hardware fixtures cover all four families.

- Packed FP16 HADD2/HFMA2 support H1_H0/H0_H0/H1_H1, per-half absolute/negate, SAT, packed register/uniform/constant inputs and separate immediate lanes (high first in FORMAT). HFMA2 widens to binary128 for an exact finite product/sum and narrows once; HADD2 widens to fp64; HMUL2 multiplies natively. Low-half broadcasts read only two bytes in native p-code, with or without |x|/-x (applied to the half). Families (`src/sass/semantics/families/floating.py`, 2026-10-13): HADD2/HFMA2 F16_V2 (exact: FTZ/FMZ opaque; readable ignores them, also on SM100+ `hfma2_mma` classes), HADD2.F32 with FTZ/SAT, HMUL2 with FTZ/FMZ/SAT, HMNMX2 F16/BF16/E6M9 with .NAN/.XORSIGN/F16 FTZ/.IS_A. Swizzles and HMNMX2's per-format NaN test/order are `select_lookup` values (forking on them made SM90 18.75 MB). BF16/E6M9 arithmetic formats, F32/H0_NH1 inputs and RELU stay opaque. HFMA2.MMA is native only for `-RZ, RZ, immediate` with nofmz/nosat: it preserves zeros/infinities/denormals but maps each NaN to 0x7fff (SM89 hardware). Decode-time canonicalization keeps it one COPY. Seven GPU fixtures patch only an arithmetic word for lane/modifier/MMA testing, then execute the same cubin on GPU and emulator.

- Step 2 adds mixed `URb`/`URc` inputs, uniform opcode aliases, native PRMT (all seven modes), FSEL and ordinary FMNMX. FMNMX numeric inputs win over NaN, two NaNs return `0x7fffffff`, and signed zeros choose -0 for min/+0 for max. `.NAN`, `.XORSIGN`, predicate-result `.IS_A`, packed/saturating VIADD and its carry modes stay opaque. Hardware coverage includes SM89 FSEL and FMNMX/FTZ; FSEL.FTZ remains emulator-tested. All nine architectures pass value/dataflow tests; rejected class selectors are reported as opaque in coverage.

- IMAD.HI selects the high word of the full 64-bit product plus 64-bit addend, including low-word carry; CUDA mad.hi lowering supplies the addend in the high register of a pair. SM75/SM80 HI/WIDE constant forms read 64-bit addends (the md's 32 is an md error, `src/sass/md/fixes.py`). Wide views containing a hardwired zero slot are also explicit fallbacks, except views starting at RZ/URZ, which retain all-zero semantics.

## Exact and readable floating point (2026-10-08)
- Two languages per architecture: `smXX` is exact (hardware results), `smXX_readable` is the plain formula. `Gen(arch, fp)`, `sass.sleigh.gen.variant(arch, fp)`, `--fp=` on `sass-gen-sleigh`/`sass-build-languages` (builds both by default). The ELF opinion lists only the exact languages. Decode, display and dataflow are identical, and every Ghidra test takes the lowering (`pytest -m ghidra --fp readable|all`, `uv run python -m tests.semantics ARCH readable`, `sass-compare-ghidra --fp readable`).
- `src/sass/sleigh/lower.py` (`Lower.float_op`) contains both lowerings; `sass.sleigh.gen.MODES` names them. The readable lowering uses native-width `f+`/`f*` and ignores rnd, FTZ/FMZ, fusion and FMNMX NaN/±0 rules (including .NAN). SAT and abs/neg remain. Its reference is the unfused RN softfloat formula, compared bit for bit.
- Exact directed rounding (fp32 via fp64, fp64 via binary128) uses s = RN_wide, a TwoSum residual (zero for products, which are exact in the wide format), RN narrowing, then ±1 on the encoding by residual sign. RM zero-sign fix: an exact cancellation is -0 unless both addends are +0. This also cancels the emulator's minimum-subnormal underflow error. SM89 GPU fixtures cover .RZ/.RM/.RP for FADD/FMUL/FFMA/DADD/DMUL/DFMA. Ghidra 12.1.4 binary128 `f<` orders NaN operands, so the code guards with `nan()`.

## SM107 (2026-10-14)
SM107 is its own ISA, not an alias: the SM103 md plus SM120's CONSTANTS and IMNMX shape (Pu/Pv/Pq, `FMT_flo`), minus E0M3/E3M4, plus UE5M3 and 56 classes no other architecture has. No SM107 GPU: nothing here is hardware-verified. The evidence is ptxas 13.4.92 (`nvidia-cuda-nvcc` 13.4 wheel; 13.3 has no sm_107) lowering PTX for `sm_107a`, the PTX ISA 9.4 text (docs.nvidia.com, §9.7.5 mixed precision, §9.7.10.24 `cvt`), and CUDA 13.4's `cuda_fp8.hpp` host conversions. ptxas encodes its mnemonic strings, so a `strings` search for opcodes finds nothing: probe it with PTX instead. The probes: `research/sm107/` (`probe.py PTXAS candidates.txt`, output in `results.txt`).
- Packed binary32 pairs, one family for FADD2/FMUL2/FFMA2 (SM100+, previously opaque) and FHADD2/FHFMA2/FHMUL2 (`floating.py`, `pair_lanes`). ptxas lowerings of f32x2 PTX fixed the source lanes: a pair is (lane 0 = Rn, lane 1 = Rn+1); `.LO_HI` swaps; `.F32` and immediates (32 encoded bits, `F32Imm(64)` notwithstanding) broadcast; `|x|` applies to both lanes; lane 0 is negated by the `-` bit and lane 1 by `-` XOR NEGA `.NP`, after the swizzle (`-R.NP` = negate lane 0, `R.NP` = lane 1, `-R` = both; also for uniform registers). `.NP` on a broadcast has no ptxas form (it copies the register instead) and stays opaque; `.FMZ` stays opaque.
- PTX 9.4 mixed precision (sm_107f) lowers 1:1: `add/sub{.rn,.rz,.rm,.rp}.f32x2.{f16x2,bf16x2}.f32x2` -> FHADD2 `a16b` (`d[0:31] = convert(a[0:15]) + c[0:31]`), `fma.rnd.f32x2.{f16x2,bf16x2}.f32x2.f32x2` -> FHFMA2 (one fused rounding), `add/sub.rz.ftz.f16x2.f32x2.f32x2` / `.rz.bf16x2` -> FHADD2 `d16b`, `mul.rz.ftz.f16x2.f32x2.f32x2` / `.rz.bf16x2` -> FHMUL2 (`d[0:15] = convert(a[0:31] op c[0:31])`, RZ output). ptxas requires `.ftz` for the F16x2 result and the md forbids it for BF16x2; PTX's `.ftz` for half types is "flushes subnormal inputs and results to sign-preserving zero" (stated for neg/min/max, not restated in §9.7.5), modelled as flushing the binary32 inputs and the F16 result. RZ to binary32 then RZ to the half format is RZ once. NaN results follow F2FP's F16/BF16 packs (inferred).
- HMUL2 on SM107 names all three formats in one operand (`Dfmt.Afmt.Bfmt`, `F16x2.F16x2.F16x2` = `F16_V2`); PTX `mul.f16x2.f16x2.bf16x2` / `mul.bf16x2.bf16x2.f16x2 d, a, c` lower to the mixed forms (no rounding modifier or `.ftz` accepted) with `d = a * convert(c)`: c rounded (RN) to d's format, then the product rounded RN. BF16_V2 (SM86+) and the mixed forms are native without SAT/FTZ/FMZ.
- VISET (`integer.py`): PTX `set.<cmp>.u32.{s32,u32}` (defined as 0xffffffff/0) lowers to a bare `VISET.BM.<cmp>.{S32,U32}` whose result is stored as is, so `.BM` is an all-ones/zero mask; `vset2/vset4` lower to per-lane `.S32` VISETs followed by `& 1`. ptxas never emits the packed 16x2/8x4 formats; they apply the same rule per lane.
- F2FP scaled downconverts (`scaled_pair`): ptxas puts PTX cvt's scale-factor in Rc's high half: `.scaled::n2::ue8m0` (S2_6) as `IMAD.U32 Rc, s, 0x10000` (A's scale Rc[31:24], B's Rc[23:16]; the unscaled S2_6 form passes 0x7f7f0000), and SM107's `.scaled::n1::ue8m0` (`SCALE_BY_C`) as `IMAD.SHL.U32 Rc, s, 0x1000000` (Rc[31:24] for both). The S2_6 family read Rc[15:8]/Rc[7:0] before 2026-10-14. Rd[31:16] = Rc[15:0], MERGE_C's half (the unscaled forms' default H0), is inferred: ptxas always passes zero there. The SM107 forms round RN/RZ to E4M3/E5M2/E2M3/E3M2/UE5M3 like the unscaled downconvert.
- UE5M3 (PTX 9.4 `.ue5m3x2`; ptxas `F2FP.UE5M3.F32.PACK_AB_MERGE_C`): `cuda_fp8.hpp`'s host conversion drops the sign first, bias 15, largest finite 0xfe, NaN 0xff, no infinity; overflow gives 0xfe under SATFINITE, RZ or RM, else 0xff; Inf 0xfe only under SATFINITE. Native in the plain and scaled downconverts; the UE5M3 upconverts (`F2FP.F16.UE5M3.UNPACK_B`, scaled to BF16) stay opaque.
- `.PZO` (PTX cvt: "if the result of the conversion produces -0.0, it will be forced to +0.0") is native in the 8-bit downconverts; for S2_6 it changes nothing (no -0).
- Opaque with md dataflow, as their analogues everywhere: USYNCS, SYNCSU, SYNCS phasechk_pv, UCCTL, UBLKL2CCTL, UTMAL2CCTL, UTMASTG/UTMAREDG URc, UTRACEEVENT, LDTM sparsify, `__HIR0X1C0/1D0`; IMNMX's Pu/Pv/Pq forms (SM107, SM120); the E8 (UE8M0) downconvert destination.

## Generator reference

The generator preserves register spans and predicates for every class. Value semantics
add executable scalar values and native overrides or named runtime primitives
for the semantics families in `src/sass/semantics/families/` (routed by `src/sass/semantics/operations.py`). The test harness
can load cubin text and run independent threads as whole kernels. The general
Ghidra loader, warp scheduler, and concurrent memory model remain separate work.

`src/sass/semantics/operands.py` builds operand values independently of assembly display.
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

### Coverage

[coverage.json](coverage.json) is a compact, generated ledger for all ten
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
GPU comparisons currently cover the 144 whole-kernel SM89 fixtures (18,432 output comparisons) recorded in
[verification.json](verification.json), rather than every class and selector.

IMAD.HI adds a 64-bit addend to the full product before selecting the high
word. A 32-bit non-pair addend is added to the selected high word. IMAD.WIDE
writes the whole sum. The CUDA `mad.hi` lowering places its scalar addend in
the upper register of a pair, and the whole-kernel oracle executes that
compiler lowering directly. An SM89 probe also verifies the 64-bit constant
source path; narrower constant forms follow CuLifter's source-width handling
and pass the SM75 emulator fixture.

Native overrides cover ordinary MOV, IABS, SEL, IMAD low/high/wide, IADD3,
LOP3, SHF, LEA low/high/extended, IMNMX, ISETP including `.EX`, S2R/CS2R, basic memory operations,
and supported BRA/EXIT/CALL/RET forms. Common supported restrictions include:

- ISETP `.EX` compares the high words and uses `Pr` for the low-word result
  when those words are equal, then applies the selected AND/OR/XOR combiner
  with `Pp` to produce `Pu`/`Pv`. Direct register, uniform, immediate, and
  ordinary constant-bank forms are native; CX and SM120 ISETP64 forms remain
  opaque. SM75's shared `EX`/`noex` selector is handled separately at decode
  time so ordinary ISETP does not read `Pr` or compare high words.
- SM120's extra IMNMX predicate operands/results remain opaque.
- Wide views containing the hardwired zero slot, except views starting at the
  zero register itself, remain opaque. This avoids writing ordinary backing
  storage for a zero-register component.
- Indexed-register, descriptor, extended/uniform memory, unsupported address
  modes, and 256-bit memory variants retain opaque calls.
- CALL handles NOINC forms and RET handles absolute NODEC forms. Branch-stack
  changes and reconvergence-sensitive variants require additional state.

Mixed ordinary/uniform register forms of IMAD, IADD3, ISETP and LEA use the
same native calculations. UIADD3, UIMAD, USHF, ULEA, UISETP, ULOP3, USEL and
UMOV reuse those emitters with uniform register and predicate names. Ordinary
32-bit IADD and VIADD are native; packed/saturating VIADD and carry modes retain
opaque fallbacks. VIMNMX implements PT=min and !PT=max for S/U32, S/U16x2,
and SM120's S/U8x4 formats with register, uniform-register, immediate, and
SM75-SM90 CX B sources. Signed RELU forms clamp each selected lane at zero;
unsigned RELU encodings are invalid according to the machine description.
Non-PT predicate outputs remain opaque. CuLifter's per-lane behavior has
independent exact/readable emulator fixtures on SM100 and SM103, and VIMNMX
also on SM120; this path has no GPU verification because these instructions
are newer than the available SM89 hardware. Uniform wide views follow the
same zero-slot restrictions.
VIADDMNMX adds B modulo each selected lane width, then applies the same
predicate-selected min/max against C. It supports S/U32 and S/U16x2 with R,
uniform-register, and immediate B/C forms on SM100 and SM103. The encoded B
negation is applied before lane extraction, matching CuLifter's operand
materialization. Signed RELU forms clamp each result lane at zero; unsigned
RELU encodings are invalid according to the machine description. Independent
exact/readable emulator fixtures cover lane overflow, signed comparisons,
predicate polarity, B negation, RELU, and all source shapes. No GPU
verification is available for these newer instructions.
Native IADD3.X adds both predicate inputs to the complemented 32-bit operands.
Ordinary negation retains its 33-bit +1, including the carry from negating zero.
IADD3 predicate outputs indicate a sum at least 2^32 and 2^33; the first output
wins if both destinations name the same predicate. LEA computes the shifted
low/high word before applying negate/complement and adds its predicate for .X.
IMAD.X adds one predicate; HI/WIDE carry indicates unsigned overflow of the
full 64-bit product and pair addend, including signed product bit patterns.
For SM75/SM80 HI classes with a 32-bit C addend, the predicate is added to the
extracted product high word and C before the 32-bit result and carry-out are
computed. Exact/readable fixtures cover both carry-in values at the wrap edge.
These operations capture input predicates before writing overlapping outputs.
IADD3/LEA encodings modifying both Ra and Rb retain opaque fallbacks. Discarded
carry outputs preserve compact arithmetic. Uniform aliases share these emitters.

PRMT emits native byte selection for IDX (including sign replication), F4E,
B4E, RC8, RC16, ECL and ECR, in SASS source order: data A, selector, data B.

BMSK emits native 32-bit mask generation from position and width. Instruction
bit 75 selects clamp (0, `BMSK`) or wrap (1, `BMSK.W`). Wrap masks both counts
with 31; the resulting mask still ends at bit 31. Clamp returns zero for a
position at least 32 and saturates large widths at the end of the word. Zero
width returns zero in both modes. Register, uniform-register, immediate and
ordinary constant-bank inputs are supported. The `cx[URx][offset]` form also
has native P-code on SM75-SM90. Its 64-bit uniform-register pair contains a
bounded constant-buffer header, and its offset is in bytes. SM75-SM89 decode
the low 45 bits as base/16 and the high 19 bits as size/16; SM90 decodes the
low 51 bits as base/64 and the high 13 bits as size/16. The load uses `ram`
at the decoded GPU virtual address, returning zero outside the region.
The layouts come from Mesa's `nvk_bindless_cbuf` definitions; SM89 hardware
validates addressing and bounds. SM90's layout and zero-on-out-of-bounds
behavior are emulator-tested, with hardware validation pending. The shared
provider materializes CX values for supported scalar and packed emitters on
SM75-SM90. Direct GPU evidence remains BMSK-only; other consumers need
opcode-specific probes before their hardware behavior is established.
Dedicated SM89 whole-kernel GPU/reference/p-code comparisons cover
1,504 inputs per mode; emulator tests cover both languages on all architectures.
The BMSK CX GPU fixtures (`tests/cases/bmsk.py`) supply a separate manually patched SASS oracle
for CX counts, region sizes and offsets: 8,192 GPU/reference/P-code comparisons
pass in each language variant. Another 446 focused CX cases pass in each of
the ten SM75-SM90 languages. The oracle does not claim PTX generated CX.

FSEL selects the modified floating input, with optional .FTZ. FMNMX implements
ordinary min/max, including .FTZ: a numeric input wins over NaN, two NaNs yield
`0x7fffffff`, and equal signed zeros yield -0 for min or +0 for max. With
.NAN, either NaN input produces canonical `0x7fffffff`; numeric inputs still
use the ordinary min/max and signed-zero rules. Predicate true selects min;
false selects max. `.XORSIGN` takes the XOR of the raw source sign bits before
the absolute source modifiers, selects min/max by magnitude, and applies the
combined sign to a non-NaN result; NaN results bypass the sign update.
FMNMX predicate-result `.IS_A` forms remain opaque. The SM89 GPU checks compare FSEL
and FMNMX bits exactly, including `.FTZ`, `.NAN`, `.FTZ.NAN`, and
`.XORSIGN`; FSEL.FTZ and other architectures have emulator coverage.

FMNMX3 is native on SM100/SM103 for P0–P6/PT with optional inversion, all
`FTZ`/`noftz` and `NAN`/`nonan` selectors, and RRR, RUR, and immediate-B
forms. PT selects three-way minimum; !PT selects maximum. Exact mode follows
the PTX three-input min/max rules: default min/max selects the numeric input
when paired with one NaN, `.NAN` returns canonical `0x7fffffff` if any input
is NaN, and equal signed zeros select -0 for min or +0 for max. `.FTZ` flushes
subnormal inputs to signed zero. Readable mode keeps CuLifter's ordered
comparison formula. Emulator fixtures cover both predicate directions and
inversion, all selector combinations, NaNs in each input position, signed
zeros, infinities, and subnormals. No SM100+ GPU is available, so this
implementation is emulator-verified against the PTX specification, not
hardware-measured.

UP2UR's immediate-mask `.B0` form is native on all nine architectures. It
starts from `URa` and ORs `UPi << i` for each selected bit `i` from 0 through
6; higher mask bits are ignored, matching CuLifter's handler. The generated
p-code specializes the immediate mask so it reads only the selected uniform
predicate registers. Register-mask UP2UR and non-B0 selectors remain opaque.
Exact/readable emulator fixtures cover empty, sparse, full, and
high-bit masks and a false guard. A GPU fixture (`tests/cases/predicates.py`) patches UP2UR into
a compiled SM89 kernel wrapper; nine masks across 128 threads passed 1,152
GPU/reference/exact-p-code comparisons, including a nonzero URa base. The PTX
lowering probe did not emit UP2UR, so this hardware result comes from patched
SASS rather than a PTX lowering.

P2R and R2P support `.B0`–`.B3` with immediate, R, or UR masks on all nine
architectures. P2R replaces the selected byte of its base integer with the
packed selected `P0`–`P6` bits; R2P reads the selected source byte and updates
only the selected predicates, preserving the others. Mask bits above 6 are
ignored. Constant-bank/CX mask sources remain opaque because their `URb`
operands are address components, not plain masks. Exact and readable emulator
fixtures cover every selector and empty, sparse, full, high-bit, and register
masks. Focused dataflow checks pass for all 18 exact/readable architecture
languages, with 24/24 native cases per language. The patched SM89 whole-kernel
GPU probe passed 2,304 GPU/reference/exact-p-code comparisons across 18
selector/mask cases. Hardware evidence covers SM89 only.

### Exact and readable floating point

Every architecture has two languages. `SASS:LE:64:smXX` (exact) models hardware
results; `SASS:LE:64:smXX_readable` writes the plain formula for decompiler
reading. They decode and display identically, and have the same register
dataflow. Only floating-point value semantics differ. ELF opinions select the
exact language; choose the readable one explicitly. The SLEIGH backend
(`src/sass/sleigh/lower.py`) holds both lowerings. `uv run sass-build-languages --fp=exact|readable` limits a
build to one mode; both are built by default.

The readable lowering uses native-width `f+`/`f*` (FFMA is `a f* b f+ c`, one
operation per operator plus a copy). It treats every rounding mode as
round-to-nearest, and drops FTZ/FMZ flushing, fused single rounding, and the
FMNMX NaN/signed-zero rules (including .NAN). SAT, absolute and negate remain.
As a result, FADD/FMUL/FFMA/DADD/DMUL/DFMA with .RM/.RP/.RZ or .FMZ, HADD2/HFMA2
.FTZ/.FMZ, and FMNMX.NAN are native in this lowering. Its tests compare against
the unfused round-to-nearest formula, bit for bit.

FADD/FMUL/FFMA/FSETP are native exact p-code (`f+`, `f*`, `f<`, `nan`, ...) for
every rounding mode, .FTZ (denormal inputs/results flushed to signed zero),
.SAT and FMUL scale (computed in fp64, exact). Under round-to-nearest, FFMA
computes `a*b` exactly in fp64 and narrows the fp64 sum. This deliberate double
rounding can differ from hardware in rare midpoint cases.

Directed rounding (.RM/.RP/.RZ) computes the result in the doubled width: fp64
for fp32, binary128 for fp64. It keeps the exact residual: a TwoSum error for
sums, zero for the exact products. It narrows with round-to-nearest, then moves
the encoding by one ulp when the residual's sign calls for it. Under RM, an
exact cancellation gives -0 unless both addends are +0. The corrected result is
exact, and it also undoes Ghidra's underflow error described below.
`sass_prim_*` remains the fallback for .FMZ and unknown selectors.

Ghidra 12.1.4's emulator flushes results strictly between half and one minimum
subnormal to zero (`FloatFormat.getEncoding`, the `n < 0` branch). The p-code
itself is IEEE, so the round-to-nearest tests exclude that band. Its binary128
comparisons also order NaN operands, so the directed-rounding code tests
`nan()` explicitly.

DADD/DMUL/DFMA are native for round-to-nearest, with 64-bit register pairs,
uniform inputs, high-word immediates, constant-bank loads, and source absolute/negate
modifiers. DFMA uses a 16-byte binary128 intermediate: the product is exact,
but under round-to-nearest the wide sum followed by narrowing can double-round
in rare midpoint cases. Directed rounding uses the exact residual correction
above. DSETP shares the ordered/unordered FP32
comparison logic and combines complementary results with AND/OR/XOR; MIN/MAX
selectors retain opaque fallbacks. Dynamic constant operands still require an
explicit operand provider. The same Ghidra underflow limitation applies to FP64.

HADD2/HFMA2/HMUL2 implement packed FP16 under RN, including per-lane absolute/negate,
H1_H0/H0_H0/H1_H1 selection, saturation, register/uniform inputs, separate lane
immediates, and constant-bank values. HFMA2 uses binary128 for an exact finite
half product and sum before a single rounding to half; HADD2 widens to fp64.
Both results are captured before writing an overlapping destination. An
unmodified H0_H0 source reads only its low two bytes in optimized p-code.
HADD2.F32 is also native on SM86/89/90/100/101/103/120: it adds the selected
low half lanes from `-RZ` and the second operand, then widens that half result
to F32. The second-source H1_H0/H0_H0/H1_H1 selectors, absolute/negate,
FTZ/no-FTZ, SAT/no-SAT, and available register, uniform, immediate, and
constant-bank forms are covered. SM89 GPU fixtures confirm the register form
for all 48 selector/modifier combinations (128 inputs each); RC forms are
emulator-tested. SM90 CX is emulator-tested; other CX forms use the shared
bounded-load provider where their MD classes exist.
HMUL2 multiplies each selected pair after widening to fp32, which holds the
exact finite binary16 product before one narrowing conversion. F16_V2 supports
FMZ, FTZ, and no-flush modes, with SAT and no-SAT variants. FTZ flushes
subnormal inputs and results while preserving their signs. On SM89, FMZ
produces canonical +0 when either selected input has an all-zero exponent,
including zero-times-infinity; this case is checked against the GPU. SAT clamps
the result to [0, 1]. Register, uniform, immediate, and
constant-bank sources are covered where the machine description provides them.
BF16/E6M9 formats, FP32-input packed arithmetic, H0_NH1, and RELU remain opaque.
The minimum-subnormal Ghidra emulator limitation also applies to FP16.

The `HFMA2.MMA Rd, -RZ, RZ, imm_hi, imm_lo` form is a native constant move
under nofmz/nosat. Hardware shows it preserves finite bits, signed zero,
infinities, and denormals, while canonicalizing each NaN half to `0x7fff`.
This transform is evaluated from encoded immediates at decode time, so the
move emits one COPY. Other MMA forms retain opaque fallbacks.

HMNMX2 is native for materialized F16_V2, BF16_V2, E8M7_V2, and E6M9_V2
operands: register, uniform, immediate, and ordinary constant-bank forms
where present in the MD. E8M7_V2 aliases BF16_V2's encoded format. `Pp`
selects min when true and max when false. Per-lane H1_H0/H0_H0/H1_H1 selection
and source absolute/negate modifiers are honored. F16 `.FTZ` flushes inputs
and results to signed zero; BF16/E8M7 and E6M9 do not expose FTZ. `.NAN`
canonicalizes NaN results to `0x7fff`. `.XORSIGN` compares magnitudes and
applies the XOR of raw source signs. RRR/RUR/RCR require both absolute flags;
RIR has only the register source's absolute flag, and its immediate keeps its
sign. The SM89 whole-kernel oracle covers 19 HMNMX2 fixtures with 128 vectors
each; exact/readable semantic matrices cover SM75-SM120. CX sources use the
shared bounded-load provider on SM75-SM90. SM90+ `IS_A` predicate-output
classes use native P-code for supported source forms with `nan=nonan`; the
`.NAN` selector stays opaque until its interaction with source flags is known.
`Pu` and `Pv` report whether source A supplied the low and high lane results,
respectively, based on NVIDIA patent US 12,554,493 and the `IS_A` selector.
Exact/readable emulator and dataflow checks cover 45 cases on SM90 and 27 on
SM100, SM101, SM103, and SM120. This interpretation is not GPU-verified: the
available SM89 GPU cannot execute the SM90+ form, and the patent does not
independently establish every tie/NaN detail. The default two-NaN path's A
flag is modeled but has not been measured on hardware.

FSET, HSET2 and HSETP2 (2026-10-14, ported from CuLifter's handler list) reuse
FSETP's comparison (`float_test`) and its `cond bop Pp` combiner. FSET writes
1.0f or +0 (`.BF` is its only md form). HSET2 compares each lane and writes
1.0 in the lane format (`.BF`: 0x3c00, SM90+ BF16_V2 0x3f80) or 0xffff (`.BM`),
else 0. HSETP2 writes lane 0's result to Pu and lane 1's to Pv; with `.H_AND`
it ANDs the lanes and writes `both bop Pp` and `!both bop Pp`, like FSETP's
Pu/Pv. Lanes are widened exactly to binary32 before comparing, after F16 FTZ
flushing; the md forbids FTZ with BF16. Every non-ALTERNATE class is native on
SM75-SM120 (register, uniform, immediate, constant and SM75-SM90 CX sources).
The `fset_simple`, `*_noBop` and SM100+ `*_fixed` classes are ALTERNATE.

Runtime primitives cover the float fallbacks, F2I/I2F, PRMT, SHFL/VOTE,
QSPC, and synchronization events. QSPC requires `context["address_spaces"]`,
a mapping from `global`, `local`, and `shared` to half-open address intervals;
SM90+ also accepts `shared_cluster`. `S` queries CTA shared memory, while
SM90+'s `D` queries cluster-visible shared memory, corresponding to PTX
`isspacep.shared::cluster`. `Pu` reports whether the address belongs to the
queried space. `Rd` reports a one-hot input-space tag: global `1`, shared `2`,
local `4`, based on SM89 GPU probes. `Rd` with D is kept opaque until checked
on SM90 hardware. The emulator cannot infer generic-pointer provenance from
an integer address; missing mappings, unmapped addresses, or overlapping
ranges raise `UnsupportedSemantics`. Floating arithmetic uses exact rational values
with one IEEE rounding step; FTZ, saturation, and directed rounding are explicit.
Conversion results for NaN, infinity, or out-of-range inputs and FMZ/NTZ modes
whose conventions are unconfirmed raise `UnsupportedSemantics`. NaN results
are canonical quiet NaNs; GPU payload selection is not verified. PRMT implements
IDX and the six specialized modes.

F2IP has a native path for packed F32-to-S8/U8 conversion when the instruction
uses round-to-nearest-even, NTZ, no RELU, and materialized 32-bit inputs,
including CX loads on SM75-SM90. The SM89 CX probe covers 16 S8/U8 forms
and passes 2,048 GPU/reference and exact/readable P-code comparisons per mode.
It rounds and saturates each input independently; NaNs become zero and infinities
saturate by sign. The first input occupies bits 8–15, the second bits 0–7, and
the low 16 bits of C occupy bits 16–31. Other destination/source formats,
rounding selectors and modifiers retain opaque fallbacks. The SM89 GPU oracle
verifies the packed byte order and edge behavior for directly materialized
sources.

F2FP has guarded packed F32-to-F16 and F32-to-BF16 `PACK_AB` paths for RN and
RZ, including RELU, SATFINITE, and their combination, with R/UR, immediate,
and ordinary constant-bank sources. F32-to-TF32 `PACK_B` supports RN/RZ and
RELU with R/UR, immediate, and ordinary constant-bank sources.
The first source converts into bits 31–16 and the second into bits 15–0.
F16 conversion uses integer rounding, including subnormals and overflow; BF16
uses its F32 high half and ties-to-even, with NaNs canonicalized to `0x7fff`.
TF32 retains ten fraction bits and rounds the discarded 13 according to RN/RZ.
F32-to-E6M9 `PACK_AB` is native for RN/RZ with RELU, SATFINITE, and their
combinations on SM86/SM89/SM90, where the MD exposes that destination selector.
E6M9 uses one sign bit, six exponent bits with bias 31, and nine fraction bits.
RN uses ties-to-even; RZ truncates overflow to max finite; SATFINITE clamps
infinity and overflow to signed max finite; RELU maps negative outputs, including
negative zero, to positive zero and leaves NaNs canonical at `0x7fff`. Exact
and readable emulator suites pass on all three architectures, including
R/UR/immediate/direct-C sources where present. A patched SM89 GPU probe covered
RRR, RIR immediate, and RCR constant-bank forms: 13,824 packed-lane comparisons
across eight selector combinations (RN/RZ, RELU, SATFINITE) matched an
exact-rational reference, and 1,536 whole-kernel lanes matched P-code. The
probe includes 576 lanes per selector/source form and checks underflow and
overflow boundaries, random values, infinities, NaNs, and signed zero.
`MERGE_C` is native for F32-to-F16/BF16/E6M9 `PACK_AB` classes, with RN/RZ,
RELU, SATFINITE, and their combinations. The converted value occupies the low
16 bits; H0/H1 selects the low/high half of the merge source, which is copied
into the result's high 16 bits. R/UR, immediate, ordinary constant-bank, and
SM75-SM90 CX forms are supported where present. SM89 direct GPU
probes established H0/H1 placement for F16 and confirmed H0 behavior for BF16
and E6M9; current exact
and readable semantic suites exercise the supported ordinary source forms on
all nine architectures. The available RTX 4070 Laptop GPU is reachable but
SM89, so it cannot execute the newer SM100 selector forms.
F32-to-E4M3/E5M2 `PACK_AB_MERGE_C` is native for RN, SATFINITE, no RELU, and
R/UR, immediate, and ordinary constant-bank inputs, with H0/H1 selection of the carried C half. E5M2 infinity
clamps to max finite; NaNs use `0x7f` with the source sign. `F16.E4M3/E5M2
UNPACK_B` is native for RN with optional RELU and R/UR, immediate, and ordinary constant-bank sources, with
H0/H1 selection of the source half. RELU maps negative results, including
negative zero, to positive zero per lane; NaNs retain canonical positive
`0x7fff`. Finite E4M3 values are normalized exactly;
E5M2 finite values and infinities widen by shifting the encoding. Both formats
canonicalize every NaN to positive `0x7fff`, as established by exhaustive
SM89 byte probes. SM100 `E2M1.UNPACK_B` now widens the two nibbles in a
selected source byte to F16 for RIR/RRR/RUR sources, with RN and optional
RELU. B0–B3 select the byte; all 256 byte values pass exact/readable emulator
fixtures on SM100/SM101/SM103/SM120. The independent reference follows PTX's
documented E2M1-to-F16x2 packing order. E0M3 uses its sign bit and 3-bit
integer magnitude (−7…7, including signed zero). All 256 byte values pass
exact/readable emulator fixtures and RIR/RRR/RUR
dataflow checks on SM100/SM101/SM103/SM120. E0M3 hardware validation remains
pending because the available GPU is SM89. E3M4 also widens to F16 for
RIR/RRR/RUR sources with RN, UNPACK_B, and optional RELU. An [independent
E3M4 format reference](https://github.com/tjthereal/fp8/blob/main/READMEFP8.md)
describes sign, 3-bit exponent (bias 3), 4-bit fraction, subnormals, and the
reserved NaN encoding. Exhaustive fixtures check all 256
source bytes in both selected halves and both RELU settings: 3,072 cases per
mode on each of SM100/SM101/SM103/SM120. Dataflow passes for all three source
classes in both modes. These E3M4 results are emulator-verified from format
evidence, not GPU-measured. The `f2fp_rs` stochastic-rounding family, FP4
scaling, and other scaled conversions remain opaque.
F16/BF16 and F32 downconversion to E4M3/E5M2 is native for RN, SATFINITE,
optional RELU, and H0/H1 merge selection. The ordinary R/UR/immediate source
forms pass 11,040 exact and readable cases per mode on SM100/SM101/SM103 and
SM120; SM120's six uniform-register `UF2FP` forms are included in its 14,592
cases. Dataflow passes for all ten ordinary forms per mode on SM100/SM101/SM103
and all sixteen ordinary plus uniform forms on SM120. NaN sign and payload are
compared by class because F16 widening may canonicalize them. These emulator
results do not establish hardware behavior on SM100+; other 8-bit destination
formats and rounding/saturation selectors, scaled S2_6 downconversion, and
stochastic rounding remain opaque.
F16/BF16-to-E2M1/E0M3 downconvert is native for the SM100/SM101/SM103/SM120
`UNPACK_B_MERGE_C` classes in RIR/RRI/RRR/RRU/RUR forms. The B input produces
the low output byte, C produces the next byte, and Rd receives the packed
16-bit result. E2M1 follows CUDA's `cuda_fp4.hpp` RN-even conversion, signed
maxnorm saturation, and NaN-to-positive-maxnorm rules. E0M3 is interpreted as
sign plus a 3-bit integer magnitude (−7…7, with signed zero): finite inputs
round to nearest-even magnitude and saturate at 7; NaNs canonicalize to
positive 7 before RELU. The E2M1 matrix passes 1,560 exact/readable fixtures per
architecture; the expanded E2M1/E0M3 matrix passes 4,320 exact and readable
fixtures per architecture, covering RIR/RRI/RRR/RRU/RUR, F16/BF16, threshold
neighbors, edge encodings, RELU, and deterministic random inputs. No compatible
SM100+ GPU is available, so E0M3 hardware behavior remains unverified.
F32-to-E2M1/E0M3 conversion is native for the SM100/SM101/SM103/SM120 `PACK_AB_MERGE_C` classes in RIR/RRI/RRR/RRU/RUR forms. A and B produce the high and low nibbles of Rd[7:0], and C supplies the preserved upper 24 bits. E2M1 uses RN-even conversion with signed maxnorm saturation; E0M3 uses signed integer magnitudes 0–7 with RN-even rounding. Both support SATFINITE and optional RELU. The F32 FP4 matrix passes 1,440 exact and 1,440 readable cases per architecture, including source forms, threshold neighbors, special values, and deterministic random inputs; all five forms pass dataflow checks in each mode. PTX 9.0 compilation with CUDA 13.0.88 confirms the E2M1 lowering to `F2FP.SATFINITE.E2M1.F32.PACK_AB_MERGE_C`. The available GPU is SM89, so this class has no hardware execution validation; E0M3 is format-derived.
The scaled `f2fp_4b_upconvert_scale__` family is also native for RIR/RRI/RRR/RRU/RUR
on SM100/SM101/SM103/SM120. It selects one byte from the FP4 source, converts
its low/high nibbles to BF16, then multiplies each lane by the corresponding
low/high UE8M0 byte in the second source. The E2M1 rule follows the PTX ISA;
E0M3 uses its sign-plus-magnitude integer grid with the same per-lane scaling
operation. RN, SATFINITE, and optional RELU are implemented. All 256 scale
bytes, FP4 codes, B0–B3 selectors, both formats, RELU settings, and source
forms pass 20,480 exact/readable fixtures per architecture, with focused
dataflow checks for both formats and all five forms. No compatible SM100+ GPU
is available, so this family has emulator but no hardware validation.
The scaled `f2fp_8b_upconvert_scale__` family is native for RIR/RRI/RRR/RRU/RUR
on SM100/SM101/SM103/SM120. E5M2, E4M3, E2M3, E3M2, E3M4, and signed S2F6
bytes widen to BF16, then multiply by the matching UE8M0 byte. H0/H1 selects
the source pair and matching scale pair; RN, optional RELU, and optional
SATFINITE are implemented. The rules for E5M2/E4M3/E2M3/E3M2/S2F6 follow the
[PTX conversion specification](https://docs.nvidia.com/cuda/parallel-thread-execution/#data-movement-and-conversion-instructions-cvt),
including S2F6's signed 8-bit value times 2^-6 and the PTX-specified conversion-
then-scale order. E3M4 uses the independent format reference described above.
Each of the six formats, five source forms, H0/H1, RELU settings, and
SATFINITE settings passes 61,440 exact/readable emulator cases per architecture.
Each lane sweeps all 256 source-byte and all 256 scale-byte values; the matrix
does not enumerate every source/scale cross-product. Dataflow/control-flow
checks pass 5/5 classes per mode on all four architectures. The available GPU
is SM89 and cannot execute SM100 SASS. The PTX 9.2 compile probe is recorded at
`tests/ptx/f2fp/f8_scaled_upconvert.ptx`; installed CUDA 13.0 ptxas supports
PTX 9.0 and rejects the PTX 9.2 version and scaled modifier, so it supplies no
compiler-lowering evidence.
SM100 UE8M0 `UNPACK_B` to BF16 is native for RIR/RRR/RUR with H0/H1 selection.
All 256 source bytes pass exact/readable emulator fixtures in both lanes on
SM100/SM101/SM103/SM120. Its reference follows CUDA's host UE8M0-to-BF16
conversion, including `0x00` to the `2^-127` BF16 subnormal and `0xff` to
canonical `0x7fff`. No compatible GPU was available for SM100 hardware
validation.
SM100/SM101/SM103/SM120 `F16.E2M3/E3M2.UNPACK_B` now widens both padded 6-bit
lanes to F16 for RIR/RRR/RUR sources, with RN and optional RELU. The fixture
reference matches CUDA's host FP6 conversion implementation for all 64
encodings per format. The SM100 full suites include 4,608 exact and readable
cases; the targeted SM101/SM103/SM120 matrix has 1,536 cases per mode and arch.
Hardware validation is pending because no compatible GPU is available. FTZ/FMZ are not selector operands in the
inspected SM89 F2FP MD classes. Immediate and ordinary constant-bank sources
pass exact/readable emulator fixtures; SM89 GPU probes exercise F16 PACK_AB
immediate and ordinary constant-bank sources (128 vectors each); other source
combinations have emulator coverage only. Exact/readable
semantic suites and dataflow suites pass for all nine
architectures. The SM89 GPU oracle confirms H1 C merging in the in-place
`Rd=Rc` form for both E4M3 and E5M2 against independent references and P-code
(128 whole-kernel vectors per format). Distinct-destination merge results and
H0 hardware behavior remain unverified. The same oracle confirms the four F16
and BF16 RN/RZ forms plus RELU, SATFINITE, and combined RELU/SATFINITE for both
formats (128 whole-kernel vectors per form). ptxas emits
`F2FP.TF32.F32.PACK_B` for the RN PTX probe on SM90, but the available SM89 GPU
cannot execute that instruction; TF32 hardware validation remains pending.
The four FP8 unpack probes compare both H0 and H1 conversions for E4M3 and
E5M2 across all 256 source-byte encodings (128 whole-kernel vectors per form).
Four additional RELU probes patch only the RELU selector bit and compare GPU,
P-code, and an independent per-lane reference for 128 vectors per form; these
cover every source byte in both extracts and both formats.

I2IP has a native path for S32 inputs packed to signed or unsigned 2-, 4-,
8-, and 16-bit lanes. SAT clamps each input to its destination range;
SATRELU clamps signed inputs to zero through the signed maximum. The no-SAT
form keeps the low destination bits. For 2- and 4-bit packing, the supported
`.28`/`.24` extraction selectors place the two converted inputs in the high
and low lane, respectively; the remaining high bits come from C. The 8-bit
and 16-bit forms accept H0/H1 extraction. H1 selects C[31:16] for the
preserved upper half of an 8-bit result; C is unused for 16-bit results. The
SM89 GPU oracle compares all eight signed/unsigned widths, plus patched no-SAT
and SATRELU selector cases, against an independent reference and Ghidra
p-code. The SM89 CX probe covers 40 selector/source combinations and passes
5,120 GPU/reference and exact/readable P-code comparisons per mode. CX inputs
are materialized on SM75-SM90; unsupported selector combinations and source
shapes retain opaque fallbacks.

F2I has a native p-code path for the hardware-verified F32-to-S32
`.TRUNC.NTZ` form. It truncates finite values toward zero, maps NaNs to zero,
and clamps infinities and overflow to the signed 32-bit limits. The path is
generated for all architectures; GPU behavior has been measured on SM89.
Exact mode falls back to the runtime primitive for other rounding modes.
Readable mode ignores the encoded rounding selector and uses truncation, like
its other floating-point operations ignore rounding details. Other formats,
destinations, and NTZ selectors fall back to the runtime primitive. Absolute
clears the source sign bit before negate toggles it; patched
SM89 GPU fixtures verify absolute, negate, and combined forms against an
independent integer reference (128 whole-kernel comparisons per form).

S2R/CS2R copy named special registers; the launch environment seeds their values. SHFL/VOTE require explicit 32-lane
values, predicates, active mask, and lane index. Inactive shuffle sources
are rejected because their values are undefined. BAR/BSSY/BSYNC require an
explicit synchronization event context; recording an acknowledged event is
not a warp-scheduling implementation. Barrier reductions/results require a
handler. MUFU is a named primitive that requires a hardware-specific handler;
it does not substitute host approximations silently. CPU arithmetic references
use the [NVIDIA PTX arithmetic definitions](https://docs.nvidia.com/cuda/parallel-thread-execution/)
as reference conventions; GPU checks are needed to establish SASS equivalence.

### Runtime ABI

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

### Build and verification

```sh
export SASS_SLEIGH_OUT=/tmp/sass-semantics
uv run sass-build-languages --output /tmp/sass-semantics
# Finish compilation before parallel Ghidra tests. The build command writes
# compiled files atomically, checks the 16 MiB decompressed size limit, and
# rejects unused-temporary diagnostics.
uv run sass-coverage /tmp/sass-semantics --output processor/SASS/semantics/coverage.json
uv run pytest                                        # Python tests, incl. softfloat vs libm and lifted semantics
uv run pytest -m ghidra --arch all --fp all          # disassembly, dataflow, semantics, emulated GPU fixtures, coverage
uv run pytest -m gpu --require-gpu                   # references vs the SM89 GPU
uv run python -m tests.semantics SM89 readable --family f2fp                       # test_pcode_semantics outside pytest
```

`src/sass/ghidra_bridge/ghidra_emulate.py` runs decoded p-code through Ghidra's real byte executor
and a userop library. It checks actual register values, memory and control flow,
not a Python reimplementation of the instruction dispatcher. `GHIDRA_PY`
selects the pyghidra Python interpreter. In a restricted environment, set
`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` to writable scratch directories.

The CUDA Driver API oracle compiles 96 SM89 kernels and executes their actual
`.text` through Ghidra's normal instruction stepping, from the prologue through
EXIT. It compares 128 random/edge input vectors per kernel (four blocks of 32),
including NaNs, infinities, denormals, signed zero, and integer boundaries.
Target mnemonics must execute in the emulator (decoded with pydecode). Since 2026-10-14 every
fixture has an independent reference: `test_refs_on_gpu` checks it against the GPU and
`test_fixtures_emulated` checks the emulated kernel against it, so only the former needs a GPU.
The `isetp_ex` PTX 64-bit comparison compiles to a low-word ISETP followed by
`ISETP.GE.U32.AND.EX`; all 128 vectors matched the GPU. All 12,288 output
comparisons passed on the RTX 4070 Laptop GPU.

The carry GPU fixtures (`tests/cases/carry.py`) add 210 patched SM89 whole-kernel
variants (26,880 comparisons) for IADD3, LEA and IMAD carry chains, including
aliased IADD3 outputs, predicate inversion, negate/complement,
shift boundaries and signed/unsigned full-width overflow. Independent integer
references and actual Ghidra execution must both match the GPU. The probe checks
compiler slot/register assignments before replacing eight reserved BAR slots
and a placeholder arithmetic instruction; compiled addressing and EXIT remain.
Other architectures are checked in the emulator, without hardware evidence.

The cubin's PARAM_CBANK and KPARAM_INFO records supply parameter offsets and
sizes. For these SM89 kernels, bank 0 parameters begin at 0x160, with the two
64-bit pointers at 0x160 and 0x168. Actual constant sections are loaded into
`cbank`; launch dimensions and named TID/CTAID special registers are seeded.
A fresh emulator per thread isolates local memory. Shared-memory fixtures use
cells private to each thread, so no cross-thread communication is modeled.
Kernel PC bounds and an instruction budget reject fallthrough and infinite loops.

Coverage includes integer arithmetic, IMAD low/high/wide, LOP3, SHF, LEA,
predicates/selects, PRMT, HADD2/HFMA2 and the MMA constant move, DADD/DMUL/DFMA/DSETP, FADD/FMUL/FFMA, FSEL, FMNMX, FSETP, F2I.TRUNC.NTZ, I2FP, moves,
uniform/ordinary constant loads, global/shared/local loads and stores, and
predicated branching with BSSY/BSYNC. FADD.FTZ and FFMA.SAT have dedicated
fixtures. Directed rounding has eighteen: .RZ/.RM/.RP for each of
FADD/FMUL/FFMA/DADD/DMUL/DFMA. Each uses edge vectors whose residual lies below
the wide format's precision, exact cancellations, and overflow. FSEL and FMNMX min/max have dedicated fixtures, including FMNMX.FTZ
and mixed NaN/signed-zero/denormal pairs. FP16 has ordinary/saturating and
broadcast/modifier fixtures; MMA move constants include signed zero, negative
signaling/quiet NaNs, denormals, and infinities. Mode-specific fixtures replace
one arithmetic word in a compiled kernel to request SASS modes directly;
scheduling bits and compiled prologues remain intact. The manifest records
these patches.
SM89 F2I.TRUNC.NTZ to S32 maps NaN to zero and clamps infinities/overflow;
other NTZ combinations still reject until verified.

BREV and POPC are native 32-bit operations for register, uniform-register,
immediate, and ordinary constant-bank sources where present. FLO supports
32-bit sources with `.S32` and `.SH`/default mode: the default result is the
highest set-bit index, `.SH` returns the leading-zero shift, and zero returns
`0xffffffff` in both modes. Its predicate destination is supported only when
encoded as `PT`. SGXT supports `.S32`/`.C`, selects the byte indexed by the low
two bits of its first source, then sign-extends that byte to 32 bits. The
shared provider materializes CX operands on SM75-SM90 where those classes
exist; other FLO and SGXT selectors retain opaque fallbacks. The tests compare BREV against a per-bit reversal reference,
POPC against `bit_count`, FLO against the bit length/leading-zero definitions,
and SGXT against signed byte extraction.

IDP has native guarded semantics for `IDP.4A` and `IDP.2A.LO/.HI` R, URb,
ordinary constant-bank, and SM75-SM90 CX B forms. `.4A` multiplies four independently
signed or unsigned byte pairs; `.2A` multiplies two signed or unsigned 16-bit
A lanes by consecutive signed or unsigned bytes selected from B's low or high
half. Both accumulate into C modulo 2^32, including Rc negation. Independent reference fixtures cover all selector and
signedness combinations, overflow, Rc negation, and source/destination overlap
on all nine architectures in exact and readable languages. Eight SM89 PTX
`dp2a` kernels lower to the expected SASS selectors and pass 128 whole-kernel
GPU/P-code/reference comparisons apiece.

VIMNMX3 uses its Pp input to select three-way min for PT and max otherwise,
per lane, for S32/U32/S16x2/U16x2. Native support covers R, URb, and immediate
B forms on SM100 and SM103. Signed RELU forms clamp each result lane at zero;
unsigned RELU encodings are invalid according to the machine description.
Independent lane-wise fixtures pass in exact and readable mode. No hardware
oracle result is available because these instructions are newer than the
available SM89 hardware.

VABSDIFF4 supports S8/U8 register, uniform, immediate, ordinary constant-bank,
and SM75-SM90 CX operands for `SIMD_MRG` and `ACC`; non-PT Pu destinations
remain opaque. `SIMD_MRG` packs four absolute byte differences and ignores C.
`ACC` adds the four differences as a scalar, then adds C modulo 2^32. CuLifter's
CPU handler matches these SASS rules. A whole-kernel SM89 oracle patched one
LOP3 instruction per fixture to the target VABSDIFF4 encoding; 128 GPU,
emulator, and independent-reference comparisons passed for each fmt/reduction
pair. This is patched SASS evidence, not a claim that ptxas emits those words
for a PTX vabsdiff4 instruction.

Floating results are bit-exact except that two NaNs compare equal regardless
of payload/sign (independently for each packed half). MMA move constants are
compared bit-exactly, including NaN canonicalization. FTZ and saturation have no additional numeric tolerance.
MUFU approximations, warp collectives, device calls/returns, barrier reductions,
and cross-thread memory ordering remain outside this hardware validation.
The loop fixture explicitly acknowledges BSSY/BSYNC events; this verifies its
per-thread result without asserting a warp scheduler.

Use `NVCC` to select the toolkit. Without a GPU, `pytest -m gpu` skips; `--require-gpu` fails.
`uv run python -m tests.observe FAMILY` runs a family's fixtures and prints raw GPU outputs without asserting.
CUDA device access requires running outside the restricted sandbox here.

Recorded results for this implementation are in [verification.json](verification.json).
