"""Predicate transfers and logic: UP2UR (uniform predicates into a uniform register), P2R/R2P and PLOP3."""
import random
from itertools import product
from tests.harness.case import cx_header, is_cx, put, ram, source
from tests.refs.integer import p2r as p2r_ref, r2p as r2p_ref


def bits_of(packed): return {f"P{i}": packed >> i & 1 for i in range(7)}


def emulator_cases(c):
    # Immediate UP2UR merges the selected UP0..UP6 into URa's low byte as P2R does.
    for mask, base in product((0, 1, 0x2a, 0x55, 0x7f, 0x80, 0xff), (0xa5a50000, 0xffffffff, 0xa5a5a5a5)):
        c.add("up2ur__Imm", {"UPg": "UPT", "URd": 8, "UPR": "UPR", "URa": 4, "Sb": mask, "insert": "B0"},
              {"UR4": base, **{f"UP{bit}": (0x55 >> bit) & 1 for bit in range(7)}}, {"UR8": p2r_ref(0x55, base, mask, 0), "__native": True})
    c.add("up2ur__Imm", {"UPg": "UPT", "URd": 4, "UPR": "UPR", "URa": 4, "Sb": 0x2a, "insert": "B0"},
          {"UR4": 0xa5a5a5a5, **{f"UP{bit}": (0x55 >> bit) & 1 for bit in range(7)}}, {"UR4": p2r_ref(0x55, 0xa5a5a5a5, 0x2a, 0), "__native": True})
    # A false uniform guard leaves the destination untouched.
    c.add("up2ur__Imm", {"UPg": 6, "URd": 8, "UPR": "UPR", "URa": 4, "Sb": 0x7f, "insert": "B0"},
          {"UR4": 0x12345678, "UR8": 0xdeadbeef, "UP6": 0, **{f"UP{i}": 1 for i in range(6)}}, {"UR8": 0xdeadbeef, "__native": True})
    # PLOP3: two truth tables over the same three (optionally negated) predicates.
    lut = lambda table, p, q, r: table >> (4 * p + 2 * q + r) & 1
    for u, v in ((0xa8, 0), (0x2a, 0x80), (0xe0, 0x96), (0x6a, 0xff)):
        for p, q, r in product((0, 1), repeat=3):
            for nots in (0, 5):
                xs = [x ^ (nots >> i & 1) for i, x in enumerate((p, q, r))]
                fields = dict(Pu=0, Pv=1, Pp=2, Pq=3, uimm8=u, vimm8=v, **{f"{n}@not": nots >> i & 1 for i, n in enumerate(("Pp", "Pq"))})
                expected = dict(P0=lut(u, *xs), P1=lut(v, *xs), __native=True)
                c.add("plop3_lut_2out_", dict(fields, Pr=4, **{"Pr@not": nots >> 2 & 1}), dict(P2=p, P3=q, P4=r), expected)
                c.add("plop3_lut_2out_uniform_", dict(fields, UPr=4, **{"UPr@not": nots >> 2 & 1}), dict(P2=p, P3=q, UP4=r), expected)
                c.add("uplop3_lut_2out_", dict(UPu=0, UPv=1, UPp=2, UPq=3, UPr=4, uimm8=u, vimm8=v,
                                               **{f"UP{n}@not": nots >> i & 1 for i, n in enumerate(("p", "q", "r"))}),
                      dict(UP2=p, UP3=q, UP4=r), dict(UP0=lut(u, *xs), UP1=lut(v, *xs), __native=True))
    # P2R/R2P pack/unpack the seven writable predicates. P2R replaces the masked bits of the
    # selected byte (mask bit 7 clears bit 7, higher bits are ignored); R2P ignores mask bits above
    # 6 and preserves every unselected predicate. Masks come from every source form: a register,
    # an immediate, c[2][16], or a CX descriptor (one 16-byte unit at 0x1000) at byte 4.
    def mask_source(k, mask, units=1):
        fields, regs, memory = {}, {}, []
        if is_cx(k):
            fields.update(URb=10, Sb_offset=4); regs.update(cx_header(c.arch, 10, 0x1000, units)); memory.append(ram(0x1004, mask))
        else:
            name = source(k, "Rb", "URb", "Sb")
            put(k, name, mask, fields, regs, memory, 6, bank=(2, 16), imm=(k.operand_types[name].type,))
        return fields, regs, memory

    def add(k, mask, byte, rd, base, units=1):
        fields, regs, memory = mask_source(k, mask, units)
        loaded = mask if units else 0
        if k.mnemonic == "P2R":
            c.add(k.name, dict(fields, Pg="PT", Rd=rd, Ra=4, Pr="PR", insert=f"B{byte}"), dict(regs, **bits_of(0x55), R4=base),
                  {f"R{rd}": p2r_ref(0x55, base, loaded, byte), "__native": True}, memory=memory)
        else:
            c.add(k.name, dict(fields, Pg="PT", Ra=4, a_bsel=f"B{byte}"), dict(regs, **bits_of(0x2a), R4=base),
                  {**bits_of(r2p_ref(0x2a, base, loaded, byte)), "__native": True}, memory=memory)

    for k in c.of("P2R", cx=None) + c.of("R2P", cx=None):
        for mask, byte in product((0, 1, 0x25, 0x7f, 0x80, 0xff), range(4)):
            for rd, base in ((2, 0x12345678), (2, 0xffffffff), (4, 0xa5a5a5a5)) if k.mnemonic == "P2R" else ((None, 0xa1b2c354),):
                add(k, mask, byte, rd, base)
        if is_cx(k): add(k, 0xff, 0, 2, 0xffffffff, units=0)   # past the descriptor's size the mask reads 0

