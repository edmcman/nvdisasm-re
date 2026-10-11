# ptxas final SASS encoding trace

Target: CUDA 13.0.88 `/usr/local/cuda-13.0/bin/ptxas`, SHA256
`daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`.
Addresses are absolute runtime/Ghidra addresses for this non-PIE build, **not**
the nvdisasm addresses in AGENTS.md.

## Reproduction

rr 5.9.0 records and replays this probe using native CPU detection; no Skylake
override is needed. Both commands exited successfully on 2026-10-08:

```sh
rr record -o /tmp/rr-ptxas-native59 /usr/local/cuda-13.0/bin/ptxas -arch=sm_100 -o /tmp/ptxas_rr_native59.cubin /tmp/ptxas_one_add.ptx
rr replay --autopilot /tmp/rr-ptxas-native59
```

Subsequent unbound recording hit the zero-branch-counter error. Binding to
CPU0 with `--bind-to-cpu=0` resolved it without a microarchitecture override;
use that binding for repeatable recording on this hybrid CPU. The original
unbound successful recording above remains valid.

Probe kernel loads two u32 parameters, adds them, and stores the result through
a u64 pointer. Final cubin is 5192 bytes. `.text.one_add` is at file offset
0x680, size 0x100. nvdisasm confirms LDC, LDC.64, LDCU.64, LDC.64, IADD3,
STG, EXIT, BRA and eight padding NOPs.

## Confirmed path

1. Driver `0x446240` serializes an intermediate ELF at call `0x4476ad`.
   Its text is 160 bytes at offset 0x570; it differs from native SASS.
2. `finalize_serialized_elf` (`0x612de0`), called at `0x447a91`, receives
   input ELF in RSI, output-pointer slot in RDX, output-size slot in RCX.
   It produces the final native SASS cubin.
3. `encode_finalizer_instruction_list` (`0x6e4110`) is called through
   `run_mercury_encoding_pipeline` (`0x6f52f0`). It clears a 16-byte scratch,
   invokes an encoder object's vtable+0x10, and copies that word into the
   output instruction stream at `0x6e46ae`.
4. The live encoder method is `dispatch_sm100_final_sass_encoder` (`0x12c3510`).
   It dispatches by instruction u16 at +0xc and variant bytes at +0xe/+0xf.
5. `0x1c9f280` copies already encoded text into final ELF at call `0x1c9feba`.
6. Driver calls fwrite (return PC `0x448a08`) once with the final 5192 bytes.

Reverse watchpoints confirmed steps 3–5. At the ELF copy, RSI points to a
256-byte native SASS buffer. Watching its first qword backward reaches
`0x6e46ae`; watching that instruction's source scratch backward reaches
handler `0x1a71e40`.

## Final dispatch tables

Directory at `0x232e6e0`: 16-byte entries `{row_pointer, row_count}` indexed
by opcode. Rows are 24 bytes: key bytes at +0/+1, member-function pointer at
+8, signed this adjustment at +0x10. The dispatcher binary searches the
two keys, calls the handler, then calls `0x9b3020` for shared processing.
An odd member-function pointer denotes a virtual call under the Itanium ABI.

Live mappings, in final instruction order:

| Opcode index | Keys (hex) | Handler | Final mnemonic |
|---|---|---|---|
| e4 | 01/03 | 1a71e40 | LDC, LDC.64 |
| d3 | 00/03 | 12b7560 | LDCU.64 |
| 0c | 20/02 | 1255720 | IADD3 |
| 38 | 07/19 | 135daf0 | STG.E |
| 11 | 00/05 | 1a25630 | EXIT |
| 04 | 04/07 | 1261400 | BRA |
| 2d | 00/02 | 1a488f0 | NOP |

LDC directory entry `0x232f520` points to four rows at `0x2330e00`;
01/03 row `0x2330e18` points to `0x1a71e40`. That handler sets opcode 0xb82,
Rd bits16..23, base bits24..31, bank bits54..58 and offset bits38..53,
alongside guard/selector fields. For `LDC R1,c[0][0x37c]`, resulting qwords
are `0000df00ff017b82 / 000ff00000000800` after shared processing.

IADD3 row `0x2339220` points to `0x1255720`, which starts with opcode 0x210.
The live instruction is `IADD3 R5,PT,PT,R4,R5,RZ`.

Ghidra contains renamed/commented handlers, dispatcher and list encoder, and
partial evidence-backed `FinalSassOperand`, `FinalSassInstruction`,
`FinalSassEncoder` types under `/ptxas_finalizer`. Unknown bytes remain undefined.

## Intermediate IADD3 bridge (rr verified)

The finalizer reads the IADD3 directly from **input ELF +0x5d0** (text offset
0x60). Its 16 bytes are `010c4004f8000300410101f800014001`. Header fields:
length16, namespace0, opcode0xc, variant20/02. Register numbers are 10-bit
fields at bit70 (Rd=5), bit102 (Ra=4), bit118 (Rb=5).

`decode_intermediate_text_to_finalizer_ir` (`0x6f2bf0`) copies from that ELF
cursor into decoder scratch+0x220 at `0x6f319b`.
`dispatch_intermediate_instruction_decoder` (`0x1803d50`) uses directory
`0x239a4a0` and calls `decode_intermediate_IADD3_variant_20_02` (`0xeb76c0`).
It creates instruction `0x2668ac70` in this recording—the same pointer passed
later to native handler `0x1255720`. Reverse watches of Rd followed the
operand array's capacity-growth copies back to the initial bit decode.

Before serialization, `dispatch_ori_instruction_to_encoder` (`0x18f1be0`)
invokes `dispatch_intermediate_stream_encoder` (`0x17aa010`), whose directory
is `0x23a6940`. The earlier IADD3 has the same register values. Its operands
were built by `materialize_converted_instruction_operands` (`0x1bbbdc0`),
called by `dispatch_ori_opcode_conversion` (`0x9ed2d0`) from
`merc_convert_instruction` (`0x9f3340`), the per-instruction step of phase
141 MercConverter (see "ConvertUnsupportedOps vs MercConverter"; this function
was previously misnamed `run_convert_unsupported_ops_phase`).

The materializer iterates a tree of typed operand nodes: kind1 becomes a
general-register operand (kind2), kind2 becomes a predicate operand (kind1).
Register numbers are already assigned at node+0x38. It appends 32-byte records
with `append_instruction_operand_copy` (`0x10afaf0`), whose growth helper is
`reserve_instruction_operand_capacity` (`0x6fb7b0`). Destination R5's copy
from node+0x38 to its temporary record is at `0x1bbc84e`.

This proves intermediate-to-native repacking for this IADD3 example. It does
not prove that all finalization is one-to-one, nor identify where PTX addition
was originally lowered or registers allocated.

The shared suffix after native variant dispatch is now named
`encode_final_sass_scheduling_fields` (`0x9b3020`). It reads instruction+0x70
and packs metadata into the high qword; specific scheduling field names remain
to be correlated with machine descriptions.

## Earlier add lowering

