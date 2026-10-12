"""Predicate transfers: UP2UR (uniform predicates into a uniform register) and P2R/R2P."""
from tests.harness.case import put, source


def emulator_cases(c):
    # CuLifter's immediate UP2UR form packs the selected UP0..UP6 bits over URa.
    for mask in (0, 1, 0x55, 0x7f, 0xff):
        base = 0xa5a50000
        c.add("up2ur__Imm", {"UPg": "UPT", "URd": 8, "UPR": "UPR", "URa": 4, "Sb": mask, "insert": "B0"},
              {"UR4": base, **{f"UP{bit}": (mask >> bit) & 1 for bit in range(7)}}, {"UR8": base | (mask & 0x7f), "__native": True})
    # A false uniform guard leaves the destination untouched.
    c.add("up2ur__Imm", {"UPg": 6, "URd": 8, "UPR": "UPR", "URa": 4, "Sb": 0x7f, "insert": "B0"},
          {"UR4": 0x12345678, "UR8": 0xdeadbeef, "UP6": 0, **{f"UP{i}": 1 for i in range(6)}}, {"UR8": 0xdeadbeef, "__native": True})
    # P2R/R2P pack/unpack the seven writable predicates. Mask bits above 6 are ignored and
    # R2P preserves every unselected predicate.
    for op in ("P2R", "R2P"):
        for k in c.of(op, cx=None):
            mask_name = source(k, "Rb", "URb", "Sb")
            if not mask_name or any(atom.type in ("C", "CX") for atom in k.operand_types.values()): continue
            for mask in (0, 1, 0x25, 0x7f, 0xff):
                fields, regs = {"Pg": "PT", "Ra": 4}, {}
                put(k, mask_name, mask, fields, regs, None, 6, imm=(k.operand_types[mask_name].type,))
                for byte in range(4):
                    if op == "P2R":
                        base, shift = 0x12345678, byte * 8
                        fields.update(Rd=2, Pr="PR", insert=f"B{byte}")
                        regs.update({f"P{i}": (0x55 >> i) & 1 for i in range(7)}, R4=base)
                        c.add(k.name, dict(fields), dict(regs), {"R2": (base & ~(0xff << shift)) | ((0x55 & mask & 0x7f) << shift), "__native": True})
                    else:
                        initial, src = {f"P{i}": i & 1 for i in range(7)}, 0xa1b2c354
                        fields["a_bsel"] = f"B{byte}"
                        regs.update(initial, R4=src)
                        c.add(k.name, dict(fields), dict(regs),
                              {**{f"P{i}": (src >> (byte * 8 + i)) & 1 if mask & (1 << i) else initial[f"P{i}"] for i in range(7)}, "__native": True})


def gpu_fixtures():
    """Patched carry-kernel slots: P2R/R2P after ISETPs establish P0..P6 = 0b1010101; UP2UR after
    UISETPs set selected uniform predicates."""
    from tests.harness.gpu import sm89
    from tests.harness.templates import slot_fixture
    data, nop = [(i, 0, 0) for i in range(128)], sm89("nop_")
    init = [sm89("isetp__RRR_RRR_noEX", Pu=lo, Pv=hi, Pp="PT", Ra="RZ", Rb="RZ", icmp="EQ", fmt="U32", bop="AND") for lo, hi in ((0, 1), (2, 3), (4, 5), (6, "PT"))]
    p2r = lambda ra, mask, insert: sm89("p2r__RsIR_RIR", Rd=11, Pr="PR", Ra=ra, Sb=mask, insert=insert)
    r2p = lambda ra, mask, bsel: sm89("r2p__RIR", Ra=ra, Sb=mask, a_bsel=bsel)
    mov = lambda value: sm89("mov__RI", Rd=4, Sb=value, PixMaskU04=15)
    out = [slot_fixture(f"p2r_mask_{m:08x}", init + [p2r("RZ", m, "B0"), nop, nop, nop], data, [0x55 & m & 0x7f] * 128, 0x140)
           for m in (0, 1, 0x15, 0x2a, 0x55, 0x7f, 0xff, 0x80000055, 0xffffffff)]
    # The selector chooses the destination byte that receives the packed bits.
    out += [slot_fixture(f"p2r_b{b}", [mov(0x11223344)] + init + [p2r(4, 0x7f, f"B{b}"), nop, nop], data,
                         [(0x11223344 & ~(0xff << 8 * b)) | (0x55 << 8 * b)] * 128, 0x150) for b in range(4)]
    # Clear selected bits from the initialized pattern, then pack all seven back.
    out.append(slot_fixture("r2p_mask_15", init + [r2p("RZ", 0x15, "B0"), nop, p2r("RZ", 0x7f, "B0"), nop], data, [0x40] * 128, 0x140))
    # The selector chooses which source byte supplies the selected predicate bits.
    out += [slot_fixture(f"r2p_b{b}", [mov(0xa1b2c354)] + init + [r2p(4, 0x7f, f"B{b}"), nop, p2r("RZ", 0x7f, "B0")], data,
                         [(0xa1b2c354 >> 8 * b) & 0x7f] * 128, 0x150) for b in range(4)]
    bits = 0b1011010
    for mask in (0, 1, 2, 0x55, 0x7f, 0xff, 0x24, 0x80000024, 0xffffffff):
        remaining, prep = [b for b in range(7) if mask & (1 << b)], [sm89("umov__UI", URd=4, Sb=0x100)]
        while remaining:
            bit = remaining.pop(0); value = bool(bits & (1 << bit))
            other = next((o for o in remaining if bool(bits & (1 << o)) != value), None)
            if other is not None: remaining.remove(other)
            prep.append(sm89("uisetp__URURUR_URURUR", UPu=bit, UPv="UPT" if other is None else other, URa="URZ", URb="URZ", UPp="UPT",
                             icmp="EQ" if value else "NE", fmt="U32", bop="AND"))
        prep += [nop] * (6 - len(prep))
        out.append(slot_fixture(f"up2ur_mask_{mask:02x}", prep + [sm89("up2ur__Imm", URd=5, UPR="UPR", URa=4, Sb=mask, insert="B0"), nop], data,
                                [0x100 | (bits & mask & 0x7f)] * 128, 0x160, final=sm89("mov__RU", Rd=11, URb=5, PixMaskU04=15)))
    return out
