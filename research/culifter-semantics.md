# CuLifter instruction-semantics comparison

CuLifter is included as the `third_party/culifter` submodule at commit
`bc5a9f0fc7de929bb6965d7fed28a4dde6c1ef4b` (`main`, inspected 2026-10-11).
Upstream describes it as a SASS-to-LLVM lifter for SM75 with experimental
support for newer targets. The source now also contains substantial SM90+
NVVM and CPU backend code. This comparison reads the registered `@lift_for`
handlers in `lift/lifter.py`, `lift/lifter_cpu.py`, and `lift/lifter_nvvm.py`,
then compares their opcode names with this checkout's `sass/operations.py` and
the generated architecture manifests in `processor/SASS/data/languages`.

## How to read the comparison

CuLifter's registered handler is a dispatch entry, not proof that every
modifier, operand form, or architecture is modeled. Some entries deliberately
raise `UnsupportedInstructionException`; others are identity operations,
zero-filled placeholders, or NVVM inline assembly that only has meaning when
compiled for a GPU. I count a feature as a candidate gap only where the source
contains a value, memory, predicate, or control-flow calculation. Even there,
coverage is usually restricted to specific encodings and contexts.

This repository represents some behavior as `native` p-code and some as
`primitive` calls. The generated coverage manifests distinguish those from
`opaque` classes, but a family can mix all three statuses and a native entry
can have selector guards. Thus “we have it” below means the relevant family or
behavior already has a non-opaque implementation somewhere; it does not mean
all its SASS forms are covered. A manifest entry for an opcode absent from
`OPERATION_FAMILIES` remains opaque here, even if an upstream handler has the
same spelling.

## Strongest new semantic candidates