`lower_add_operation_to_target_instruction` (`0x9daa40`) is live for this
probe, called by opcode conversion. It selects target opcode0xc and populates
role6 (destination), role1 (predicate/default), role0xd (arithmetic source A),
role0x14 (arithmetic source B), with further optional operand handling.
`set_converted_instruction_operand_by_role` (`0x1bbd2c0`) maps roles6/1/2
to target slots0/4/5 for IADD3. Its write at0x1bbd5b8 replaces a typed operand
payload; the observed value31->5 reflects replacing a predicate/default with
the destination operand, **not** proof of register allocation there.

At lowering entry, input+0x48=2,+0x4c=0xb,+0x50=3, with tagged operands
0x9000004e,0x10000057,0x10000058 at+0x54/+0x5c/+0x64. Meanings of these
tags and type numbers still need verification. This is lowering of internal
IR, not the PTX text parser. The function includes type-dependent paths.

## Reusable capture and validation

`research/ptxas/capture_finalizer.py` can be sourced in rr's GDB before
continuing. It logs intermediate bytes, decoded instruction identities,
native dispatch table rows and operand records, and final16-byte words.
The probe is checked in as `tests/ptx/arithmetic/one_add.ptx`; captured evidence is
`research/ptxas/one_add_finalizer.jsonl`.

Validated recording:7 intermediate decodes,16 native instructions. All256
captured native bytes match the final cubin text exactly. IADD3 has the same
instruction identity before and after finalizer processing:

```
intermediate: 010c4004f8000300410101f800014001
native:       1072050405000000ffe0ff0700ca1f00
```

Batch replay works with debugger `-ex` arguments:

```sh
PTXAS_CAPTURE=/tmp/capture.jsonl rr replay TRACE -- -batch -ex 'source /home/ed/Projects/nvdisasm-re/research/ptxas/capture_finalizer.py' -ex continue
```

Using rr `-x` to run `continue` failed because its command file was executed
before the debugger attached. A finish breakpoint captures each native word
after the shared scheduling suffix. The script also captures add lowering
and operand construction. Output appends; choose a fresh filename per run.

## Differential add/sub/type probes

Four additional checked-in PTX probes and JSONL traces were compiled with
the same ptxas/SM100 and captured with rr. Every emitted native byte matched
the corresponding cubin text (256 bytes each, except384 for add_u64).

| PTX probe | Internal flags/type at add lowering | Native arithmetic |
|---|---|---|
| add.s32 | 2 / 11 | IADD3 R5,PT,PT,R4,R5,RZ |
| sub.u32 | 2 / 11; source2 modifier0x80000000 | IADD3 R5,PT,PT,R4,-R5,RZ |
| add.rn.f32 | 0x2002 / 6 | FADD R5,R4,R5 |
| add.u64 | two operations,5 / 11,each6 operands | UIADD3 then UIADD3.X |

The signed32 probe normalizes to the same internal type11 as unsigned32;
type11 should not be described as preserving signedness in this context.
**Target class0xc is an arithmetic family**, not uniquely IADD3. Its keys
20/02 select IADD3 (`0x1255720`),08/05 select FADD (`0x1256480`),24/02
select UIADD3 (`0x1255c10`),1c/0a select UIADD3.X (`0x1258090`). These live
handlers are renamed/typed in Ghidra. Earlier IADD3-specific names refer to
particular variants, not the whole class.

`python3 research/ptxas/validate_capture.py CAPTURE.jsonl OUTPUT.cubin`
checks native dispatch/encoding identities and all emitted bytes against
the cubin's actual text sections. It passed for all five stored probe captures.

The 64-bit uniform carry chain is:

```
UIADD3   UR4,UP0,UPT,UR4,UR6,URZ
UIADD3.X UR5,UPT,UPT,UR5,UR7,URZ,UP0,!UPT
```

It is already split into two operations before `0x9daa40`. The splitter is
now confirmed as `emit_wide_add_as_two_carry_operations` (`0xa31040`).

## Wide-add splitter and carry dataflow

Reverse watching the low half's internal opcode reaches instruction creation
at0x92c43f in `create_and_insert_lowering_ir_instruction` (`0x92c240`),
called through `emit_six_operand_ir_instruction` (`0x9331f0`) by the splitter.
`lower_wide_integer_operations` (`0xa36360`) dispatches original opcode2,
type10 to this path; it also handles other wide operation families.

The splitter allocates a predicate, emits a low-word and a high-word opcode5
operation, updates the destination pair, and removes the original instruction.
`narrow_64bit_arithmetic_type_to_32bit` (`0x7e53d0`) maps9->11,10->12,
otherwise6. This unsigned64 probe starts as type10, becomes two type12
operations, then becomes type11 before target conversion.

The six tagged operands are destination, predicate output, source A, source B,
predicate input, control. Live construction in the recording:

| Operand | Low word | High word |
|---|---|---|
| destination | DEF ID0x57 | DEF ID0x58 |
| predicate output | DEF ID0x59 | discard0xf0000000 |
| source A | constant descriptor0x50000002 | constant descriptor0x50000006 |
| source B | constant descriptor0x50000004 | constant descriptor0x50000007 |
| predicate input | absent0x70000000 | USE ID0x59 |
| control | 0x60000001 | 0x60000001 |

DEF uses tag0x90000000; USE uses0x10000000. Subsequent source materialization
creates register IDs0x5b..0x5e. Allocation maps destination IDs0x57/0x58 to
UR4/UR5 (IR class3); predicate ID0x59 maps to UP0 (IR class2). Sources map
to UR4,UR6,UR5,UR7. Thus the explicit compiler dependency matches the final
UIADD3/ UIADD3.X carry chain. The captured low predicate DEF ID and high
predicate USE ID were checked equal, and all384 final bytes match the cubin.

The extended `add_u64_finalizer.jsonl` now includes original-wide input,
both constructed carry operations, allocated operand mapping, intermediate
decoding and final native encoding. The capture script records all six
descriptors, rather than truncating each lower-add record to three.

These observations support low-word add with carry passed to the high word;
they do not independently establish every predicate/carry modifier's hardware
behavior. Existing SM89 carry tests provide separate execution evidence.

### Wide subtraction: negation becomes complement in the high word

The `sub_u64.ptx` probe also reaches original opcode 2, type 10, with
source B's modifier word set to `0x80000000`. The same splitter emits:

```
low:  source B modifier 0x80000000 (negation)
high: source B modifier 0x20000000 (complement)
```

The low predicate DEF and high predicate USE both have ID `0x59`, allocated
to UP0. Source conversion maps those modifier words to payload byte 0 bits
0 and 2 respectively. The resulting native instructions are:

```
UIADD3   UR4, UP0, UPT, UR4, -UR6, URZ
UIADD3.X UR5, UPT, UPT, UR5, ~UR7, URZ, UP0, !UPT
```

For 32-bit halves, the low sum is `a_lo + (~b_lo & 0xffffffff) + 1`,
retaining its 33rd bit as the carry predicate. The high sum is
`a_hi + (~b_hi & 0xffffffff) + carry`. This includes the important
`b_lo == 0` case: negation contributes `2^32`, and carry is set even
though the low result simply equals `a_lo`. Together the words implement
`(a - b) mod 2^64`.

`sub_u64_finalizer.jsonl` captures the original operation, both split
operations, allocated operands, intermediate decoding and final encoding.
All 24 encoded instructions (384 bytes) match the final cubin. The low
negation, high complement and equal predicate IDs were checked directly.
The Ghidra splitter comment now records both add and subtraction evidence.

