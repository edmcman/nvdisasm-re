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
| `BREV`, `FLO`, `POPC`, `SGXT` | Bit reversal; find-leading-one/normalizing shift; population count; signed byte extraction/extension. `FLO` handles U32/U64 and `.SH`; `POPC` calls an LLVM intrinsic. | BREV and POPC were already native. FLO and SGXT now have guarded native 32-bit paths for ordinary and uniform registers, immediates, and constant-bank inputs. CX forms and unsupported selectors remain opaque. | Scalar paths are ported in progress. FLO returns the highest set-bit index by default and the leading-zero count in `.SH`; signed 64-bit FLO and other selector combinations remain outside this change. SM89 PTX `bfind` compiler and GPU checks confirm both `FLO.U32` modes. |
| `VABSDIFF4` | Four packed signed/unsigned byte absolute differences; packed-byte result or accumulated scalar, with optional saturation. | Native guarded S8/U8 `SIMD_MRG` and `ACC` paths for non-CX forms with `Pu=PT`; all source shapes are covered where present. | SM89 GPU tests establish that SASS `SIMD_MRG` packs four differences and ignores C, while `ACC` scalar-sums the differences then adds C modulo 2^32. 128 whole-kernel GPU/p-code/reference checks pass per fmt/reduction pair. CX, non-PT Pu, and unrepresented SAT encodings remain opaque. PTX `vabsdiff4` syntax uses a lane mask and lane-wise `.add` ([PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)); the compiler probe expanded those PTX forms rather than emitting this SASS opcode, so the PTX-level rules are not used as a direct SASS mapping. |
| `VIMNMX`, `VIMNMX3`, `VIADDMNMX` | Per-lane packed integer min/max, three-way min/max, and add followed by min/max; supports S/U 8-, 16-, and 32-bit forms and RELU in helper code. | Signed RELU paths are native for all three families, with lane-wise clamp to zero. Unsigned RELU selector combinations are invalid per the machine description and remain opaque. `VIMNMX` covers S32/U32/S16x2/U16x2 on SM100/SM103 and S8x4/U8x4 additionally on SM120; non-PT Pu/Pv outputs and CX remain opaque. `VIMNMX3` covers S/U32 and S/U16x2 on SM100/SM103. `VIADDMNMX` covers S/U32 and S/U16x2 across five R/uniform/immediate classes on SM100/SM103. | Independent fixtures pass in exact/readable mode: VIMNMX 504 per SM100/SM103 and 756 on SM120; VIMNMX3 648 and VIADDMNMX 840 per SM100/SM103. All affected language pairs compile. The available SM89 GPU cannot execute these newer instructions, so there is no hardware verification. |
| `HADD2.F32`, `HMUL2`, `HMNMX2`, `HSETP2`, `HSET2` | Packed two-lane half arithmetic, min/max, comparisons and predicate/mask results. `HADD2.F32` adds the selected low half lanes and widens the result to F32. `HMNMX2` and comparison handlers apply lane broadcast and per-lane abs/neg; some forms call backend intrinsics. | HADD2.F32 is native across SM86/89/90/100/101/103/120 for supported selectors, FTZ/SAT and register/uniform/immediate/constant-bank shapes; CX remains opaque. HMUL2 is native for F16_V2 with no-flush, FTZ, or FMZ selectors and SAT/no-SAT across all architectures; register, uniform, immediate and constant-bank source shapes are supported where present. BF16/E6M9, FP32-input packed arithmetic, H0_NH1 and RELU remain opaque. HMNMX2/HSETP2/HSET2 remain opaque. | The SM89 GPU oracle verifies 48 HADD2.F32 selector/modifier combinations with 128 whole-kernel comparisons each. Constant-bank HADD2.F32 forms and all supported HADD2.F32 architecture variants pass exact/readable emulator suites. The SM89 GPU oracle also verifies 128 whole-kernel comparisons each for HMUL2, SAT, FTZ, FMZ and lane selection, including FMZ zero-times-infinity. FTZ flushes subnormal inputs/results preserving sign; FMZ yields canonical +0 if either selected source has zero exponent. Exact/readable emulator suites cover all nine architectures. Strong remaining gaps are half min/max and comparisons. |
| `IDP` | Four signed/unsigned 8-bit products accumulated with a 32-bit addend; supports mixed operand signedness. | Native guarded `IDP.4A` for R, URb, and ordinary constant-bank B sources; CX and 2A classes remain opaque. | Four products accumulate modulo 2^32; Rc negation is preserved. All four signedness pairs are covered by independent emulator fixtures across nine architectures. No IDP PTX-to-SASS or GPU check is recorded yet. |
| `P2R`, `R2P`, `UP2UR`, `B2R` | Pack/unpack predicate bits to/from integer or uniform registers; B2R obtains a barrier result. | P2R/R2P `.B0`–`.B3` support immediate, R, and UR masks on all nine architectures. UP2UR immediate-mask `.B0` is native for UP0–UP6 on all nine architectures. Constant/CX P2R/R2P masks, non-B0 UP2UR, register-mask UP2UR, and B2R remain opaque. | P2R replaces the selected byte of its base with selected P0–P6 bits; R2P reads that source byte into selected predicates and preserves unselected predicates. Both ignore mask bits above 6. Exact/readable semantic suites pass on all architectures. Focused P2R/R2P dataflow passes all 18 exact/readable language variants (24/24 each). `tests/predicate_transfer_semantics.py` passes 2,304 whole-kernel GPU/reference/exact-p-code comparisons on SM89 across 18 selector/mask cases. Constant-bank/CX masks stay opaque because URb is part of an address. UP2UR follows CuLifter's formula: preserve URa, OR each selected UPi into bit i for i=0..6, and ignore higher mask bits. `tests/up2ur_semantics.py` patches UP2UR into a compiled SM89 wrapper and passes 1,152 whole-kernel GPU/reference/exact-p-code comparisons across nine masks, including high mask bits and a nonzero base. The PTX lowering probe did not emit UP2UR. B2R still depends on synchronization state. |
| `F2F` / `F2FP`, `F2IP`, `I2IP`, `FRND` | Float-width conversion including packed F16/BF16/FP8 paths; packed float-to-int and int packing; float rounding. | F2IP has a guarded native F32-to-S8/U8 path for R inputs with RN-even/NTZ and no RELU. I2IP has guarded native S32-to-S/U2, S/U4, S/U8, and S/U16 paths with supported extraction selectors and saturation modes. Other F2F/F2FP, I2IP selectors/shapes, and FRND forms remain opaque. | SM89 GPU verifies F2IP round-even, saturation, NaN/Inf handling and byte packing. I2IP signed/unsigned widths, no-SAT and patched SATRELU modes pass whole-kernel GPU comparisons. All nine architectures pass exact/readable emulator suites. PTX probes confirm `cvt.pack.sat` lowering for I2IP widths. FP8 and other packed formats remain a broad gap. |
| `FMNMX` | Predicate-selected scalar float min/max. CuLifter compares in order and selects one input. | Native for nonan and `.NAN`, with `.FTZ` and `.FTZ.NAN`, across all nine architectures and supported R/UR/immediate/constant-bank forms. `.NAN` returns canonical `0x7fffffff` if either input is NaN; `.XORSIGN`, CX and predicate-result `.IS_A` remain opaque. | All exact/readable emulator suites pass on nine architectures. The SM89 whole-kernel GPU oracle verifies min/max with `.NAN` and `.FTZ.NAN`: 512 targeted GPU/reference/P-code comparisons passed across quiet/signaling NaNs in either source, two NaNs, signed zeros, infinities and subnormals. |
| `FMNMX3` | Three-input floating min/max selected by literal PT/!PT; registered only for SM100+. | Native for PT/!PT and `noftz`/`nonan` with RRR, RUR, and immediate-B sources on SM100/SM103. Uses two ordered comparisons; ties and unordered comparisons keep the earlier value. Dynamic predicate, FTZ/NAN selectors, and CX remain opaque. | Exact/readable emulator suites pass 48 independent cases per SM100/SM103, covering both predicate directions, ties, signed zeros, infinities and NaNs. No compatible GPU is available to establish hardware behavior; these edge rules currently follow CuLifter's ordered-compare formula. |
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