def gpu_fixtures():
    """Patched carry-kernel slots: P2R/R2P and PLOP3 after ISETPs establish P0..P6 = 0b1010101; UP2UR
    after UISETPs set selected uniform predicates."""
    from tests.harness.gpu import sm89
    from tests.harness.templates import cx_fixture, slot_fixture
    data, nop = [(i, 0, 0) for i in range(128)], sm89("nop_")
    init = [sm89("isetp__RRR_RRR_noEX", Pu=lo, Pv=hi, Pp="PT", Ra="RZ", Rb="RZ", icmp="EQ", fmt="U32", bop="AND") for lo, hi in ((0, 1), (2, 3), (4, 5), (6, "PT"))]
    p2r = lambda ra, mask, insert, rd=11: sm89("p2r__RsIR_RIR", Rd=rd, Pr="PR", Ra=ra, Sb=mask, insert=insert)
    r2p = lambda ra, mask, bsel: sm89("r2p__RIR", Ra=ra, Sb=mask, a_bsel=bsel)
    mov = lambda value, rd=4: sm89("mov__RI", Rd=rd, Sb=value, PixMaskU04=15)
    out = [slot_fixture(f"p2r_mask_{m:08x}", init + [p2r("RZ", m, "B0"), nop, nop, nop], data, [p2r_ref(0x55, 0, m, 0)] * 128, 0x140)
           for m in (0, 1, 0x15, 0x2a, 0x55, 0x7f, 0xff, 0x80000055, 0xffffffff)]
    # The selector chooses the destination byte that receives the packed bits.
    out += [slot_fixture(f"p2r_b{b}", [mov(0x11223344)] + init + [p2r(4, 0x7f, f"B{b}"), nop, nop], data,
                         [p2r_ref(0x55, 0x11223344, 0x7f, b)] * 128, 0x150) for b in range(4)]
    # A nonzero base keeps every bit outside the mask's low byte; mask bit 7 clears bit 7.
    grid = [(0xffffffff, m) for m in (0, 1, 0x2a, 0x55, 0x80, 0xff, 0xffffffff)] + [(0xa5a5a5a5, m) for m in (0x2a, 0x55, 0xff)]
    out += [slot_fixture(f"p2r_base_{base:08x}_b{b}_mask_{m:08x}", [mov(base)] + init + [p2r(4, m, f"B{b}"), nop, nop], data,
                         [p2r_ref(0x55, base, m, b)] * 128, 0x150) for base, m in grid for b in range(4)]
    # Rd == Ra, the in-place accumulation form.
    out += [slot_fixture(f"p2r_inplace_b{b}_mask_{m:08x}", [mov(0xa5a5a5a5)] + init + [p2r(4, m, f"B{b}", rd=4), nop, nop], data,
                         [p2r_ref(0x55, 0xa5a5a5a5, m, b)] * 128, 0x150,
                         final=sm89("mov__RR", Rd=11, Rb=4, PixMaskU04=15)) for b in (0, 2) for m in (0x2a, 0x55)]
    # Clear selected bits from the initialized pattern, then pack all seven back.
    out.append(slot_fixture("r2p_mask_15", init + [r2p("RZ", 0x15, "B0"), nop, p2r("RZ", 0x7f, "B0"), nop], data, [0x40] * 128, 0x140))
    # The selector chooses which source byte supplies the selected predicate bits.
    out += [slot_fixture(f"r2p_b{b}", [mov(0xa1b2c354)] + init + [r2p(4, 0x7f, f"B{b}"), nop, p2r("RZ", 0x7f, "B0")], data,
                         [(0xa1b2c354 >> 8 * b) & 0x7f] * 128, 0x150) for b in range(4)]
    # Register and uniform-register masks (R6, UR6); R2P is read back by P2R at 0x180.
    readback = p2r("RZ", 0x7f, "B0")
    masks = {"RRR": lambda m: mov(m, 6), "RUR": lambda m: sm89("umov__UI", URd=6, Sb=m)}
    for form, load in masks.items():
        operand = dict(Rb=6) if form == "RRR" else dict(URb=6)
        out += [slot_fixture(f"p2r_{form}_b{b}_mask_{m:08x}", [mov(0xffffffff), load(m)] + init + [sm89(f"p2r__{form}_{form}", Rd=11, Pr="PR", Ra=4, insert=f"B{b}", **operand), nop],
                             data, [p2r_ref(0x55, 0xffffffff, m, b)] * 128, 0x160) for m in (0x2a, 0x80, 0xffffff55) for b in (1, 3)]
        out += [slot_fixture(f"r2p_{form}_b{b}_mask_{m:08x}", [mov(0xa1b2c354), load(m)] + init + [sm89(f"r2p__{form}", Ra=4, a_bsel=f"B{b}", **operand), nop],
                             data, [r2p_ref(0x55, 0xa1b2c354, m, b)] * 128, 0x160, final=readback) for m in (0x15, 0xffffff2a) for b in (0, 2)]
    # Constant-bank masks: c[0x0][0x0] is blockDim.x, so the block size picks the mask.
    for block in (32, 64, 128):
        out += [slot_fixture(f"p2r_RCR_b{b}_base_{base:08x}_mask_{block:02x}", [mov(base)] + init + [sm89("p2r__RCR_RCR", Rd=11, Pr="PR", Ra=4, Sb_bank=0, Sb_addr=0, insert=f"B{b}"), nop, nop],
                             data, [p2r_ref(0x55, base, block, b)] * 128, 0x150, block=block) for base in (0, 0xffffffff) for b in (0, 3)]
        out += [slot_fixture(f"r2p_RCR_b{b}_mask_{block:02x}", [mov(0xffffffff)] + init + [sm89("r2p__RCR", Ra=4, Sb_bank=0, Sb_addr=0, a_bsel=f"B{b}"), nop, nop],
                             data, [r2p_ref(0x55, 0xffffffff, block, b)] * 128, 0x150, final=readback, block=block) for b in (0, 2)]
    # CX masks, read from the descriptor (the input array) at byte 4: the second word of every row.
    # The wrapper sets no predicates, so P2R's masks select only bit 7 (no predicate behind it), and
    # R2P selects all seven and is read back by a P2R at 0x130 into the stored R7.
    rng = random.Random(89)
    values = [0xffffffff, 0, 0xa5a5a5a5, 0x5a5a5a5a] + [rng.getrandbits(32) for _ in range(124)]
    for m, units, bytes_ in ((0xffffff80, 128, range(4)), (0x80, 0, (0,)), (0x100, 128, (0,)), (0, 128, (1,))):
        loaded = m if units else 0
        for b in bytes_:
            want = [p2r_ref(0, a, loaded, b) for a in values]
            assert want == [p2r_ref(0x7f, a, loaded, b) for a in values], "the result must not depend on the predicates"
            out.append(cx_fixture(f"p2r_cx_b{b}_units_{units}_mask_{m:08x}", "p2r__RCxR_RCxR", dict(Rd=7, Pr="PR", Ra=2, insert=f"B{b}"),
                                  [(a, m, 0) for a in values], want, units=units))
    out += [cx_fixture(f"r2p_cx_b{b}_mask_{m:08x}", "r2p__RCxR", dict(Ra=2, a_bsel=f"B{b}"), [(a, m, 0) for a in values],
                       [r2p_ref(0, a, m, b) for a in values], after=("p2r__RsIR_RIR", dict(Rd=7, Pr="PR", Ra="RZ", Sb=0x7f, insert="B0")))
            for m in (0x7f, 0xffffffff) for b in range(4)]
    # PLOP3 after the same initialization; P2R reads all seven predicates back.
    initial = [(0x55 >> i) & 1 for i in range(7)]
    lut = lambda table, xs: table >> (4 * xs[0] + 2 * xs[1] + xs[2]) & 1
    for n, (u, v, pu, pv, srcs, nots) in enumerate([(0xa8, 0x00, 5, 6, (0, 1, 2), 0), (0x2a, 0x80, 1, 3, (0, 2, 4), 0), (0xe0, 0x96, 0, 6, (1, 3, 6), 5),
                                                  (0x6a, 0xfe, 2, 4, (6, 5, 4), 2), (0x80, 0x7f, 5, 1, (0, 7, 2), 7), (0x17, 0xe8, 3, 0, (2, 4, 6), 0)]):
        xs = [(1 if p == 7 else initial[p]) ^ (nots >> i & 1) for i, p in enumerate(srcs)]
        want = list(initial)
        want[pu], want[pv] = lut(u, xs), lut(v, xs)
        plop3 = sm89("plop3_lut_2out_", Pu=pu, Pv=pv, Pp=srcs[0], Pq=srcs[1], Pr=srcs[2], uimm8=u, vimm8=v,
                     **{f"{x}@not": nots >> i & 1 for i, x in enumerate(("Pp", "Pq", "Pr"))})
        out.append(slot_fixture(f"plop3_{n}", init + [plop3, nop, p2r("RZ", 0x7f, "B0"), nop], data,
                                [sum(b << i for i, b in enumerate(want))] * 128, 0x140))
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
                                [p2r_ref(bits, 0x100, mask, 0)] * 128, 0x160, final=sm89("mov__RU", Rd=11, URb=5, PixMaskU04=15)))
    # A nonzero URa merges as for P2R: UP0..UP6 = 0b1010101. UR4/UR5 hold the
    # kernel's store descriptor (a bit-59 descriptor traps), so these use UR8/UR9.
    uinit = [sm89("uisetp__URURUR_URURUR", UPu=lo, UPv=hi, URa="URZ", URb="URZ", UPp="UPT", icmp="EQ", fmt="U32", bop="AND")
             for lo, hi in ((0, 1), (2, 3), (4, 5), (6, "UPT"))]
    up2ur = lambda urd, mask: sm89("up2ur__Imm", URd=urd, UPR="UPR", URa=8, Sb=mask, insert="B0")
    for base, urd, masks in ((0xffffffff, 9, (0, 1, 0x2a, 0x55, 0x80, 0xff, 0xffffffff)), (0xa5a5a5a5, 9, (0x2a, 0x55, 0xff)), (0xa5a5a5a5, 8, (0x2a, 0x55))):
        out += [slot_fixture(f"up2ur_base_{base:08x}_ur{urd}_mask_{m:08x}", [sm89("umov__UI", URd=8, Sb=base)] + uinit + [up2ur(urd, m), nop, nop], data,
                             [p2r_ref(0x55, base, m, 0)] * 128, 0x150, final=sm89("mov__RU", Rd=11, URb=urd, PixMaskU04=15)) for m in masks]
    return out