`research/ptxas/check_wide_chains.py ADD_CUBIN SUB_CUBIN` executes the actual
compiled SM100 arithmetic words in Ghidra's `PcodeEmulator`, transferring the
low destination and predicate to the high-word state. On these probes,
1,224 PTX-formula comparisons passed (100 edge pairs and 512 deterministic
random pairs per operation; 2,448 native-p-code instruction executions).
The check also verifies the low carry predicate and rejects opaque/primitive
calls in the arithmetic p-code. It tests the two arithmetic words, not the
whole kernel or SM100 hardware. Compile inputs with CUDA 13.0.88 ptxas:

```
ptxas -arch=sm_100 tests/ptx/arithmetic/add_u64.ptx -o /tmp/ptxas_add_u64.cubin
ptxas -arch=sm_100 tests/ptx/arithmetic/sub_u64.ptx -o /tmp/ptxas_sub_u64.cubin
python3 research/ptxas/check_wide_chains.py /tmp/ptxas_add_u64.cubin /tmp/ptxas_sub_u64.cubin
```

## Register allocation and modifier mapping

The IR register table is at context+0x58. Tagged descriptor word0 masked
with0xffffff indexes this table; `(word0>>28)&7 == 1` selects the register
path. Record+0x40 is the register class, **+0x44 is the allocated physical
number**. `resolve_allocated_register_number` (`0xa3b930`) was renamed from
the stale `resolve_sass_register_class_index` interpretation.

The probe's destination ID0x4e maps to class6/R5; source IDs0x57/0x58 map to
class6/R4,R5. `build_target_destination_operand` (`0x9d1880`) and
`build_target_source_operand` (`0x9d4380`) build64-byte operand payloads,
with kind1 at+0x10 and physical number at+0x18. The later materializer
converts them to32-byte target operands (kind2/general register).

For sub.u32, descriptor word1 bit31 becomes converted payload byte0 bit0,
then native IADD3's arithmetic negate modifier. Bit30 becomes payload bit1
and bit29 toggles bit2, but those meanings are not independently verified.

Reverse watching destination register record+0x44 reaches write0x94fe7c in
`assign_physical_register_to_allocation_chain` (`0x94fdd0`), assigning5
from unallocated0xffffffff. Its callers include
`allocate_one_register_class` (`0x971a90`) and `run_register_allocation`
(`0x9721c0`), earlier than add lowering. Ghidra has partial types for
allocated register records, tagged descriptors,64-byte converted operands
and internal lowering instruction headers under `/ptxas_lowering`.

## Wide integer multiplication

`mul_wide_u32.ptx` and `mul_wide_s32.ptx` establish this path:

```
dispatch_ori_opcode_conversion (0x9ed2d0), case 0x8d
  -> lower_multiply_family_to_target (0x9d4c10)
  -> conversion vtable + 0xb8
  -> lower_integer_multiply_to_IMAD (0x9db020)
  -> begin_converted_target_instruction (0x1bb8f40), class 0x20
  -> intermediate stream / finalizer decode
  -> dispatch_sm100_final_sass_encoder, keys 0x1e/0x0a
  -> encode_sm100_IMAD_WIDE_variant_1e_0a (0x12857d0)
```

A reverse watchpoint on pre-serialization instruction class +0xc stopped at
the class write `0x1bb8f5c`; its caller returned to `0x9db050`. This identified
the integer lowering method directly. The target begin method also clears
the builder's prior operand/modifier trees.

Both inputs use opcode `0x8d`, three descriptors (destination DEF ID0x4e,
source USE IDs0x57/0x58), with type12 for unsigned and type11 for signed.
`parse_integer_multiply_shape` (`0x7e1dc0`) constructs a now-typed
`IntegerMultiplyShape`: destination index0, sources1/2, absent addend/predicate
indices=-1, predicate class5, wide_result=1, other result flags0. The parser
sets wide_result at +0x37; low/high flags are +0x35/+0x36. The lowerer passes
width2 to destination conversion and width1 to both sources, and synthesizes
the zero addend at operand role0x1b. Physical allocation is R2 for the
destination pair and R2/R3 for sources; the result therefore overlaps inputs.

Both variants select directory class0x20, row `0x23365b0`, keys0x1e/0x0a,
handler `0x12857d0`. **Signedness is not a dispatch key.** The lowerer's
vtable+0x170 setter receives0x18 (unsigned) or0x14 (signed). At final dispatch,
instruction selector flags at +0x30 are0x84/0x88. Getter `0x10b7910` extracts
bits2..3 and mapper `0x10b44e0` selects high-qword bit9 (native bit73).
In the actual cubins only that bit differs between the arithmetic words:

```
unsigned: 2572020203000000ff008e0700ca1f00
signed:   2572020203000000ff028e0700ca1f00
```

The encoder packs opcode0x225, Rd at bits16..23, Ra24..31, Rb32..39,
and Rc64..71. Final instructions are
`IMAD.WIDE.U32 R2, R2, R3, RZ` and `IMAD.WIDE R2, R2, R3, RZ`.
All16 encoded instructions (256 bytes per probe) match the cubins.
The `mul_wide_{u32,s32}_finalizer.jsonl` captures include lowering, parsed
shape, allocated operand conversion, intermediate decoding, selector flags,
dispatch row and final native bytes.

`check_wide_multiply.py U32_CUBIN S32_CUBIN` executes those actual words in
Ghidra native p-code and checks an independent signed/unsigned product
modulo2^64. All1,186 comparisons passed (81 edge pairs plus512 deterministic
random pairs per mode), including negative inputs and source/destination
overlap. The check rejects opaque/primitive arithmetic. This establishes
compiler/emulator consistency, not execution on SM100 hardware.

## Nonzero multiply-add operands and a uniform-zero semantics gap

`mad_hi_u32.ptx` and `mad_wide_u32.ptx` use a third, nonzero parameter.
Both complete encoder streams match their cubins (24 words/384 bytes each).

For `mad.hi.u32`, reverse watching the final lowering opcode reaches its
creation at `0x92c43f`, called through `0x933100`, return `0x9c520d`.
The caller is now named/typed `legalize_high_multiply_addend_to_pair`
(`0x9c5000`). Its input is opcode0x73, type12, four descriptors:
dest DEF0x51, source USE0x5b/0x5c, addend USE0x5d. Parsed shape has_addend=1,
high_result=1. For the register addend it emits opcode0x110, type10,
with operands zero and original addend, constructing a 64-bit pair whose
low half is zero and high half is c. Literal addends use `value << 32`
instead. It then emits a seven-operand opcode0x70 with destination,
discard predicate output, sources, pair addend, absent predicate input,
and control0x60000007, and deletes the original instruction.

The control's mode3 sets shape +0x38 rather than the ordinary high_result
flag +0x36; +0x38 is still named unknown_38 in the partial type. The lowerer
uses either flag to select high-word output and a two-register addend.
The complete scope of the alternate mode remains uncharacterized.

Final code loads c into R5, sets R4 to zero using HFMA2, then executes:

```
IMAD.HI.U32 R5, R6, R7, R4
```

