# Semantics limitation audit

Worklist of semantics limitations identified in the 2026-10-11 review of
`sass/operations.py`, compared with the checked-in CuLifter machine
descriptions and repository semantics policy. Remove each item when its
implementation and evidence are complete.

## F2FP operand forms and modifiers

Resolved for materializable R/UR/immediate/ordinary constant-bank sources and
F32-to-F16/BF16 pack RELU/SATFINITE selectors, including the combined form.
SM89 GPU probes execute F16 PACK_AB register, immediate, and ordinary
constant-bank sources (128 vectors each), the F16/BF16 RELU/SATFINITE selector
matrix, and FP8 unpack RELU. CX loads now use the shared typed provider on
SM75-SM90; focused result fixtures cover CX consumers including F2FP, IDP,
integer min/max, and packed half operations. The SM89 `f2fp__RCxR` F16
PACK_AB form also passes 128 GPU/reference and 128 exact plus readable P-code
comparisons. BMSK CX probes test shared descriptor bounds. SM89 GPU probes also cover F2FP
PACK_AB and MERGE_C, F2IP, I2IP, HMNMX2, VABSDIFF4, and IDP.4A CX forms.
VIMNMX and VIADDMNMX still need family-specific hardware probes.
TF32 and other architectures have emulator
coverage but lack hardware evidence on the available SM89 GPU; that is a
verification scope limit, not a reason to restrict generated semantics.

The selector inventory is grounded in `sass.ir` classes and enums. On SM86,
SM89, and SM90, the `f2fp__` family allows
`DSTFMT=F16/BF16/E6M9/E8M7/E5M2/E4M3`; native F16/BF16 support is unchanged.
E6M9 `PACK_AB` is now native for RN/RZ, RELU, SATFINITE, and combinations.
Exact/readable semantic suites pass on SM86/SM89/SM90. An SM89 patched-SASS
probe covered RRR, RIR immediate, and RCR constant-bank forms and passed
13,824 GPU/reference comparisons plus 1,536 whole-kernel GPU/P-code
comparisons.
The probe tests underflow and overflow boundaries as well as random values,
infinities, NaNs, and signed zero. Exact/readable semantic suites also cover
R/UR/immediate/direct-C source classes on all three architectures.
E8M7 aliases BF16's encoded selector value. The SM89 `f2fp_rs`
stochastic-rounding family (RS, RNDSRC, RNDSZ, RNDTH) remains opaque pending a
correct model of its random input/state. `f2fp_merge_c` is now native for
materializable forms on all nine architectures: F32-to-F16/BF16/E6M9, RN/RZ,
RELU, SATFINITE, and combined selectors, with H0/H1 selecting the high-half
merge value. CX forms use the shared load provider where their class shape is
supported; focused emulator fixtures cover the available CX source shapes,
and one SM89 F16 `PACK_AB` CX source form passes GPU/reference/P-code checks.
The SM89 `MERGE_C` CX matrix now covers all 48 MD-valid combinations of
F16/BF16/E6M9, RN/RZ, RELU, SATFINITE, and H0/H1. GPU results matched an
independent conversion reference and exact/readable whole-kernel P-code for
6,144 vectors in each comparison set. The probe compares F16 NaNs by class,
because the GPU canonicalizes NaN payloads differently from the reference.
SM89 direct GPU probes established H0/H1 placement for F16 and confirmed H0
behavior for BF16 and E6M9; current exact/readable semantic suites cover the
available ordinary source classes on all architectures. Remaining F2FP
limitations are the conversion families and stochastic modes listed below,
plus hardware validation on architectures other than SM89.

FTZ/FMZ are not selector operands in the inspected SM89 F2FP classes; the
earlier claim that they were MD-valid F2FP selectors was unsupported and has
been removed. `ntz` is an ignored encoding bit in the unpack class, not an
FTZ/FMZ selector. The SM100 MD adds these distinct non-ALTERNATE F2FP class
families, which are not fully supported by the current native emitter:

