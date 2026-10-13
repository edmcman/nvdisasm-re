# F2FP scaled upconverts: iswzC and scale lanes (2026-10-13)

Question: which bytes of Rc hold the UE8M0 scales of `f2fp_8b_upconvert_scale` /
`f2fp_4b_upconvert_scale` (SM100+), given the md's `/ISWZC:iswzC` on Rc (H0, H1, B0-B3; 6 and 7
invalid)? The earlier emitter ignored iswzC and took the scale half from `extract_B`.

## Compiler evidence (ptxas 13.3, `-arch=sm_100a`, PTX 9.3; disassembled with nvdisasm 13.4)

| PTX | SASS |
| --- | --- |
| `cvt.rn.bf16x2.ue8m0x2 r3, h1;` | `F2FP.BF16.E8.UNPACK_B R5, R0` |
| `cvt.rn.scaled::n2::ue8m0.bf16x2.e2m1x2 r3, q1, h3;` | `F2FP.BF16.E2M1.UNPACK_B R5, R0, R5.H0` |
| `cvt.rn.scaled::n2::ue8m0.bf16x2.e4m3x2 r3, h2, h1;` (h2:h1 = r1) | `F2FP.BF16.E4M3.UNPACK_B R5, R0.H1, R0.H0` |
| `cvt.rn.scaled::n2::ue8m0.bf16x2.e4m3x2 r3, h2, h2;` | `PRMT R0, R0, 0x7632, R0` then `F2FP.BF16.E4M3.UNPACK_B R5, R0.H1, R0.H0` |
| `cvt.rn.satfinite.relu.scaled::n2::ue8m0.bf16x2.e2m3x2 r3, h1, h3;` | `F2FP.SATFINITE.RELU.BF16.E2M3.UNPACK_B R5, R0, R5.H0` |
| `cvt.rn.scaled::n2::ue8m0.bf16x2.s2f6x2 r3, h1, h2;` | `F2FP.BF16.S2_6.UNPACK_B R5, R0, R5.H0` |

- The PTX scale-factor is Rc with iswzC = H0, independent of the source half: `extract_B = H1`
  with `iswzC = H0` reads the scales from R0's low half. The earlier emitter's rule (scales from
  the `extract_B` half) is wrong for that encoding.
- ptxas never encoded H1 or B0-B3: a scale pair in a high half was moved to the low half by PRMT.
  PTX has no way to ask for them (upconverts only take a `.b16` pair, `.scaled::n2::ue8m0`), so
  their meaning is unknown and they stay opaque.

## PTX ISA (cvt, 9.7.10.x)

- "Inputs are converted to destination type and then multiplied by scale-factor. The scaling
  factor of upper 8 bits of a is stored in the upper 8 bits of operand scale-factor and the
  scaling factor of lower 8 bits of a is stored in the lower 8 bits" (4-bit form: upper/lower 4
  bits). Upper source element -> upper half of d.
- S2_6 downconvert: "inputs are divided by scale-factor and then conversion is performed"; a / the
  upper BF16 with scale-factor[15:8], b / the lower with [7:0].
- Not specified: the result for a NaN scale (UE8M0 0xff). Modelled as BF16 NaN 0x7fff, the BF16
  canonical NaN of the F2FP packs; no SM100 hardware check.

## Attempts to make ptxas encode iswzC H1 or B0-B3 (all gave H0)

- Scale in the high half of a live register (no dead register to PRMT into): a fresh register,
  `F2FP.BF16.E4M3.UNPACK_B R7, R5, R7.H0`.
- Scale pair built from one source byte (`(b & 0xff) * 0x101`, `prmt 0x2222`): `R0.H0`/`R5.H0`.
- Immediate scale `0x7f80`: the F32Imm form, printed `4.5738381875562029195e-41.H0`.
- Scale from the high half of a 32-bit kernel parameter: `LDCU.U16 UR6, c[0x0][0x38a]` then
  `F2FP.BF16.E4M3.UNPACK_B R5, R0, UR6.H0` (a 16-bit load of just that half).

ptxas never emits the other iswzC values from PTX; their meaning needs SM100 hardware.

## ptxas 13.0 internals (Ghidra, `/usr/local/cuda/bin/ptxas`, non-PIE, addresses = objdump)

- The register-form scaled encoders (opcode 0x63e; also 0xa3e/0xe3e) write bits 26-24 as
  `FUN_010b6340(FUN_010bf2b0(operand 2))`: `FUN_010bf2b0` returns `((op+0x18) >> 21 & 7) + 0x6c`,
  `FUN_010b6340` maps 0x6d..0x72 through the table at 0x22fd410 (0, 1, 2, 3, 4, 5), else -1. So
  iswzC = s - 1 for ptxas's 3-bit operand sub-register selector s (1..6), and only the scaled F2FP
  encoders read this attribute through that getter (20 call sites).
- s is set by generic operand construction (`and $0xff1fffff; or $0x200000|$0x400000|$0x800000`
  at 0x8ea673, 0xaecae5, 0x8775ab, ...; a setter at 0x917f18 maps modifier ids 0xf6..0xfa to 1..5).
  The md names iswzC 0..5 H0, H1, B0..B3: ptxas's selector is the generic half/byte register view.
- `extract_B` (bit 88) is a different attribute: `((op+0x18) >> 11 & 3) + 0xc`, 0xd -> 0 (H0),
  0xe -> 1 (H1) (`FUN_010bf280`, `FUN_010b6090`).
- Conclusion: H1 is ptxas's "high half of Rc" view (scale pair Rc[31:16]), by naming and IR, not
  by compiler output or the PTX text; B0..B3 select a byte, how one byte scales two lanes is
  unknown. Both stay unmodelled.