The addend is the pair R4/R5, not just R4. The destination overlaps its
high half. Encoder `encode_sm100_IMAD_HI_variant_04_0a` (`0x1a36290`),
row0x2336340, class0x20/keys0x04/0x0a, packs native opcode0x227.
This proves the compiler representation of PTX's
`(high32(a*b) + c) mod 2^32` as SASS `high32(a*b + (uint64(c)<<32))`.
Actual native p-code passed637 independent PTX-formula comparisons.
An additional515 comparisons with arbitrary full 64-bit SASS addends
passed, including low-word carries and wraparound. These additional checks
validate the emulator formula, not hardware execution or a PTX lowering
with arbitrary low addend bits.

For `mad.wide.u32`, the chosen uniform lowering is:

```
UIMAD.WIDE.U32 UR4, UR4, UR5, URZ
UIADD3        UR4, UP0, UPT, UR4, UR6, URZ
UIADD3.X      UR5, UPT, UPT, UR5, UR7, URZ, UP0, !UPT
```

UR6/UR7 contain the original 64-bit c. The wide multiply encoder is now
`encode_sm100_UIMAD_WIDE_variant_23_0a` (`0x12864f0`), row0x2336628,
class0x20/keys0x23/0x0a. Register operand kind0xa is uniform; the zero
sentinel0x3ff maps to encoder+0xc's value, emitted as an eight-bit URZ255.

**Uniform zero (fixed 2026-10-08):** this real compiler word exposed that
SLEIGH hard-coded URZ as 63. SM75-SM90 define a 64-entry UniformRegister
enum with URZ=63; SM100+ define 256 entries with URZ=255 and UR63 ordinary.
`Gen.register_zero` now takes URZ from the md and masks it to the operand
field width, so six-bit fields keep 63. URZ has its own slot outside the
64-register bank. `check_mad.py` passes all three checks (1,274 PTX-formula
and 515 full-addend comparisons). `test_semantics.py` covers URZ with
UIMAD.WIDE/UIADD3 and, on SM100+, UR63 as storage; at the old HEAD the
UIMAD.WIDE case is `sass_opaque_UIMAD`.

## Lowering-table survey (2026-10-08)

ptxas has no data table for PTX/ORI→SASS lowering. Conversion is code:
`dispatch_ori_opcode_conversion` (`0x9ed2d0`, MercConverter) is a ~240-case switch on the
internal opcode (`insn+0x48 & ~0x3000`) calling per-family handlers, e.g.
`lower_wide_integer_operations` (`0xa36360`, cases 2/3/5/7 add-like, 6 mul,
10/0x95/0x97/0x122, 0x24, 0x62...). Statically initialized data found:

- **SASS/Mercury opcode names**: 773 `{char*, len}` entries at `0x29fe300`
  (.bss, filled by a static initializer around `0x40c000`), ROT13, sorted
  alphabetically (`ACQBULK`=0 ... `IADD3`=0x8c, `IMAD`=0x93 ... `NONE`=0x304).
  It includes 644 `MERCURY_*` pseudo-ops (e.g. `MERCURY_min_srcs_uimm_0`,
  `MERCURY_redux_s32_sync_unaligned_3`, `MERCURY_mov_b32_dests_ur_srcs_sr_0`),
  99 families, mostly barriers/atomics/mbarrier/warpgroup MMA/min/max/
  addmin/vabsdiff4/sad. These are pre-expansion opcodes, so their bodies
  are code. This enum is not the final encoder directory's index
  (IADD3 is 0x0c there).
- **Optimizer phases**: 159 names at `0x22bd0c0` (`OriCheckInitialProgram`,
  `ConvertUnsupportedOps`, ...).
- **PTX-level templates**: 269 ROT47 names at `0x27fc1e8`
  (`mad_fused_hi_tmpl_1`, `tanh_tmpl_1`, `cvt_tmpl_5`, `atom_tmpl_12`,
  cp.async.bulk, warpgroup MMA...). They have a parallel array of 269 functions
  (`0x4657d0`-`0x46be80`), each calling `0x465030` with a PTX opcode string
  (`mad.fused.hi`, `tanh`), a type (`I32`, `F16`) and an operand-kind string
  (`000U`). These register instruction signatures, not expansion bodies.
- **Built-in routine prototypes**: plain-text PTX declarations such as
  `__cuda_sm20_div_s16`, `__cuda_sm20_dblrcp_rn_slowpath_v3`,
  `__cuda_sm20_dsqrt_rn_f64_v3` and the `__cuda_sm1xx_cp_async_bulk_tensor_*`
  family (~1,084 PTX-like strings, 181 KB). No plaintext bodies; if the bodies
  exist, they are encoded or compressed elsewhere (not yet found).

None of these encode value semantics. ptxas lowering semantics must come from
handler code or from black-box differential compilation.

## Printing ORI (2026-10-08)

**No built-in dump in release ptxas.** Knobs (~1,800, ROT13 names, e.g.
`DUMPIR`) can be set only by a function-level PTX pragma
`.pragma "global knob NAME=VALUE";`, which is accepted only with the
undocumented option `-uumn` (sets options+0x230 → ctx+0x7741; without it:
"Pragma ... unsupported"). There is no `-knob` option and no knob environment
variable in this build; `DUMP_KNOBS_TO_FILE` is read but wrote nothing. With
`-uumn`, the pragma reaches `ParseKnobsString` (`0x79b530`), but DUMPIR does
nothing: in the phase loop (`0xc64f70`) the "Before/After <phase>" strings are
built and freed unused, the Report* phases (10, 112, 120, 123) have empty
`execute` (`rep ret`) and `isNoOp` = 1, and DUMPIR (OCG knob 0x10a) is read
only on the register-allocator error path. Phase numbers come from the name
table at `0x22bd0c0` (157 phases scheduled); the third-party wiki
(GrigoryEvko/crucible-notes, ptxas/wiki) gives phase numbers off by up to 33,
and its `-knob` recipe applies to internal builds.

**Printer.** `research/ptxas/ori_print.py` (gdb Python) breaks at the
dispatch loop's `execute` call (`0xc6508d`: rsi = ctx, r13 = phase name) and
walks the IR:

- instruction list head ctx+0x110, links +0 prev / +8 next; opcode +0x48
  (`& ~0x3000`, the masked bits are flags), type +0x4c, operand count +0x50,
  8-byte operands from +0x54;
- operand word: bit 31 = DEF, bits 28-30 = kind (1 vreg, 2 symbol/param,
  4 label, 5 constant, 6 immediate, 7 absent/discard), low 24 bits = id;
  modifier word: 0x80000000 negate, 0x20000000 complement, 0x0100000N memory
  in space N (2 param, 3 global), 0x02040000/0x04000000 appear on pair halves;
- vreg descriptor `*(ctx+0x58)[id]`: class +0x40 (2 UP, 3 UR, 5 P, 6 R,
  9 fixed system values whose number is their own id; checked against final
  SASS), physical register +0x44 (-1 before allocation);
- type +0x4c: 1 none, 6 f32 (also any 32-bit constant-bank load), 7 f16,
  9 s64, 10 u64/b64, 11 s32 (also sign-agnostic 32-bit add/mul.lo/mad.lo),
  12 u32/b32, 19 f64, 20 pred, 26 condition code, 31 bf16; 28 seen only on
  multimem f16x2. The printer shows these names (`ORI_RAW=1` keeps tN).