| SASS family or group | CuLifter implementation | State in this repo | Assessment |
| --- | --- | --- | --- |
| `BREV`, `FLO`, `POPC`, `SGXT` | Bit reversal; find-leading-one/normalizing shift; population count; signed byte extraction/extension. `FLO` handles U32/U64 and `.SH`; `POPC` calls an LLVM intrinsic. | BREV and POPC were already native. FLO and SGXT now have guarded native 32-bit paths for ordinary and uniform registers, immediates, constant-bank inputs, and CX loads where present on SM75-SM90. Unsupported selectors remain opaque. | Scalar paths are ported in progress. FLO returns the highest set-bit index by default and the leading-zero count in `.SH`; signed 64-bit FLO and other selector combinations remain outside this change. SM89 PTX `bfind` compiler and GPU checks confirm both `FLO.U32` modes. |
| `VABSDIFF4` | Four packed signed/unsigned byte absolute differences; packed-byte result or accumulated scalar, with optional saturation. | Native guarded S8/U8 `SIMD_MRG` and `ACC` paths for materialized 32-bit sources with `Pu=PT`; CX values use the bounded-load provider on SM75-SM90 where present. | SM89 GPU tests establish that SASS `SIMD_MRG` packs four differences and ignores C, while `ACC` scalar-sums the differences then adds C modulo 2^32. 128 whole-kernel GPU/p-code/reference checks pass per fmt/reduction pair. The SM89 CX B probe passes 16 forms and 2,048 GPU/reference and exact/readable P-code comparisons per mode. Non-PT Pu outputs and unrepresented SAT encodings remain open. PTX `vabsdiff4` syntax uses a lane mask and lane-wise `.add` ([PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)); the compiler probe expanded those PTX forms rather than emitting this SASS opcode, so the PTX-level rules are not used as a direct SASS mapping. |
| `VIMNMX`, `VIMNMX3`, `VIADDMNMX` | Per-lane packed integer min/max, three-way min/max, and add followed by min/max; supports S/U 8-, 16-, and 32-bit forms and RELU in helper code. | Signed RELU paths are native for all three families, with lane-wise clamp to zero. Unsigned RELU selector combinations are invalid per the machine description and remain opaque. `VIMNMX` covers S32/U32/S16x2/U16x2 on SM100/SM103 and S8x4/U8x4 additionally on SM120; non-PT Pu/Pv outputs remain opaque; CX values use the bounded-load provider on SM75-SM90 where those classes exist. `VIMNMX3` covers S/U32 and S/U16x2 on SM100/SM103. `VIADDMNMX` covers S/U32 and S/U16x2 across five R/uniform/immediate classes on SM100/SM103. | Independent fixtures pass in exact/readable mode: VIMNMX 504 per SM100/SM103 and 756 on SM120; VIMNMX3 648 and VIADDMNMX 840 per SM100/SM103. All affected language pairs compile. The available SM89 GPU cannot execute these newer instructions, so there is no hardware verification. |
| `HADD2.F32`, `HMUL2`, `HMNMX2`, `HSETP2`, `HSET2` | Packed two-lane half arithmetic, min/max, comparisons and predicate/mask results. `HADD2.F32` adds selected low half lanes and widens the result to F32. | HADD2.F32 is native for supported selectors, FTZ/SAT and materialized sources; CX uses the bounded-load provider on SM75-SM90. HMUL2 is native for F16_V2 with no-flush, FTZ, or FMZ selectors and SAT/no-SAT across all architectures. HMNMX2 is native for materialized F16_V2, BF16_V2, E8M7_V2, and E6M9_V2 forms, including CX on SM75-SM90 and lane swizzles, abs/neg, F16 FTZ, NAN, and form-specific XORSIGN behavior. With one NaN and default NaN handling, XORSIGN applies the raw source-sign XOR to the selected non-NaN result; `.NAN` and two-NaN cases remain canonical. SM90+ `IS_A` classes have native Pu/Pv source-A indicators for `nan=nonan` forms, inferred from NVIDIA patent US 12,554,493; `.NAN` forms stay opaque until their source-flag interaction is known. HSETP2/HSET2 remain opaque. | Exact/readable emulator cases cover HMNMX2 across SM80-SM120. The SM89 whole-kernel oracle passes 18,432 comparisons across 144 kernels, including NaN/XORSIGN vectors for F16, BF16, and E6M9. Its 64-form CX selector matrix passes 8,192 GPU/reference and exact/readable P-code comparisons per mode. Exact/readable `IS_A` tests and dataflow checks pass 45 cases on SM90 and 27 each on SM100, SM101, SM103, and SM120; hardware verification remains open because the available GPU is SM89. HSETP2/HSET2 remain opaque. |
| `IDP` | `.4A`: four signed/unsigned byte products; `.2A.LO/.HI`: two signed/unsigned 16-bit A lanes multiplied by consecutive signed/unsigned bytes selected from B's low/high half. All accumulate into a 32-bit C value. | Native guarded `.4A` and `.2A.LO/.HI` for R, URb, ordinary constant-bank and SM75-SM90 CX B sources where present. | Independent emulator fixtures pass all nine architectures and cover signedness, lane selection, overflow, Rc negation, and overlapping registers. Eight SM89 `dp2a` PTX kernels lowered to the expected SASS forms and pass 128 whole-kernel GPU/P-code/reference comparisons each. The SM89 IDP.4A CX probe passes all four S8/U8 source-format combinations with 512 GPU/reference and exact/readable P-code comparisons per mode. |
| `P2R`, `R2P`, `UP2UR`, `B2R` | Pack/unpack predicate bits to/from integer or uniform registers; B2R obtains a barrier result. | P2R/R2P `.B0`–`.B3` support immediate, R, and UR masks on all nine architectures. UP2UR immediate-mask `.B0` is native for UP0–UP6 on all nine architectures. Constant/CX P2R/R2P masks, non-B0 UP2UR, register-mask UP2UR, and B2R remain opaque. | P2R replaces the selected byte of its base with selected P0–P6 bits; R2P reads that source byte into selected predicates and preserves unselected predicates. Both ignore mask bits above 6. Exact/readable semantic suites pass on all architectures. Focused P2R/R2P dataflow passes all 18 exact/readable language variants (24/24 each). `tests/predicate_transfer_semantics.py` passes 2,304 whole-kernel GPU/reference/exact-p-code comparisons on SM89 across 18 selector/mask cases. Constant-bank/CX masks stay opaque because URb is part of an address. UP2UR follows CuLifter's formula: preserve URa, OR each selected UPi into bit i for i=0..6, and ignore higher mask bits. `tests/up2ur_semantics.py` patches UP2UR into a compiled SM89 wrapper and passes 1,152 whole-kernel GPU/reference/exact-p-code comparisons across nine masks, including high mask bits and a nonzero base. The PTX lowering probe did not emit UP2UR. B2R still depends on synchronization state. |
| `F2F` / `F2FP`, `F2IP`, `I2IP`, `FRND` | Float-width conversion including packed F16/BF16/FP8 paths; packed float-to-int and int packing; float rounding. | F2FP supports F32-to-F16/BF16/E6M9 `PACK_AB` and `MERGE_C` RN/RZ with RELU and SATFINITE, including H0/H1 merge selection. F32-to-TF32 `PACK_B` RN/RZ with RELU, F32-to-E4M3/E5M2 `PACK_AB_MERGE_C`, and F16.E4M3/E5M2 `UNPACK_B` RN with optional RELU are also supported. R/UR, immediate, and ordinary constant-bank sources are handled where MD forms and operand materialization permit. CX values use the bounded-load provider on SM75-SM90 where the emitter supports the class; focused emulator fixtures cover F2FP, IDP, integer min/max, and packed half CX sources, with SM89 GPU validation for F16 `PACK_AB` CX (128 vectors) and the 48-form F16/BF16/E6M9 `MERGE_C` CX selector matrix (6,144 vectors per exact/readable P-code mode). SM100/SM101/SM103/SM120 E2M3/E3M2 and E3M4 `UNPACK_B` to F16, UE8M0 `UNPACK_B` to BF16, and E2M1/E0M3 `UNPACK_B` to F16 are native for RIR/RRR/RUR. F16/BF16-to-E2M1/E0M3 `UNPACK_B_MERGE_C` is native for RIR/RRI/RRR/RRU/RUR with RN-even, SATFINITE, and optional RELU; B and C supply the lower and upper output bytes. E0M3 is interpreted as sign plus a 3-bit integer magnitude (−7…7, with signed zero), rounds to nearest even, saturates at 7, and canonicalizes NaNs to positive 7 before RELU. E2M1/E0M3 upconvert covers B0–B3; E0M3 is interpreted as sign plus a 3-bit magnitude, with signed zero. The scaled upconvert family is native for E2M1/E0M3 on SM100/SM101/SM103/SM120, with B0–B3 byte selection and per-lane UE8M0 scaling into BF16. The scaled 8-bit family is native for E5M2/E4M3/E2M3/E3M2/E3M4/S2_6 to BF16 on those architectures, with H0/H1 paired source/scale selection, RN, optional RELU, and optional SATFINITE across RIR/RRI/RRR/RRU/RUR forms. E2M3/E3M2 and UE8M0 cover H0/H1. All supported source encodings pass exact/readable emulator fixtures. E5M2/E4M3 upconvert, other scaled formats, stochastic `f2fp_rs`, F32-to-E2M1/E0M3 `PACK_AB_MERGE_C` is native for RIR/RRI/RRR/RRU/RUR with RN, SATFINITE, and optional RELU. The F32 path passes 1,440 exact/readable cases and 5/5 dataflow checks per mode on each of SM100/SM101/SM103/SM120; PTX compilation confirms the E2M1 lowering, while GPU execution is unavailable on the SM89 device. Other format/merge combinations remain opaque. FTZ/FMZ are not selector operands in inspected SM89 F2FP classes. F2IP has a guarded native F32-to-S8/U8 path for materialized 32-bit inputs with RN-even/NTZ and no RELU, including CX on SM75-SM90. The SM89 F2IP CX probe passes 16 S8/U8 forms and 2,048 GPU/reference and exact/readable P-code comparisons per mode. I2IP has guarded native S32-to-S/U2, S/U4, S/U8, and S/U16 paths with supported extraction selectors H0/H1 and saturation modes, including CX inputs on SM75-SM90. H1 selects C[31:16] for the preserved high half of S8/U8 output; it has no effect for S16/U16. Other I2IP selectors/shapes and FRND forms remain opaque. | Exact/readable F2FP semantic suites pass on all nine architectures for the supported families, including the new F16/BF16/E6M9 `MERGE_C` selectors and ordinary source forms. The SM100 E2M3/E3M2 upconvert matrix passes 4,608 exact and 4,608 readable emulator cases; SM101/SM103/SM120 each pass 1,536 cases per mode. E3M4 upconvert passes 3,072 cases per mode and 3/3 source-form dataflow checks on each SM100/SM101/SM103/SM120 architecture against an independent format reference; it has no GPU validation. Scaled 8-bit upconvert passes 61,440 cases per exact/readable mode and 5/5 dataflow/control-flow checks per mode on each architecture; each lane sweeps all source bytes and scale bytes but not their full Cartesian product. PTX 9.2 documents the standard formats and conversion-then-scale order. The checked-in PTX probe cannot compile with the installed CUDA 13.0 ptxas (PTX 9.0 maximum), and the available GPU is SM89, so this family has no compiler-lowering or hardware execution evidence. The E2M1 and E0M3 upconvert matrices each pass 6,144 cases per mode on each of SM100/SM101/SM103/SM120, with all three source classes dataflow-checked; F16/BF16 E2M1 downconvert initially passed 1,560 cases per mode and architecture; the expanded E2M1/E0M3 suite passes 4,320 cases per mode on each of SM100/SM101/SM103/SM120; the UE8M0 matrix passes 1,536 cases per mode on each. All 128 E2M3/E3M2 encodings match CUDA’s host FP6 conversion reference, and UE8M0 matches CUDA's host conversion function. The E2M1 downconvert reference follows `cuda_fp4.hpp`'s RN-even rounding, saturation, NaN, and nibble-order rules; no compatible GPU is available for hardware validation. SM89 direct GPU probes established H0/H1 placement for F16 and confirmed H0 behavior for BF16/E6M9; the 48-form `MERGE_C` CX matrix validates RN/RZ, RELU, SATFINITE, and H0/H1 on GPU against an independent reference and exact/readable P-code. SM89 E6M9 `PACK_AB` validation covers RRR, RIR immediate, and RCR constant-bank sources across eight selector combinations: 13,824 GPU/reference comparisons and 1,536 whole-kernel GPU/P-code comparisons passed. Existing F2FP GPU probes cover F16/BF16 pack, TF32 compiler lowering, FP8 merge, and E4M3/E5M2 unpack as separately recorded in `verification.json`. F2IP and I2IP whole-kernel GPU coverage is recorded below; PTX probes confirm `cvt.pack.sat` lowering for I2IP widths. |
| `FMNMX` | Predicate-selected scalar float min/max. CuLifter compares in order and selects one input. | Native for nonan and `.NAN`, with `.FTZ`, `.FTZ.NAN`, and `.XORSIGN`, across all nine architectures and supported R/UR/immediate/constant-bank forms. `.NAN` returns canonical `0x7fffffff` if either input is NaN. `.XORSIGN` takes raw source signs before absolute modifiers, chooses by magnitude, then applies their XOR sign unless the result is NaN. CX and predicate-result `.IS_A` remain opaque. | Exact/readable emulator suites pass on nine architectures. The SM89 whole-kernel GPU oracle verifies min/max with `.NAN` and `.FTZ.NAN`, and `min.xorsign.abs.f32` lowered to and executed as `FMNMX.XORSIGN`: 640 GPU/reference/P-code comparisons passed across NaNs, signed zeros, infinities, subnormals, and sign combinations. |
| `FMNMX3` | Three-input floating min/max selected by the effective Pp predicate; registered only for SM100+. | Native for dynamic P0–P6/PT selectors with optional inversion, all FTZ/NAN selector combinations, and RRR, RUR, and immediate-B sources on SM100/SM103. Exact mode follows the PTX three-input min/max rules; readable mode keeps CuLifter's ordered-comparison formula. CX remains opaque. | SM100/SM103 exact suites pass 25,238 cases and readable suites pass 18,822 cases each. The focused fixtures cover all selector combinations, predicate directions/inversion, NaNs in each position, signed zeros, infinities, and subnormals. No compatible GPU is available; the exact rules are emulator-verified against PTX ISA documentation, not hardware-measured. |
| `HMMA`, `DMMA`, `IMMA`, `HGMMA`/`WGMMA`/`QGMMA` | Matrix multiply-accumulate paths. HMMA 884 uses vector backend intrinsics; Hopper WGMMA uses NVVM inline assembly or a limited CPU scalar/descriptor model. | Opaque for these families (when represented in our manifests). | Potentially high-impact gap, especially matrix paths. This is not one scalar semantic: layouts, fragment ownership, accumulation, descriptors and architecture all matter. CuLifter's CPU code itself marks register/shape restrictions and descriptor forms that pass C through. |
| `ATOM`, `ATOMG`, `ATOMS`, `RED`, `REDG` | Global/shared and newer descriptor atomics/reductions. CuLifter has backend-specific RMW lowering and specialized forms. | Ordinary `ATOM` has primitive support in this repo, with selector/context restrictions; `ATOMG`, `ATOMS`, `RED`, `REDG` remain opaque (the newer forms appear on their supporting architectures). | The newer global/shared/descriptor forms are gaps. The existing ATOM primitive means only unsupported operations/types/forms are candidates for extension; compare its runtime guards before claiming the ordinary family is missing. |
| `LDSM`, `STSM` | Warp-collective matrix shared-memory load/store, using `ldmatrix`/`stmatrix` inline assembly in NVVM and explicit CPU layout handling. | Opaque (2–4 classes/arch where present). | Clear family gap, with cross-lane fragment layout as the hard part. A scalar load/store is not equivalent. |
| `LDGSTS` | Global-to-shared asynchronous copy, including widths and guard predicate. NVVM emits GPU async-copy operations; CPU models a synchronous guarded copy. | Opaque: 5 classes/arch on SM80–SM120. | Clear gap. Memory ordering and asynchronous completion need an explicit execution model. |
| `UTMALDG`, `UTMASTG`, `UBLKCP`, `UBLKRED`, `ARRIVES`, `ACQBULK`, `SYNCS`, `FENCE`, `WARPGROUP` | Tensor/bulk async transfer and reduction, mbarrier operations, proxy fences, and WGMMA commit/wait. NVVM uses architecture-specific inline assembly; CPU has a software/runtime model for selected protocols. | Opaque where present. This repository explicitly leaves descriptor/extended memory and cross-thread synchronization state to handlers. | Major gap in modern async memory/synchronization. The code provides concrete mapping ideas, but the correctness depends on descriptors, memory spaces, participating lanes, ordering and barrier phases. |
| `QSPC` | Address-space query; PTX `isspacep.const` lowered to integer range comparisons on SM89 rather than QSPC. The CPU backend classifies addresses in its wrapper memory model. | Runtime primitive for G/L/S; SM89 GPU probes show `Rd` reports a one-hot input-space tag (G=1, S=2, L=4) independently of the queried selector. SM90+ D is cluster-visible shared memory and remains supported with `Rd=RZ`; nonzero `Rd` on D stays opaque pending SM90 hardware evidence. | The SASS address is an integer with no Ghidra address-space provenance, so P-code requires an execution-environment range mapping. Missing, unmapped, or overlapping ranges raise `UnsupportedSemantics`; GPU evidence covers SM89 only. |

