"""Scalar integer and bit operations: IMAD/IADD3/LOP3/LEA/SHF/ISETP/PRMT, uniform aliases,
BREV/POPC/FLO/SGXT and MOV."""
import json, random
from tests.harness.case import cbank, put, source
from tests.paths import ROOT
from tests.refs.bits import MASK32, MASK64, sext
from tests.refs.integer import COMPARISONS, brev, combine, compare, flo, prmt, sgxt_byte


def bit_ops(c):
    # CuLifter comparison: scalar bit operations against per-bit (BREV) and bit_count (POPC).
    rng = random.Random("brev_popc")
    vectors = [0, 1, 0x80000000, 0xffffffff, 0x01234567, 0xaaaaaaaa, 0x55555555] + [rng.getrandbits(32) for _ in range(20)]
    for op, ref in (("BREV", brev), ("POPC", int.bit_count)):
        for k in c.of(op):
            src = source(k, "Rb", "URb", "Sb")
            if src is None: continue
            if k.operand_types[src].type == "C":
                for bank, offset, value in ((2, 16, 0x01234567), (3, 24, 0x80000001)):
                    fields = {"Rd": 2, "Sb_bank": bank, "Sb_addr": offset}
                    if "Sb@invert" in k.operand_types: fields["Sb@invert"] = 1; value_in = ~value & MASK32
                    else: value_in = value
                    c.add(k.name, fields, {}, {"R2": ref(value_in), "__native": True}, memory=[cbank(bank, offset, value)])
                continue
            for i, value in enumerate(vectors):
                fields, regs = {"Rd": 2}, {}
                put(k, src, value, fields, regs, [], 4, imm=(k.operand_types[src].type,))
                if i == 1 and src + "@invert" in k.operand_types: fields[src + "@invert"] = 1; value = ~value & MASK32
                c.add(k.name, fields, regs, {"R2": ref(value), "__native": True})
    # FLO returns the highest set-bit index, .SH the normalization shift; zero gives -1; .S32
    # searches the complement of negative values.
    for k in c.of("FLO"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None: continue
        for fmt in ("S32", "U32"):
            for mode in ("nosh", "SH"):
                for value in (0, 1, 2, 0x80000000, 0xffffffff, 0xfffffffe, 0x01000000, 0x00ffffff):
                    fields, regs, memory = {"Rd": 2, "Pu": "PT", "fmt": fmt, "sh": mode}, {}, []
                    put(k, src, value, fields, regs, memory, 4, bank=(2, 16), imm=(k.operand_types[src].type,))
                    c.add(k.name, fields, regs, {"R2": flo(value, mode == "SH", fmt == "S32"), "__native": True}, memory=memory)
    # SGXT selects one signed byte using the low two index bits.
    for k in c.of("SGXT"):
        src = source(k, "Rb", "URb", "Sb")
        if src is None: continue
        for index in range(4):
            for value in (0, 0x7f80ff01, 0x80ff007f, 0xdeadbeef):
                fields, regs, memory = {"Rd": 2, "Ra": 4, "cw": "C", "fmt": "S32"}, {"R4": index}, []
                put(k, src, value, fields, regs, memory, 6, bank=(2, 16), imm=(k.operand_types[src].type,))
                c.add(k.name, fields, regs, {"R2": sgxt_byte(value, index), "__native": True}, memory=memory)


def arithmetic(c):
    rng = random.Random("int_alu")
    for i in range(20):
        a, b, d = [rng.getrandbits(32) for _ in range(3)]; regs = {"R2": a, "R4": b, "R6": d}
        rd = 2 if i % 2 else 8  # overlap source/destination
        common = dict(Rd=rd, Ra=2, Rb=4, Rc=6)
        c.add("imad__RRR_RRR", dict(common, fmt="U32"), regs, {f"R{rd}": (a * b + d) & MASK32})
        c.add("imad_hi__RRR_RRR", dict(common, fmt="U32"), regs, {f"R{rd}": ((a * b + d) & MASK64) >> 32})
        c.add("iadd3_noimm__RRR_RRR", common, regs, {f"R{rd}": (a + b + d) & MASK32})
        c.add("iabs__RRR_R", dict(Rd=rd, Rb=4), regs, {f"R{rd}": abs(sext(b, 32)) & MASK32})
        c.add("sel__RRR_RRR", dict(Rd=rd, Ra=2, Rb=4, Pp=0), dict(regs, P0=i % 2), {f"R{rd}": a if i % 2 else b})
        # SM120 adds a second predicate input and two predicate results: an explicit fallback.
        c.add("imnmx__RRR_RRR", dict(Rd=rd, Ra=2, Rb=4, Pp=0, fmt="U32"), dict(regs, P0=1),
              dict(error="sass_opaque_IMNMX") if c.arch == "SM120" else {f"R{rd}": min(a, b)})
        c.add("lop3_lut__RRR_RRR", dict(common, imm8=0x96, Pp="PT"), regs, {f"R{rd}": a ^ b ^ d})
        c.add("lea_lo_noimm__RRR_RRR", dict(Rd=rd, Ra=2, Rb=4, scaleU5=i % 8), regs, {f"R{rd}": ((a << (i % 8)) + b) & MASK32})
    # Mixed register banks and uniform aliases retain original md operand names.
    rng = random.Random("uniform")
    for i in range(12):
        a, b, d = [rng.getrandbits(32) for _ in range(3)]
        regs = dict(R2=a, R4=b, R6=d, UR2=a, UR4=b, UR6=d)
        c.add("imad__RUR_RUR", dict(Rd=2, Ra=2, URb=4, Rc=6, fmt="U32"), regs, dict(R2=(a * b + d) & MASK32))
        c.add("imad__RRU_RRU", dict(Rd=2, Ra=2, Rb=4, URc=6, fmt="U32"), regs, dict(R2=(a * b + d) & MASK32))
        c.add("iadd3_noimm__RUR_RUR", dict(Rd=2, Ra=2, URb=4, Rc=6), regs, dict(R2=(a + b + d) & MASK32))
        c.add("lea_lo_noimm__RUR_RUR", dict(Rd=2, Ra=2, URb=4, scaleU5=3), regs, dict(R2=((a << 3) + b) & MASK32))
        c.add("isetp__RUR_RUR_noEX", dict(Pu=0, Pv=1, Ra=2, URb=4, Pp="PT", icmp="LT", bop="AND", fmt="U32"), regs, dict(P0=int(a < b), P1=int(a >= b)))
        common = dict(URd=2, URa=2, URb=4, URc=6)
        c.add("uiadd3__URURUR_URURUR", common, regs, dict(UR2=(a + b + d) & MASK32, __max_ops=2))
        c.add("uimad__URURUR_URURUR", dict(common, fmt="U32"), regs, dict(UR2=(a * b + d) & MASK32, __max_ops=2))
        c.add("ulop3_lut__URURUR_URURUR", dict(common, imm8=0x96, UPp="UPT"), regs, dict(UR2=a ^ b ^ d))
        c.add("ulea_lo_noimm__URURUR_URURUR", dict(URd=2, URa=2, URb=4, scaleU5=3), regs, dict(UR2=((a << 3) + b) & MASK32))
        c.add("ushf__URURUR_URURUR", dict(common, fmt="U32", dir="R", cw="W", hilo="LO"), regs, dict(UR2=(((d << 32) | a) >> (b & 31)) & MASK32))
        c.add("uisetp__URURUR_URURUR", dict(UPu=0, UPv=1, URa=2, URb=4, UPp=0, icmp="LT", bop="AND", fmt="U32"), dict(regs, UP0=1), dict(UP0=int(a < b), UP1=int(a >= b)))
        c.add("usel__URURUR_UUU", dict(URd=2, URa=2, URb=4, UPp=0), dict(regs, UP0=i & 1), dict(UR2=a if i & 1 else b))
        c.add("umov__UR", dict(URd=2, URb=4), regs, dict(UR2=b, __max_ops=1))
        c.add("umov__UI", dict(URd=2, Sb=b), {}, dict(UR2=b, __max_ops=1))
        if "viadd__RRR_RRR" in c.classes:
            fmt = c.enum(c.classes["viadd__RRR_RRR"], "fmt")
            c.add("viadd__RRR_RRR", dict(Rd=2, Ra=2, Rb=4, fmt="U32" if "U32" in fmt else "32"), regs, dict(R2=(a + b) & MASK32))
        c.add("iadd_noimm__RRR_RRR", dict(Rd=2, Ra=2, Rb=4), regs, dict(R2=(a + b) & MASK32))
    # URZ is 63 in SM75-SM90 but 255 in SM100+, where 63 is the ordinary UR63
    # (ptxas emits UIMAD.WIDE.U32 ..., URZ with the eight-bit encoding).
    a, b, d = 0x9abcdef1, 0x87654321, 0x13579bdf
    urz = dict(UR4=a, UR5=b)
    c.add("uimad_wide__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc="URZ", fmt="U32"), urz, dict(UR2=a * b & MASK32, UR3=a * b >> 32, __native=True))
    c.add("uiadd3__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc="URZ"), urz, dict(UR2=(a + b) & MASK32, __native=True))
    if c.md.enums["UniformRegister"]["URZ"] != 63:
        c.add("uiadd3__URURUR_URURUR", dict(URd=2, URa=4, URb=5, URc=63), dict(urz, UR63=d), dict(UR2=(a + b + d) & MASK32, __native=True))


def permute(c):
    # Every byte selector, including sign replication and overlapping Rd.
    for mode in ("IDX", "F4E", "B4E", "RC8", "RC16", "ECL", "ECR"):
        for selector in range(16):
            a, d = 0x80ff017f, 0x55aa0081
            sel = selector * 0x1111 if mode == "IDX" else selector
            c.add("prmt__RRR_RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6, pmode=mode), dict(R2=a, R4=sel, R6=d), dict(R2=prmt(a, sel, d, mode), __native=True))
    c.add("prmt__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, pmode="IDX"), dict(R2=0x03020100, R4=0x6420, R6=0x07060504), dict(R8=0x06040200))
    # UPRMT is IDX only (/IDXOnly:idx), on uniform registers.
    for k in c.of("UPRMT", "uprmt__URURUR"):
        for selector in (0x3210, 0x7654, 0x89ab, 0xcdef, 0x0f1e, 0x6420):
            a, d = 0x80ff017f, 0x55aa0081
            c.add(k.name, dict(URd=2, URa=4, URb=5, URc=6), dict(UR4=a, UR5=selector, UR6=d), dict(UR2=prmt(a, selector, d, "IDX"), __native=True))
    for mode, selector, want in [("F4E", 1, 0x04030201), ("B4E", 1, 0x06070001), ("RC8", 2, 0x02020202),
                                 ("RC16", 1, 0x03020302), ("ECL", 2, 0x03020202), ("ECR", 2, 0x02020100)]:
        c.add("prmt__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, pmode=mode), dict(R2=0x03020100, R4=selector, R6=0x07060504), dict(R8=want))


def fixed(c):
    c.add("mov__RI", dict(Rd=3, Sb=0xdeadbeef, PixMaskU04=15), {}, dict(R3=0xdeadbeef, __max_ops=1))
    c.add("mov__RR", dict(Rd=3, Rb=2, PixMaskU04=15), dict(R2=0xdeadbeef), dict(R3=0xdeadbeef, __max_ops=1))
    c.add("mov__RR", dict(Rd=2, Rb=2, PixMaskU04=15), dict(R2=0xdeadbeef), dict(R2=0xdeadbeef))
    # Compact p-code: ordinary forms lower to a fixed number of operations.
    for cls, fields, registers, result, limit in [
            ("iadd3_noimm__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6), dict(R2=1, R4=2, R6=3), 6, 2),
            ("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=2, R4=3, R6=4), 10, 2),
            ("lop3_lut__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, imm8=0x96, Pp="PT"), dict(R2=1, R4=2, R6=4), 7, 3),
            ("shf__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32", dir="R", cw="W", hilo="LO"), dict(R2=3, R4=1, R6=2), 1, 8)]:
        c.add(cls, fields, registers, dict(R8=result, __max_ops=limit))
    c.add("mov__RI", dict(Rd=3, Sb=5, PixMaskU04=15, Pg=0), dict(P0=0, R3=9), dict(R3=9))
    c.add("mov__RI", dict(Rd="RZ", Sb=5, PixMaskU04=15), {}, dict(RZ=0, PT=1))
    c.add("mov__RI", dict(Rd=3, Sb=5, PixMaskU04=1), {}, dict(error="sass_opaque_MOV"))
    c.add("imad_wide__RRR_RRR", dict(Rd="RZ", Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=3, R4=4, R6=5, R7=1), dict(RZ=0, PT=1))
    c.add("imad_wide__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc="RZ", fmt="U32"), dict(R2=3, R4=4), dict(R8=12, R9=0))
    c.add("imad_wide__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=0xffffffff, R4=2, R6=3, R7=1), dict(R8=1, R9=3))
    c.add("isetp__RRR_RRR_noEX", dict(Pu=0, Pv=1, Ra=2, Rb=4, Pp=2, icmp="LT", bop="AND", fmt="U32"), dict(R2=2, R4=3, P2=1), dict(P0=1, P1=0))
    c.add("shf__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32", dir="R", cw="W", hilo="LO"), dict(R2=0xabcdef12, R4=36, R6=0x12345678), dict(R8=0x8abcdef1))
    c.add("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="S32"), dict(R2=0xfffffffe, R4=3, R6=1), dict(R8=0xfffffffb))
    c.add("imad_hi__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="S32"), dict(R2=0xfffffffe, R4=3, R6=1, R7=0), dict(R8=0xffffffff))
    c.add("iadd3_noimm__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, **{"Ra@negate": 1}), dict(R2=2, R4=3, R6=4), dict(R8=5))
    c.add("imad__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32", **{"Rc@negate": 1}), dict(R2=2, R4=3, R6=4), dict(R8=2))
    c.add("imad_hi__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt="U32"), dict(R2=0xffffffff, R4=2, R6=3, R7=1), dict(R8=3))


def imad_constant(c):
    """IMAD HI/WIDE with a constant-bank addend, whose width the md declares per architecture."""
    records = json.loads((ROOT / "out" / c.arch / "semantics/IMAD.json").read_text())
    hi = next((x for x in records if x["cls"] == "imad_hi__RRC_RRC"), next(x for x in records if x["cls"].startswith("imad_hi__")))
    width = hi["sizes"]["ISRC_C_SIZE"]
    memory = [dict(space="cbank", address=(2 << 32) | 16, hex="ffffffff02000000")]
    c.add("imad_wide__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32"),
          dict(R2=0xffffffff, R4=2), dict(R8=0xfffffffd, R9=2 if c.arch in ("SM75", "SM80") else 4, P0=0), memory=memory)
    c.add("imad_hi__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32"),
          dict(R2=0xffffffff, R4=2), dict(R8=4, P0=0) if width > 32 else dict(R8=0, P0=1), memory=memory)
    if width == 32:
        # Narrow HI addends include the encoded predicate carry input. With a zero product,
        # C=UINT32_MAX and Pp=0/1 distinguish both the result bit and the carry-out predicate.
        for carry, result, predicate in ((0, 0xffffffff, 0), (1, 0, 1)):
            c.add("imad_hi_x__RRC_RRC", dict(Rd=8, Pu=0, Ra=2, Rb=4, Sc_bank=2, Sc_addr=16, fmt="U32", Pp=1),
                  dict(R2=0, R4=0, P1=carry), dict(R8=result, P0=predicate, __native=True),
                  memory=[dict(space="cbank", address=(2 << 32) | 16, hex="ffffffff")])


def selector_domains(c):
    # Exhaust the selector domains changed by decode-time specialization.
    x, y, z = 0x81234567, 0x76543210, 0xa5a5f0f0
    for lut in range(256):
        result = sum(((lut >> (((x >> i & 1) << 2) | ((y >> i & 1) << 1) | (z >> i & 1))) & 1) << i for i in range(32))
        c.add("lop3_lut__RRR_RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6, imm8=lut, Pp="PT"), dict(R2=x, R4=y, R6=z), dict(R2=result))
    for fmt in ("U32", "S32", "U64", "S64"):
        width = 32 if fmt.endswith("32") else 64
        for direction in ("L", "R"):
            for mode in ("C", "W"):
                for half in ("LO", "HI"):
                    for shift in (0, 31, 32, 63, 64, 65):
                        count = min(shift, width) if mode == "C" else shift & (width - 1)
                        value = (z << 32) | x
                        if direction == "R" and fmt.startswith("S"): value = sext(value, 64)
                        value = (value << count if direction == "L" else value >> count) & MASK64
                        c.add("shf__RRR_RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6, fmt=fmt, dir=direction, cw=mode, hilo=half),
                              dict(R2=x, R4=shift, R6=z), dict(R2=(value >> (32 if half == "HI" else 0)) & MASK32))
    for fmt in ("U32", "S32"):
        left = x if fmt == "U32" else sext(x, 32)
        for op in COMPARISONS:
            condition = compare(left, y, op)
            for bop in ("AND", "OR", "XOR"):
                for predicate in (0, 1):
                    c.add("isetp__RRR_RRR_noEX", dict(Pu=0, Pv=1, Ra=2, Rb=4, Pp=0, icmp=op, bop=bop, fmt=fmt),
                          dict(R2=x, R4=y, P0=predicate),
                          dict(P0=combine(condition, predicate, bop), P1=combine(not condition, predicate, bop)))


def isetp_ex(c):
    # CuLifter's ISETP.EX consumes Pr as the low-word comparison result and compares the
    # encoded operands as high words, across every comparison, combiner and source form.
    for k in (k for k in c.of("ISETP") if "ex" in k.operand_types):
        src = source(k, "Rb", "URb", "Sb")
        if src is None: continue
        ex_modes = ["noex", "EX"] if "noex" in c.enum(k, "ex") else ["EX"]
        for fmt in c.labels(k, "fmt", ("U32", "S32")):
            for ex_mode in ex_modes:
                for av, bv, low in [(0, 0, 0), (0x80000000, 0x7fffffff, 1), (0x7fffffff, 0x80000000, 0), (0xffffffff, 0, 1)]:
                    sa, sb = (av, bv) if fmt == "U32" else (sext(av, 32), sext(bv, 32))
                    for op in COMPARISONS:
                        for bop in ("AND", "OR", "XOR"):
                            for predicate, pr_not, p_not in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 1)):
                                cond = (low ^ pr_not) if ex_mode == "EX" and av == bv else compare(sa, sb, op)
                                fields = {"Pu": 0, "Pv": 1, "Ra": 2, "Pp": 4, "Pr": 5, "icmp": op, "fmt": fmt, "bop": bop,
                                          "ex": ex_mode, "Pp@not": p_not, "Pr@not": pr_not}
                                regs, memory = {"R2": av, "P4": predicate, "P5": low}, []
                                put(k, src, bv, fields, regs, memory, 6, bank=(2, 16), imm=(k.operand_types[src].type,))
                                p = predicate ^ p_not
                                c.add(k.name, fields, regs, {"P0": combine(cond, p, bop), "P1": combine(not cond, p, bop), "__native": True},
                                      memory=memory)


def emulator_cases(c):
    for part in (bit_ops, arithmetic, permute, fixed, imad_constant, selector_domains, isetp_ex): part(c)


def gpu_fixtures():
    from tests.harness import scalar
    return [*scalar.generic("iadd3", "imad", "imad_hi", "lop3", "shf", "iabs", "imnmx", "prmt", "isetp", "sel"),
            # The 64-bit constant source joins the full product-plus-addend sum before the high word.
            scalar.launch("imad_hi_c", lambda x, y, _: ((x * y + 0x00000002ffffffff) >> 32) & MASK32),
            scalar.launch("isetp_ex", lambda x, y, _: int(x < y)),
            scalar.launch("brev", lambda x, *_: brev(x)), scalar.launch("popc", lambda x, *_: x.bit_count()),
            scalar.launch("bfind", lambda x, *_: flo(x, False)), scalar.launch("bfind_shift", lambda x, *_: flo(x, True)),
            scalar.launch("bfind_s32", lambda x, *_: flo(x, False, True)), scalar.launch("bfind_shift_s32", lambda x, *_: flo(x, True, True))]