**Opcode names in bulk.** `research/ptxas/ori_opcodes.py OUT` compiles 146
single-instruction kernels (one PTX instruction between typed parameter loads
and a store), dumps the IR before `OriCheckInitialProgram`, and subtracts a
baseline with the same signature. `research/ptxas/ori_opnames.json` holds the
60 resulting names, each with its evidence; the printer loads it. Type field:
6 f32, 7 f16, 9 s64, 10 u64/b64, 11 s32, 12 u32/b32, 19 f64, 20 pred,
26 condition code (`add.cc` result). abs/neg are `mov` with modifier
0x40000000/0x80000000. PTX `add.cc`/`addc` become `add` (t26 result) and op
0x7 (carry from that value); the 64-bit splitter's op 0x5 carries through a
predicate instead. div/rem/rcp/sqrt/ex2/lg2 are already expanded (25-440 ops)
when the first phase runs; opcodes seen only there (0x5d, 0xa4, 0xa8, 0xb4,
0x9f, 0x6f, ...) remain unnamed, as do structural ops 0x48, 0x61 (label),
0x34, 0x36, 0xbc.

**Registry-driven corpus.** `research/ptxas/ori_table_corpus.py TSV OUT` builds
kernels from ptxas's PTX instruction registry (`instruction_table.tsv`, 1,410
forms of 268 PTX opcodes, from GrigoryEvko/crucible-notes): every register
operand is loaded and stored, missing modifiers are found by trial compile.
368 forms (127 PTX names) compile; the run confirmed all earlier names and
added 20, for 79 in `ori_opnames.json`. That registry's `index` is a third
numbering (PTX parser IDs, e.g. add 0x30, mov 0x6e), and no static array maps
it to ORI opcodes.

**libnvptxcompiler_static.a** (same CUDA 13.0 install) has hashed symbol names
(`libnvptxcompiler_static_<sha1>`) but each of its 392 members keeps its source
file (STT_FILE): 128 `ori_*.cpp` (per architecture, `ori_mercury_converter_sm*`,
`ori_expand`, `ori_knobs`, `ori_display`), `cop_*` (DAG front end), `mercury_*`,
`finalizer_*`, `elfw_*`, `ptx_parser.c`, ... The executable keeps only two
source names. `ori_display.cpp` holds only small inline stubs and a vfprintf
wrapper (no ORI printer). The library also contains the old COP/DAG IR printer
(`<<< UNKNOWN DAG_OP=%s >>>`, ARB-style opcodes), absent from the executable.
Byte matching library functions to the executable fails (0 of ~58,000): the
library uses frame pointers and PIC, so a function-to-source map needs
structural matching (e.g. Ghidra BSim / Version Tracking).

**Where ORI is built.** All initial ORI instructions are created, before the
first phase, by `build_ori_for_function` (`0xc173e0`, 17 KB) and the per-
instruction selector `select_ori_instructions` (`0xc0eb10`, 34 KB), called
from `run_kernel_backend` (`0x7fbb70`), which then calls `run_optimizer_phases`
(`0x7fb6c0`) -> `dispatch_phase_list` (`0xc64f70`). The selector's input is
an intermediate IR with its own 222-opcode enum (opcode at node+8, `< 0xde`),
not PTX parser IDs and not SASS. `dag_opcode_to_ori_opcode` (`0x22b4b60`,
`ushort[222]`) maps 127 input opcodes one-to-one to ORI opcodes (118 distinct);
0 entries are lowered by the selector's switch. Verified on `sub_u64`: input
60 -> 0x120 st, 24 -> 0xbc ret, 71 -> 0x82 mov, 74 -> 0xc addr; inputs 44, 38,
95, 41 (param loads, sub) have 0 entries. The table is the third-party wiki's
`isel_slot` column; its SASS names for those 222 indices do not fit the input
(the parameter loads trace as F2F_X, POPC, BREAK, STS, CALL), so the wiki's
names for that enum are wrong.

**The input IR is the COP DAG.** Its nodes are built before
`build_ori_for_function`, on the front-end path `compile_ptx_module` ->
`compile_ptx_function_unit` -> `compile_ptx_function_backend` ->
`lower_ptx_function_definition` -> `lower_ptx_function_body_to_ocg` ->
`consume_ptx_body_event` -> `lower_ptx_instruction_to_ocg` (`0x62e890`,
118 KB, one PTX instruction at a time; the same path handles `.pragma`) ->
`cop_dag_create_node` (`0xa2f6d0`: allocates a 0xb8-byte node, opcode at +8,
sequence id at +0x24, links it into a per-function list) -> per-class
constructors (`0xae7b20`, `0xae7c60`, `0xae7d40`, vtables `0x229e558`,
`0x229e608`, `0x229e6b8`). String-anchored matching
(`research/ptxas/lib_functions.py` + `match_by_strings.py`, 826 pairs over
132 source files) places `lower_ptx_instruction_to_ocg`,
`consume_ptx_body_event` and `handle_knob_pragma` in `ptxOptimize.c` (8, 1 and
4 unique shared strings), `cop_dag_create_node` in `cop_dag_interface.cpp`
(call propagation), `build_ori_for_function` in `ori_construct_inst.cpp` and
`dispatch_phase_list` in `ori_dispatch.cpp`. Pipeline: PTX parser
(`ptx_parser.c`) -> PTX IR (`ptxIR.c`) -> COP DAG (`ptxOptimize.c` via
`cop_dag_interface.cpp`) -> ORI (`ori_construct_inst.cpp`, table + selector)
-> 157 ORI phases -> Mercury -> SASS.

**COP DAG printer.** `research/ptxas/dag_print.py` (gdb) records the DAG
context passed to `cop_dag_create_node` and, at `build_ori_for_function`,
walks its node list (`ctx+0x478`, nodes linked through `+0x48`). Node: +0
vtable (class), +8 opcode, +0x18 type (ORI codes), +0x20 basic block, +0x24
id, +0x99 operand count; operands are 0x28-byte records from +0xa8 {vtable
0x229e468, type, -, source node at +0x18, component select at +0x20, default
0xff03020100}; constant nodes (vtable 0x229e500, op 38) keep {kind, value} at
+0xa8, symbol nodes (vtable 0x229e558) a symbol pointer. Names: NVIDIA's
switch in `cop_nv_common.cpp` names 103 opcodes (`research/ptxas/dag_opnames.json`,
with the 127 DAG->ORI mappings); its 223-entry fallback table holds only
"???" in this build, even at run time. Observed roles (not named by NVIDIA):
38 constant, 41 memory-space symbol, 43 PTX register/variable, 44 param-space
symbol, 45 special register, 95 symbol+offset access (parameter load or
address), 8 branch target label. The DAG still refers to PTX registers (op 43)
across blocks rather than being fully SSA.

**No ORI name table.** The release binary has no string table indexed by ORI
opcode. SASS-level name tables exist: the static 773-entry table at
`0x29fe300`, and runtime InstructionInfo tables at `+0x1058` (constructors
`0x7a5d10`, `0x7c5410`, `0xbe7390`; the third-party wiki's "IR opcode enum")
and `+0x3060` (`0x7cb560`, `0x896d50`), built during `ScheduleInstructions`.
Read live after scheduling, none names the IR's opcodes (e.g. ORI 0x5, the
UIADD3 carry add, reads SGXT / F2I; 0x82 mov reads HSET2 / LDT). No lowercase
or ROT13 PTX-style opcode strings exist.