SM100+ F16/BF16 and F32 downconversion to E4M3/E5M2 is native for RN/RP,
SATFINITE/nosatfinite, optional RELU, and H0/H1 merge selection. For
nosatfinite, E4M3 overflow and infinity produce positive canonical NaN, while
E5M2 produces signed infinity. Finite values follow the selected rounding
mode, so negative RP overflow rounds to negative max finite. SM89 GPU probes
execute patched no-SATFINITE SASS for both formats and pass 128 whole-kernel
GPU/P-code comparisons per format against an independent reference. This
provides hardware evidence on SM89; SM100+ behavior is emulator/reference
validated. The expanded FP8 suite passes exact and readable modes on all four
architectures: 24,560 cases per mode on SM100/SM101/SM103 and 28,616 on
SM120. Dataflow passes 10/10 classes per mode on SM100/SM101/SM103 and
16/16 on SM120. RP chooses toward +infinity, including the least positive
subnormal for positive nonzero values below the FP8 range; negative values
round toward zero, and signed zero is preserved. The available GPU is SM89,
so there is no direct SM100+ hardware validation. CUDA 13.4 `ptxas` rejects
`.rp` SATFINITE FP8 PTX, so RP has emulator/MD-selector evidence but no PTX
carrier. Other destination formats and selector combinations remain open.

E2M3/E3M2 downconversion is now native on SM100/SM101/SM103/SM120 for the ten
ordinary R/uniform/immediate classes, with SATFINITE, optional RELU, and RN,
RZ, or RP. The independent reference uses the documented six-bit formats,
nearest-even or directed rounding, and the PTX SATFINITE rules (NaN becomes
positive MAX_NORM; overflow and infinity become sign-preserving MAX_NORM).
The PTX ISA specifies the two output lanes in six bits each, padded to bytes,
with A in the high byte and B in the low byte. Each of SM100, SM101, SM103, and SM120 passes 7,320 exact and 7,320
readable value comparisons, 10/10 dataflow checks per mode, and 10/10
`nosatfinite` opaque-fallback checks. CUDA 13.4 `ptxas` compiles
RN `.version 9.1` E2M3/E3M2 carriers for `sm_100a`; `nvdisasm` shows
`F2FP.SATFINITE.E2M3.F32.PACK_AB_MERGE_C`. PTX `.rz` is documented only for
`sm_107f` in PTX 9.4, and PTX `.rp` is rejected, so RZ/RP have emulator and
MD-selector evidence but no compiler carrier. Hardware validation is pending:
the available GPU is SM89. E3M4/E8 downconversion and FP6 `nosatfinite`
semantics remain open; FP6 `nosatfinite` is intentionally opaque.

| MD class prefix | Encoded conversion | Principal selectors | Current status |
| --- | --- | --- | --- |
| `f2fp_f16_to_8b_downconvert__`, `f2fp_f32_to_8b_downconvert__`, SM120 `uf2fp_*` forms | F16/F32 to 8-bit formats | E4M3/E5M2 SATFINITE/nosatfinite with optional RELU and RN/RP where encoded; E2M3/E3M2 SATFINITE with RELU and RN/RZ/RP; E3M4/E8 and FP6 `nosatfinite` semantics remain open | Partial |
| `f2fp_f16_to_mx8_downconvert_scale__`, `f2fp_f32_to_mx8_downconvert_scale__` | BF16x2/F32x2 to scaled S2_6 | SATFINITE, optional RELU, RN across RIR/RRI/RRR/RRU/RUR | Partial: SATNARROW remains opaque pending independent semantics evidence |

