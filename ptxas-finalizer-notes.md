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
called by `dispatch_ori_opcode_conversion` (`0x9ed2d0`) during
`run_convert_unsupported_ops_phase` (`0x9f3340`).

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
The probe is checked in as `research/ptxas/one_add.ptx`; captured evidence is
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
ptxas -arch=sm_100 research/ptxas/add_u64.ptx -o /tmp/ptxas_add_u64.cubin
ptxas -arch=sm_100 research/ptxas/sub_u64.ptx -o /tmp/ptxas_sub_u64.cubin
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
`dispatch_ori_opcode_conversion` (`0x9ed2d0`) is a ~240-case switch on the
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