ORI opcodes have their own numbering, not the 773-entry SASS name table.
Earlier evidence: 2 add, 5 add-with-carry (`UIADD3`/`.X` after allocation,
carry in a class-2 vreg = UP0), 0x82 mov (with a memory modifier, a load),
0x120 st, 0xb7 four-register constant load (LDCU.128), 0xc3 descriptor/
constant load (LDCU.64 desc, LDC R1), 0x109 move to a pair half.

## ConvertUnsupportedOps vs MercConverter (2026-10-09)

Phase numbers come from the execute jump table at `0x22bbeb8` (indexed like
the name table `0x22bd0c0`); a phase object's vtable slot 0 is `execute`.
Two distinct conversions exist:

- **Phase 5 ConvertUnsupportedOps** (vtable `0x22bd690`, execute `0xc60a20`):
  ORI→ORI legalization before register allocation (phase 122). It tail-calls
  arch object `ctx+0x640` slot 0; gdb on sm_100 resolves it to
  `convert_unsupported_ops_sm100` (`0x7060a0`). That calls
  `convert_unsupported_ops_generic` (`0xaed3c0`, one switch over ~110 ORI
  opcodes, emitting replacement ORI through `0x92e720`/`0x934630`, removing
  originals through `0x9253c0`). It then applies SM100 fixes (memory-op flags on
  0xf3/0x46, special/system-value operands rewritten into ORI 0xab/0xc9).
- **Phase 141 MercConverter** (vtable `0x22bebd0`, execute `0xc60300` ->
  `run_merc_converter_phase` `0x9f3760`): ORI→Mercury target classes after
  allocation and scheduling; it logs "After MercConverter". It is gated by
  `ctx+0x570` bit 0x10 and builds a converter by arch code
  `(ctx+0x630)+0x174` (0x6001, 0x7001-0x7005, 0x8000, 0x9000-0x9005; gdb:
  sm_100 = 0x9000, vtable `0x21f73f0`; sm_120 = 0x9004, vtable `0x229c670`).
  For each instruction, `merc_convert_instruction` (`0x9f3340`) optionally runs
  `convert_ori_instruction_sequence` (config+0x576 bit 0x20, set on both),
  queued post-lowering `0x9ef5e0`, then `dispatch_ori_opcode_conversion`.
  Cases call shared handlers or converter vtable slots (add family 2/3/4/5/7 ->
  slot 0, 0x82/0xa9 -> 0xe8, 0xb7/0x120 -> 0x120), or start a class directly
  (0x94 -> 0x2d NOP). Failure starts class 0xffff. Builder classes are the
  finalizer directory indices (IADD3 family 0xc, IMAD 0x20, NOP 0x2d).

Later Merc phases: MercEncodeAndDecode (142), MercExpandInstructions,
MercGenerateWARs1/2, MercGenerateOpex, MercGenerateSassUCode. Whether 142 is the
intermediate-ELF round trip is unverified.

## BMSK mode and functional semantics (2026-10-11)

The SASS md's `BMSK` opcode was selected to trace one operation from compiler
lowering through its final encoding, then implement its scalar P-code.

`dispatch_ori_opcode_conversion` (`0x9ed2d0`) case `0x18` calls
`lower_bmsk_operation_to_target` (`0x9d9f60`, renamed from `FUN_009d9f60`).
The handler starts target class 1, sets type selector 10 through builder slot
`+0x170`, and transfers destination operand 0 to role 6, position operand 1 to
role `0xd`, and width operand 2 to role `0x14`. The final descriptor's low bit
is passed to builder slot `+0x218`: clamp 0, wrap 1. A live breakpoint hit this
path for SM100 compilation; it was not hit for the SM89 probe.

The SM100 finalizer's class-1 row at `0x2339760`, keys `00/05`, calls
`encode_sm100_BMSK_variant_00_05` (`0x1a0eeb0`). The encoder starts opcode
`0x21b`, puts Rd at bits 16..23, position at 24..31, width at 32..39, and mode
at high-qword bit 11, which is **instruction bit 75**. This matches the md
assignment `BITS_1_75_75_relu=cw`; `relu` is merely the shared field's name.
`CWMode` defines `C=0`, `W=1`.

The committed probes and complete captures are
`tests/ptx/bmsk/bmsk_clamp.ptx` / `bmsk_clamp_finalizer.jsonl` and
`tests/ptx/bmsk/bmsk_wrap.ptx` / `bmsk_wrap_finalizer.jsonl`. Each capture's 24 words (384
bytes) was checked against the cubin produced by that same SM100 compiler run
using `validate_capture.py`. The arithmetic words, in little-endian byte order:

| Mode | Word | Disassembly |
|---|---|---|
| Clamp | `1b720500050000000000000000ca0f00` | `BMSK R5, R0, R5` |
| Wrap | `1b720500050000000008000000ca0f00` | `BMSK.W R5, R0, R5` |

Their XOR is exactly `1 << 75`. The dedicated SM89 CUDA fixtures also differ
only at that bit. `tests/bmsk_semantics.py` checks this directly.