The SATFINITE scaled S2_6 downconvert is native on SM100, SM101, SM103, and
SM120 for all five register/uniform/immediate source forms. It divides each
input by its UE8M0 scale, rounds to nearest even, packs input A in the high
byte and B in the low byte, maps NaN to positive MAX_NORM, and preserves the
sign when saturating infinities/overflow. The exact/readable emulator matrix
passes 9,920 cases per mode and 10/10 dataflow checks per mode on each of the
four architectures. Inputs include signed ties, infinities, NaNs, large
finite values, and scale bytes 0, 1, 126–128, 253–255 (including the subnormal
reciprocal for scale 254). These rules follow the [PTX ISA S2F6 conversion
specification](https://docs.nvidia.com/cuda/parallel-thread-execution/#data-movement-and-conversion-instructions-cvt).
CUDA 13.4 `ptxas` compiles RN `.version 9.1` E2M3/E3M2 carriers for
`sm_100a`, and `nvdisasm` shows the expected `F2FP.SATFINITE.E2M3` form.
RZ/RP carrier availability remains limited, and the available GPU is SM89, so
SM100+ hardware execution could not be checked. The distinct SASS `SATNARROW` selector remains on the opaque path:
the available PTX documentation does not establish its behavior. Focused
emulator checks confirm all ten `SATNARROW` source-form encodings use the
opaque fallback on each of SM100/SM101/SM103/SM120.

The `f2fp_rs_16b__` and `f2fp_rs_8b4b__` families encode stochastic
rounding and remain opaque until their random input/state is modeled. These
selectors and class shapes come from the SM100 MD; the related PTX `cvt`
formats document conversion and packing rules, but are lowering evidence for
SASS rather than proof of every SASS selector's behavior. See the [PTX ISA
conversion section](https://docs.nvidia.com/cuda/parallel-thread-execution/#data-movement-and-conversion-instructions-cvt).
No SM100 GPU validation is available from the current SM89-only hardware
environment. F2FP remains open for the listed conversion families and
stochastic rounding. The tested SM89 F16/BF16/E6M9 `MERGE_C` CX combinations
and the F16 `PACK_AB` CX form have direct hardware evidence; that evidence does
not cover CX forms for unsupported conversion families or other architectures.

Evidence standard for closing an item: MD classes/enums establish that a form
is encodable; CuLifter and compiler output are hypotheses/lowering evidence;
PTX documentation or an independent reference establishes expected rules;
GPU probes establish behavior only for the tested architecture and inputs;
emulator comparisons establish that P-code follows the reference, not that
the reference itself is correct. Record these evidence levels separately.

## HMNMX2 source-indicator hardware evidence

The ordinary HMNMX2 family has native F16_V2, BF16_V2, E8M7_V2, and E6M9_V2
paths for materializable register, uniform, immediate, and ordinary
constant-bank sources where present. E8M7_V2 aliases BF16_V2's encoded format.
The paths handle min/max polarity, lane broadcasts, absolute/negate, format-
specific flushing, NAN, and form-specific XORSIGN. RRR/RUR/RCR require both
encoded absolute source flags; RIR has only the register source's absolute
flag, and the immediate keeps its encoded sign. `.NAN` canonicalizes NaNs to
`0x7fff`; `.XORSIGN` compares magnitudes then writes the XOR of raw source
signs. With one NaN and default NaN handling, the selected non-NaN value still
receives that XOR sign; `.NAN` and two-NaN cases retain canonical `0x7fff`.
The SM89 GPU oracle includes this interaction for F16, BF16, and E6M9.
`MASK_INSTR_PREDICATE` does not suppress the ordinary instruction guard.

The complete exact/readable semantic matrices pass on SM75-SM120. The SM89
GPU oracle covers 19 F16, BF16, and E6M9 fixtures at 128 vectors each. E6M9
uses integer encoding-order comparisons and has no FTZ selector. CX sources
use the shared load provider on SM75-SM90, with focused result fixtures passing
in exact and readable emulator modes. SM90+ `IS_A` predicate-output classes now
have native P-code for MD-supported forms with `nan=nonan`; the `.NAN`
selector stays opaque until its interaction with source flags is known. `Pu`
and `Pv` are implemented as low/high-lane indicators that source A supplied
the selected result, based
on NVIDIA patent US 12,554,493. The patent supports the output role, but does
not settle every tie and NaN detail. Exact/readable emulator tests and dataflow
checks pass for 45 SM90 cases and 27 cases each on SM100, SM101, SM103, and
SM120; these establish that
P-code matches this interpretation and the md operand effects, not the
hardware truth table. The default two-NaN path's A flag is modeled but has not
been measured on hardware. An SM89 patched-word probe read back false in P2 for 32
vectors, while delayed-readback ISETP controls returned true. This only shows
that the available SM89 device does not produce these newer outputs. Hardware
validation on SM90 or newer remains open.
The SM89 CX hardware matrix passes all 64 MD-valid `RCxR` selector forms across
F16_V2, BF16_V2, E6M9_V2, min/max, NAN, XORSIGN, F16 FTZ, and finite/NaN CX
values: 8,192 GPU/reference and 8,192 exact P-code comparisons, plus 8,192
readable P-code comparisons. SM90 hardware validation of the `IS_A` outputs
remains pending.

## CX sources across operation families

The class-shape review is complete for `IDP`, `VIMNMX*`, `VIADDMNMX`,
`VABSDIFF4`, `F2FP`, `F2IP`, and `I2IP`. Their 32-bit value guards match the
MD `ISRC_*_SIZE` and `IDEST_SIZE` fields. The ordinary R, UR, immediate, and
direct C-bank classes are reached through the emitters' grouped source
lookups; their operand kind does not change the operation. IDP `.2A.LO/.HI`
is implemented for R, URb, and direct C-bank B sources, alongside `.4A`.
The PTX ISA documents `.2A` lane selection; 312 SM89 emulator cases and eight
PTX-lowered GPU forms (128 vectors each) pass.

CX is now materialized for SM75-SM90 as a bounded load through the MD-declared
64-bit uniform-register pair plus byte offset. SM75-SM89 use the 45-bit base,
4-bit address scaling, and 16-byte size units; SM90 uses a 51-bit base and
6-bit address scaling. The load returns zero when the access exceeds the
encoded bound. Because the CX compound contains its UR child, operation source
selection prefers the compound loaded value over that address child.

I2IP now supports its MD-valid H1 selector for 8/16-bit destinations; H1
selects Rc[31:16] for the preserved upper half of 8-bit results and has no
effect when 16-bit outputs fill Rd. The SM89 CX matrix covers 40 permitted
selector forms and passes 5,120 GPU/reference and exact/readable P-code
comparisons per mode.

The ordinary supported CX forms in `IDP`, `VIMNMX*`, `VIADDMNMX`,
`VABSDIFF4`, `F2FP`, `F2IP`, `I2IP`, packed half arithmetic, and HMNMX2 now pass their
existing materialized-width and selector checks where the emitter handles the
class shape. Focused CX result fixtures pass on SM75 (10/10) and SM90 (16/16)
in both exact and readable modes; SM89 has its full exact/readable suite and
explicit IDP.4A CX and HADD2.F32 CX load fixtures. The shared address
interpretation is inferred from the identical MD compound layout. SM89 BMSK
CX probes pass 8,192 GPU/reference and 8,192 exact P-code comparisons; the
F2FP F16 PACK_AB `RCxR` probe passes 128 GPU/reference comparisons in both exact
and readable P-code modes. The 48-form F2FP F16/BF16/E6M9 `MERGE_C` CX matrix
passes 6,144 GPU/reference and exact/readable P-code comparisons per mode. The
VABSDIFF4 CX probe passes 16 forms and 2,048 GPU/reference and exact/readable
P-code comparisons per mode. The I2IP CX probe passes 40 forms and 5,120
GPU/reference and exact/readable P-code comparisons per mode. The F2IP CX probe
passes 16 forms and 2,048 GPU/reference and exact/readable P-code comparisons
per mode. The HMNMX2 `RCxR` selector probe passes 64 forms with
8,192 GPU/reference and exact/readable P-code comparisons per mode. The IDP.4A
CX probe passes all four S8/U8 source-format combinations with 512
GPU/reference and exact/readable P-code comparisons per mode. VIMNMX and
VIADDMNMX still need targeted hardware
probes before claiming hardware-established behavior. CX
forms for stochastic rounding and unsupported conversion families remain
opaque for their separate semantic limitations. This is now a hardware-evidence
gap, rather than a blanket dynamic-load limitation.