VIMNMX's `Pu`/`Pv` outputs are per-lane source-selection indicators, not
comparison flags for unrelated conditions. An NVIDIA patent describes them
as reporting which input supplied the min/max result: scalar `.32` uses `Pu`,
packed `.16x2` uses `Pu` for lane 0 and `Pv` for lane 1, and `.8x4` uses
`Pu`/`Pv`/`Px`/`Py` for lanes 0–3. Following instructions can use those flags
to update associated values such as a winning score's row or column
([US20230101085A1](https://patents.google.com/patent/US20230101085A1/en)). The
machine description marks these as predicate writes; on SM90 it has a
dedicated `Predicate_vimnmx` enum for non-PT destinations. The patent is
internally inconsistent about polarity: its prose says the flag indicates
source B, while its pseudocode computes `result == source_a`. It also leaves
ties and the interaction with RELU unclear. We therefore still keep non-PT
outputs opaque; the available SM89 GPU lacks these SM90+ instruction forms.
An SM100 `ptxas` probe that materializes `a < b`, `a > b`, and `min(a,b) == a`
after scalar PTX min/max still emits `PT` in both VIMNMX predicate slots and
separate `ISETP` instructions. This is useful negative evidence: those ordinary
PTX comparisons do not cause `ptxas` to expose the selection flags, so this
probe cannot resolve their polarity or tie behavior.

## Partial overlap and tempting false positives

`ISETP.EX` is native for direct register, uniform, immediate, and ordinary
constant-bank sources where the architecture exposes those forms. It compares
the high words and uses `Pr` for the low-word result only when the high words
are equal, then applies the selected predicate combiner with `Pp`. SM75's
shared `EX`/`noex` selector has separate decode-time paths. CX descriptors and
SM120 ISETP64 classes remain opaque. Nine-architecture exact/readable suites,
18 dataflow variants, and an SM89 ptxas-generated 64-bit comparison GPU probe
validate the implemented forms.

The following are not missing wholesale: `FADD`, `FMUL`, `FFMA`, `FMNMX`,
`FSETP`, `DADD`, `DMUL`, `DFMA`, `DSETP`, `HADD2`, `HFMA2`, `IMAD`, `IADD3`,
`LEA`, `LOP3`, `PRMT`, `BMSK`, `SHF`, `ISETP`, `S2R`/`CS2R`, basic loads and
stores, branches, barriers, warp operations, and ordinary `ATOM` already have
families in our implementation. CuLifter can still suggest uncovered forms:
for example, its packed half min/max/comparisons extend our half arithmetic;
its backend-specific atomics extend the ordinary ATOM family; and its matrix
and async-memory code addresses forms we leave opaque. Our selector manifests
and `processor/SASS/semantics/README.md` describe the specific supported forms.

Some registered upstream entries should not be counted as gained semantics:

- `BFE`, `BFI`, and `PSETP` raise `UnsupportedInstructionException`.
- Generic `REDUX` in `lifter.py` copies the input through; only the CPU/NVVM
  subclasses replace that identity with a reduction. This repo already models
  selected warp operations as primitives, so compare the runtime's supported
  operations and execution context before calling REDUX wholly missing.
- `SSY`/`SYNC` and several memory/synchronization hints are no-ops in one or
  more backends. Some are intentional single-thread approximations; others
  discard observable inter-thread behavior.
- `FCHK` computes a special fast-division guard, rather than a general
  floating comparison. Its comments describe a compiler lowering idiom.
- `BMSK`'s upstream expression is a simple shift-and-mask and does not encode
  the clamp/wrap distinction that this repo has already investigated.
- `FMNMX3` uses ordered comparisons; the code alone does not establish its
  NaN/zero rules.
- `LEPC`, `BREAK`, and barrier handling rely on the lifter's execution model;
  placeholder constants or single-thread assumptions are not full GPU
  semantics.
- `MOV64`, `IMAD64`, `PACK64`, `PHI`, `SETZERO`, `CALLFUNC`, and similar names
  are lifter IR/parser operations or aliases, not necessarily distinct
  nvdisasm opcode families. The same applies to `.U` aliases and several
  lowering-generated names. They are excluded as standalone ISA gaps unless
  the machine description has a matching SASS family.

The upstream README documents an experimental lifter and says correctness is
validated on a limited subset of kernels. Its supported-instruction list is
not an independent proof that each handler is exact. The checked-in CuLifter
tests and per-handler comments are useful leads, but its evidence and target
architectures differ from our Ghidra p-code and GPU-oracle claims.

## Practical candidates to investigate next

For gaps with more execution-model work, assess `LDSM`/`STSM`, `LDGSTS`, and the SM90+ TMA/WGMMA family. Before porting a
handler, map its spelling to the exact `out/SMxx/md` classes, inspect operand
spans and selectors, and keep unsupported encodings opaque. Candidate
calculations should be tested against an independent integer/floating
reference and the hardware oracle on each architecture claimed.