The [PTX bmsk specification](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html#integer-arithmetic-instructions-bmsk)
defines position and width, and clamp versus count masking. Wrap masks both
counts with 31; it does not rotate mask bits past the end of the word.
Clamp position >=32 gives zero, width >=32 fills from a valid position to
bit 31, and zero width gives zero. For example, position 32 and width 1 gives
0 in clamp mode and 1 in wrap mode.

`sass/operations.py::emit_BMSK` now emits these operations as native P-code.
Register, uniform-register, immediate and ordinary constant-bank forms are
supported. The SM75-SM90 `bmsk__RCxR_RCxR` form prints, for example,
`BMSK R8, R2, cx[UR4][0x10]`; its address reads a uniform-register pair plus
an offset. Its bank/address interpretation is unresolved and remains opaque.
The coverage reason refers to this P-code limitation, not compiler support.
SM100+ has three BMSK classes, all supported. UBMSK is outside this change.

`tests/bmsk_semantics.py --require-gpu` compiled two SM89 whole kernels,
confirmed their BMSK modes, and compared 1,504 vectors per mode against both
GPU results and whole-kernel Ghidra P-code. All 3,008 comparisons passed,
including every position/width pair from 0 through 33, large unsigned counts,
and random inputs. The reference enumerates selected bits independently of
the P-code's shifts. Native P-code tests also cover alternate operand forms,
aliasing, zero registers, discarded results and predicates across all nine
architectures in both exact and readable languages. Hardware evidence is
limited to SM89; the compiler trace is SM100.

## BMSK CX addressing (2026-10-11)

Follow-up to the ordinary BMSK investigation above. The earlier opaque CX
status was a limitation of our operand-value implementation. It is superseded
for BMSK by the native load described here; other CX opcode families are not
changed.

NVIDIA's [CUDA-GDB bindless constants documentation](https://docs.nvidia.com/cuda/cuda-gdb/index.html#bindless-constants)
describes a 64-bit uniform-register header plus a byte offset, and the
`$_cuda_bindless_const(header, offset)` address resolver. The header contains
an address and region size, rather than an ordinary unencoded pointer.
[Mesa's descriptor definitions and packing/unpacking helpers](https://github.com/chaotic-cx/mesa-mirror/blob/27434d78dd5422a57b97f616ceeaaa8328bee0b9/src/nouveau/vulkan/nvk_descriptor_types.h)
give two layouts:

| Architectures | Base | Size |
|---|---|---|
| SM75-SM89 | `(header & ((1 << 45) - 1)) << 4` | `(header >> 45) << 4` |
| SM90 (Hopper) | `(header & ((1 << 51) - 1)) << 6` | `(header >> 51) << 4` |

The SM89 BMSK CX md uses `URb` as a 64-bit header, while `Sb` is a loaded
32-bit width. Its offset field at bits 40..53 encodes byte offset/4; URb is
at bits 32..37. Instruction bit 75 still selects clamp/wrap.

`tests/kernels/bmsk_cx_semantics.cu` compiles a wrapper with an observable
BMSK result and four BAR slots. The oracle pins its expected register
allocation, replaces the BARs with ULDC.64 / two USHF operations / ULOP3 to
construct the header in UR0/UR1 from the input pointer, replaces BMSK with
`BMSK[.W] R7, R2, cx[UR0][offset]`, and replaces the old result XOR with NOP.
UR4/UR5 retain the wrapper's global-memory descriptor. The six patched words
use WAIT15_END_GROUP with all scoreboard waits; every replacement is decoded
and validated before execution. This is a **manually patched SASS** fixture,
not evidence that ptxas produces the CX form from the supplied PTX.

SM89 execution established that the CX width reads the backing CUDA global
allocation at `base + offset`. Outside the declared region it reads zero,
including size zero, the exact region boundary and the maximum encoded
offset. Tests also exercise high size bits to distinguish address and size.
The reference enumerates output mask bits independently of the P-code.

`sass/semantic.py::Builder.compound` now supplies this bounded 32-bit `ram`
load specifically for BMSK CX on SM75-SM90. SM90 uses the Hopper layout at
the user's request, with emulator validation but **no Hopper hardware pass**.
Zero-on-out-of-bounds on SM90 follows the SM89 behavior and remains pending
hardware validation there. The model covers value semantics, not constant
cache behavior or coherence. Other CX instruction families remain unchanged.
Reproduce focused values with `tests/bmsk_cx_semantics.py --arch SMxx`, and
GPU plus whole-kernel P-code with `--require-gpu` (SM89). Measured results are
recorded separately under `bmsk_cx` in the semantics verification manifest.
All 64 patched fixtures (24 count fixtures and 40 bounds fixtures) passed
8,192 GPU/reference comparisons and 8,192 whole-kernel P-code comparisons
in each SM89 language variant. All ten SM75-SM90 languages compiled without
unused temporaries and passed 446 focused CX cases each. Full SM89 semantics
passed 14,027/14,027 exact and 9,197/9,197 readable cases. Register dataflow
and control-flow checks passed on all five affected architectures in exact
mode and on SM89 readable, using synthetic words plus 2,000 real samples.

### Where ptxas selects CX

For the CUDA 13.0.88 SM89 final emission path, Ghidra function
`select_bmsk_family_sass_emission_form` (`0xc4df70`) routes BMSK width operands
to emission form 8 when the third operand's modifier word has bit 24 set, the
referenced metadata record has subtype 4 in bits 10..12, and the source
register metadata is eligible. In the ORI layout, +0x64 is this operand's value word and +0x68 is its
modifier word; calling +0x68 an “extension word” was incorrect. Bit 24 belongs
to the internal memory-space modifier encoding (0x0100000N), not a separate
source-extension field. That branch resolves the allocated header register,
calls `encode_sass_scaled_offset_fields` (`0xaae660`) to produce the CX byte
offset divided by four, then calls `pack_sass_BMSK_CX` (`0x1c746b0`). The
packer writes the header register at bits 32..37, offset/4 at 40..53, and mode
at bit 75. Its shared field resolver is `resolve_sass_constant_source_fields`
(`0xa3b7b0`); classification 4 leaves the ordinary constant-bank field zero.
This establishes what the late branch tests. A generic operand conversion helper,
`FUN_00c2f140`, also has paths that OR bit 24 into an output modifier word for
emitter modes 5, 8, and `0xd` when the incoming modifier is negative. That is
not yet connected to the BMSK CX path or to a user PTX construct.

Live SM89 GDB traces hit the selector for two ordinary BMSK PTX probes. A
large kernel parameter at byte offset 8192 selected form 1, with an ordinary
`c[0][0x2a80]` source. A dynamic `ld.const` through a pointer selected form 6,
with the load materialized into UR4 before BMSK. Neither selected form 8.
The exact traces and PTX/cubin outputs are temporary files under
`/tmp/ptxas-cx-selection/`; the PTX source is reproducible from those files,
and the compiler build is the CUDA 13.0.88 SHA256 pinned at the top of this
note. These negative probes constrain the search; they do not prove no PTX
input can trigger CX.

## Remaining investigation

The intermediate ELF decode bridge and add/sub/wide-multiply conversion paths
are now traced. Extend this evidence to multiply-add/high/carry forms, memory
descriptors and operations that still have opaque semantics. In particular,
high-word addend placement is traced above, but alternate mode, signed forms
and overflow/carry conventions need more direct evidence. The encoder handlers establish final **encoding**; value semantics
need lowering evidence and independent execution checks.
The earlier `0x18f1be0` dispatcher writes the intermediate representation;
it is a distinct stage. ORI's acronym expansion remains unestablished.

The maintained capture script and complete probe JSONLs are in
`research/ptxas/`. rr recordings and compiled probe cubins currently live in
`/tmp`; rebuild cubins with the recorded CUDA13.0.88 compiler before rerunning
the checks. Earlier temporary dispatch logs omitted the first LDC and are
superseded by the complete captures.

### PTX-side search for a BMSK CX trigger (2026-10-11)

The searched PTX constructs have not produced a CX word. This matters because
PTX `bmsk` exposes only register inputs (`bmsk.{clamp,wrap}.b32 d, a, b`);
its operand matcher rejects a direct memory expression such as
`[huge+32768]` with “Arguments mismatch for instruction 'bmsk'”. The CUDA
13.0 CCCL generated wrapper likewise emits a register-constrained PTX BMSK
call.

The committed negative probe is `tests/ptx/bmsk/cx_candidate_indirect_const_no_cx.ptx`. A kernel parameter annotated as a constant-space pointer is accepted using
`.param .u64 .ptr.const.align 16 ptr`. Loading through it with `ld.const`
produces `LDC.IL R0, c[0][R0]` followed by `BMSK R5, R5, R0`, not CX. A
live GDB trace of `select_bmsk_family_sass_emission_form` for this probe
reported width value word `0x1000004a`, modifier word zero, and selected
form 0. The unannotated version instead used `ULDC` and a uniform source
register (selected form 6). A plain `ld.global` width likewise remains a
separate `LDG` followed by register-source BMSK. Direct `.const` symbol loads,
large parameter loads, and the earlier dynamic `ld.const` probe also remained
ordinary constant-bank or register BMSK operands.

The cached real-cubin scan found no CX-encoded instructions among 4,334 cubins
for SM75/80/86/89/90. This scan is bounded by that corpus and does not prove
the encoding is unreachable. The current evidence therefore does **not**
identify a PTX construct that emits BMSK CX. The code path's requirements
(memory-space modifier bit 24 plus metadata subtype 4) remain a late selector
condition, not a source-level recipe. One unresolved possibility is an
internal PTXAS lowering that does not correspond to a user-expressible PTX
BMSK operand; the direct PTX memory form is not accepted.


The modifier setter search found `FUN_00c2f140` (`0xc2f140`). Its bit-24
writes at `0xc2f353`, `0xc2f3cb`, and `0xc2f41b` are conditional on the input
modifier's negate bit and emitter modes 5, 8, or `0xd`. PTXAS rejects inline
negation on either BMSK source; a separate `neg.s32` feeding BMSK remains a
separate `IMAD.MOV ..., -R` followed by ordinary `BMSK`. This generic setter
does not explain how BMSK acquires the bit-24 condition.

A phase-boundary ORI trace of the annotated-pointer probe (`ORI_PHASES=all`)
shows the constant load remains a separate `mov` with a memory modifier, while
BMSK consumes the loaded virtual register. No optimizer phase attaches the
memory modifier to BMSK for this input. This ties the negative selector trace
to the upstream representation: the loader is classified as a memory operand,
but the BMSK width is an ordinary register operand before register allocation
and remains so through `CalcRegisterMap`. The metadata record at the selector's
modifier-derived index is null because the modifier word is zero.

### CX trigger search, round 2 (2026-10-11)

No PTX input produced a `cx[` operand. Negative results (CUDA 13.0.88 and 13.4.92 ptxas, sm_75-120):
~480 ALU/memory variants (`ld.const`, `ldu`, `ld.global.nc`, `.ptr.const` params feeding ALU ops), 175 tex/suld/sust/txq
variants, the ~2,800-file concolic corpus on sm_89/90 (zero hits at any of the 35 final-emission `descriptor
subtype == 4` checks, `0xc4xxxx-0xc5xxxx`), `ld.const` offset/width sweeps (168 files, no subtype-4 descriptor created).

Findings:
- Plain `ld.const [ptr+imm]` becomes bank-3 indexed constant access: SM89/90 `ULDC UR, c[0x3][UR+imm]`, SM100+
  `LDC R, c[0x3][R+imm]` / `LDCU UR, c[0x3][UR+imm]`. Bank 3 + index, not a bindless header.
- The late selectors test an ORI operand whose modifier word has bit 24 set and whose pool entry (`func+0x98`, created by
  `FUN_00925bb0` and the `FUN_009262a0` family; kind 0x10 const-ref descriptors) has `flags>>10 & 7 == 4`. `FUN_009262a0`'s
  5th argument is that subtype. The only caller passing 4 is the address legalizer `FUN_013a78b0` (0x13a78b0), at the
  residual-offset path that emits ORI op 0xb8; `FUN_007dead0` is the predicate for it. The two-source form selector
  `FUN_00c47220` picks forms 8/9 (CX) from it. No PTX construct reaching that path was found.
- Hidden OCG builtins are callable from PTX as functions: `__nv_ptx_builtin_ocg_<op>_<subops>_<type>` (e.g. `ldc_u32`,
  `cctl_c_{ldc|ldcu}_{shallow|deep}_{iv|ivall}`). Lowerers: `FUN_006bec60` (LDC, bank must be an immediate; emits ORI `ld`
  0xb7) and `FUN_006c5a40` (CCTL.C, builds a register + fresh class-4 vreg modifier operand). sm_89: `CCTL.E.C.IV [UR4]`
  (no CX). sm_90: CCTL dropped. SM100/120: every `ocg` call ICEs in Mercury conversion (`merc_convert_instruction`).
- SM100+ CX exists only for LDC/LDCU/CCTL. LDC encoder variants 02/03 (`0x12b7100`) and 03/03 (`0x1347320`) pack the
  `ldc_ur` classes; ordinary programs only reach 01/03 (`0x1a71e40`).
- Slang's CUDA backend maps `ConstantBuffer` to plain pointers and emits no `ldc`/`ocg` calls.
- `--query-controls`/`--apply-controls` files are encrypted; knob pragmas (`next knob`, `func knob`, ...) are present but
  reported `unsupported` in release builds.
- Tooling: gdb stalls on debuginfod downloads; run with `DEBUGINFOD_URLS=` (0.2 s per ptxas run instead of timing out).

### CX trigger search, round 3 (2026-10-11)

- Descriptor pool (`func+0x98`, interned by `FUN_00925bb0` = `intern_ori_descriptor_record`): `flags>>10 & 7` is a subtype. Only two writers exist in
  13.0 and 13.4: `FUN_0091c570` (`build_descriptor_key_from_symbol`) sets subtype 1 for memory-space id 0x160 (`.ptr.const` params), and
  `FUN_009262a0` (`make_const_ref_descriptor`) takes it as argument 5. The only caller passing 4 is the legalizer `FUN_013a78b0`, which runs that path
  only for operands that already have a subtype-4 descriptor (`FUN_007dead0`). Across the ~2,800-file corpus and my probes, subtypes seen: 0 and 1 only.
  A scan of both binaries finds a single `and $0xffffe27f` site, so 13.4 adds no new writer.
- `.ptr.const` lowering is per-target: sm_75-89 `LDC.IL R, c[0x0][R]` (subtype 1), sm_90+ `LDG.E.CONSTANT desc[UR][R.64]`. Plain `ld.const [p+imm]` is
  bank 3 plus index on every target, including sm_107/110/121 with 13.4.
- Mercury side: final SM100 LDC encoder rows are keyed by (variant byte +0xe, +0xf) = (0..3, 3). `01/03` (`0x1a71e40`) is the bank form; `02/03` and `03/03`
  (`0x12b7100`, `0x1347320`) write URa at bit 24 and Rb at bit 64, i.e. the `ldc_ur` (CX) classes. Variant bytes are written by `0xb28f10`/`0xb28f20` from a
  large shape-matching converter (`FUN_0198bcd0`); an rr reverse-watchpoint on a normal LDC shows `(1,3)` set at 0x19af9f8.
- Tooling: `rr record --bind-to-cpu=0` then `rr replay -s PORT -k` plus `gdb -batch -ex 'target remote :PORT'` works for reverse watchpoints.

### Forced CX emission from ptxas itself (2026-10-11)

`research/ptxas/force_ldc_cx.gdb` redirects the SM100 LDC row-`01/03` encoder to the row-`02/03` (`ldc_ur`) handler for the first LDC of
`force_ldc_cx_input.ptx` (sm_100). The cubin then decodes (13.0 nvdisasm) as `LDC R1, cx[UR0][0x37c]`; hits 2 and 3 give
`LDC.64 R2, cx[UR0][0x380]` and `LDC R4, cx[UR0][0x388]`. So ptxas's own packer for `ldc_ur` yields a real CX operand from ptxas
output, but it needs a debugger patch: no PTX-only input selecting that row has been found. The URa field is not set up (UR0), so the
value is not meaningful.
