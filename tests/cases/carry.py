"""Carry chains: IADD3(.X), LEA(.X/.HI/.SX32), IMAD(.HI/.WIDE)(.X) and uniform aliases.
References use full unsigned sums, keeping negate's +1 and the high product/addend;
overlapping predicate destinations capture their inputs first."""
import random
from itertools import product
from tests.refs.bits import MASK32, MASK64, sext
from tests.refs.integer import carry as reference


def emulator_cases(c):
    c.add("uiadd3_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, URc=6, UPp="UPT", UPq="UPT"), {}, dict(UR8=2, __native=True))
    c.add("uimad_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, URc=6, UPp="UPT"), {}, dict(UR8=1, __native=True))
    c.add("ulea_lo_noimm_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, scaleU5=3, UPp="UPT"), {}, dict(UR8=1, __native=True))
    rng = random.Random("carry")
    vectors = [(0, 0, 0), (MASK32, MASK32, MASK32), (0x80000000, 0x80000000, 0), (MASK32, 1, 0), (0, MASK32, 1)]
    vectors += [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(12)]
    for a, b, d in vectors:
        regs = dict(R2=a, R4=b, R6=d)
        for ext in (False, True):
            cls = "iadd3_x_noimm__RRR_RRR" if ext else "iadd3_noimm__RRR_RRR"
            for flags in (0, 1, 2, 4, 5, 6):
                attrs = {n + "@" + ("invert" if ext else "negate"): flags >> i & 1 for i, n in enumerate(("Ra", "Rb", "Rc"))}
                xs = [MASK32 - x + (0 if ext else 1) if flags >> i & 1 else x for i, x in enumerate((a, b, d))]
                for p, q in (product((0, 1), repeat=2) if ext else [(0, 0)]):
                    total = sum(xs) + p + q
                    fields = dict(Rd=2, Pu=0, Pv=1, Ra=2, Rb=4, Rc=6, **attrs, **(dict(Pp=0, Pq=1) if ext else {}))
                    c.add(cls, fields, dict(regs, P0=p, P1=q), dict(R2=total & MASK32, P0=int(total > MASK32), P1=int(total > 0x1ffffffff), __native=True))
        total = a + b + d + 1
        c.add("uiadd3_x__URURUR_URURUR", dict(URd=2, UPu=0, UPv=1, URa=2, URb=4, URc=6, UPp=0, UPq=1),
              dict(UR2=a, UR4=b, UR6=d, UP0=1, UP1=0), dict(UR2=total & MASK32, UP0=int(total > MASK32), UP1=int(total > 0x1ffffffff), __native=True))
        total = ((((d << 32) | a) << 5) >> 32 & MASK32) + b + 1
        c.add("ulea_hi_noimm_x__URURUR_URURUR", dict(URd=2, UPu=0, URa=2, URb=4, URc=6, scaleU5=5, UPp=0),
              dict(UR2=a, UR4=b, UR6=d, UP0=1), dict(UR2=total & MASK32, UP0=int(total > MASK32), __native=True))
        total = a * b + ((b << 32) | d) + 1
        c.add("uimad_wide_x__URURUR_URURUR", dict(URd=2, UPu=0, URa=2, URb=4, URc=6, UPp=0, fmt="U32"),
              dict(UR2=a, UR4=b, UR6=d, UR7=b, UP0=1), dict(UR2=total & MASK32, UR3=total >> 32 & MASK32, UP0=int(total > MASK64), __native=True))
        uniform_plain(c, a, b, d)
        # Aliased predicate outputs: Pu wins.
        for ext in (False, True):
            cls = "iadd3_x_noimm__RRR_RRR" if ext else "iadd3_noimm__RRR_RRR"
            total = a + b + d + (1 if ext else 0)
            c.add(cls, dict(Rd=2, Pu=0, Pv=0, Ra=2, Rb=4, Rc=6, **(dict(Pp=0, Pq=1) if ext else {})),
                  dict(regs, P0=1, P1=0), dict(R2=total & MASK32, P0=int(total > MASK32), __native=True))
        for cls in ("lea_lo_noimm__RRR_RRR", "lea_lo_noimm_x__RRR_RRR", "lea_hi_noimm__RRR_RRR",
                    "lea_hi_noimm_x__RRR_RRR", "lea_hi_noimm_sx32__RRR_RRR", "lea_hi_noimm_sx32_x__RRR_RRR"):
            ext, hi, sx = "_x__" in cls, "_hi_" in cls, "_sx32" in cls
            suffix = "@invert" if ext else "@negate"
            for shift in (0, 1, 5, 31):
                for flags in (0, 1, 2):
                    src = (sext(a, 32) if sx else (d << 32) | a) if hi else a
                    word = ((src << shift) >> (32 if hi else 0)) & MASK32
                    if flags & 1: word = MASK32 - word + (0 if ext else 1)
                    bv = MASK32 - b + (0 if ext else 1) if flags & 2 else b
                    for p in ((0, 1) if ext else (0,)):
                        total = word + bv + p
                        fields = dict(Rd=2, Pu=0, Ra=2, Rb=4, scaleU5=shift, **{"Ra" + suffix: flags & 1, "Rb" + suffix: flags >> 1})
                        if hi and not sx: fields["Rc"] = 6
                        if ext: fields["Pp"] = 0
                        c.add(cls, fields, dict(regs, P0=p), dict(R2=total & MASK32, P0=int(total > MASK32), __native=True))
        for mode in ("LO", "HI", "WIDE"):
            for ext in (False, True):
                if mode == "LO" and not ext: continue
                cls = "imad" + ("_" + mode.lower() if mode != "LO" else "") + ("_x" if ext else "") + "__RRR_RRR"
                for fmt in ("U32", "S32"):
                    av, bv = (sext(a, 32), sext(b, 32)) if fmt == "S32" else (a, b)
                    prod = (av * bv) & MASK64
                    for invert in ((0, 1) if ext else (0,)):
                        addend = (b << 32) | d if mode != "LO" else d
                        if invert: addend ^= MASK64 if mode != "LO" else MASK32
                        for p in ((0, 1) if ext else (0,)):
                            total = prod + addend + p
                            fields = dict(Rd=8, Ra=2, Rb=4, Rc=6, fmt=fmt)
                            if mode != "LO": fields["Pu"] = 0
                            if ext: fields.update(Pp=0, **{"Rc@invert": invert})
                            expected = dict(R8=(total >> (32 if mode == "HI" else 0)) & MASK32, __native=True)
                            if mode == "WIDE": expected["R9"] = total >> 32 & MASK32
                            if mode != "LO": expected["P0"] = int(total > MASK64)
                            c.add(cls, fields, dict(regs, R7=b, P0=p), expected)


def uniform_plain(c, a, b, d):
    """The plain uniform twins against the per-thread references: UIADD3 with negated sources, UIMAD, UIMAD.WIDE and USEL."""
    regs = dict(UR2=a, UR4=b, UR6=d)
    for flags in (0, 1, 2, 4):
        negations = {n + "@negate": flags >> i & 1 for i, n in enumerate(("URa", "URb", "URc"))}
        fields = dict(URd=2, UPu=0, UPv=1, URa=2, URb=4, URc=6, **negations)
        ref = lambda observe: reference("iadd3_noimm__RRR_RRR", {n.replace("UR", "R"): v for n, v in negations.items()}, observe, (a, b, d))
        c.add("uiadd3__URURUR_URURUR", fields, regs, dict(UR2=ref("result"), UP0=ref("pu"), UP1=int(ref("carry") >> 1), __native=True))
    for fmt in ("U32", "S32"):
        c.add("uimad__URURUR_URURUR", dict(URd=2, URa=2, URb=4, URc=6, fmt=fmt), regs, dict(UR2=reference("imad__RRR_RRR", dict(fmt=fmt), "result", (a, b, d)), __native=True))
        wide = lambda observe: reference("imad_wide__RRR_RRR", dict(fmt=fmt), observe, (a, b, d))
        c.add("uimad_wide__URURUR_URURUR", dict(URd=2, UPu=0, URa=2, URb=4, URc=6, fmt=fmt), dict(regs, UR7=b),
              dict(UR2=wide("low"), UR3=wide("high"), UP0=wide("pu"), __native=True))
    for p in (0, 1):
        for negate in (0, 1):
            c.add("usel__URURUR_UUU", dict(URd=2, URa=2, URb=4, UPp=0, **{"UPp@not": negate}), dict(regs, UP0=p), dict(UR2=a if p ^ negate else b, __native=True))


def gpu_fixtures():
    """Patched carry kernel: four slots set P2/P3 from input C, then the target instruction, then
    readback of its result, carry predicates, or high/low words into R11."""
    import itertools
    from tests.harness.gpu import sm89
    from tests.harness.templates import slot_fixture
    rng = random.Random(44); edges = [0, 1, 2, 3, MASK32, 0xfffffffe, 0x7fffffff, 0x80000000, 0x80000001, 0xffff0000]
    vectors = [(a, b, edges[(i * 7 + 4) % len(edges)]) for i, (a, b) in enumerate(product(edges, repeat=2))]
    vectors += [(MASK32, MASK32, MASK32), (MASK32, MASK32, 3), (0x80000000, 0x80000000, 0)] + [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(25)]
    prep = [sm89("lop3_lut__RuIR_RIR", Rd=0, Ra=6, Sb=1, Rc=255, imm8=0xc0, Pp=7),
            sm89("isetp__RsIR_RIR_noEX", Pu=2, Pv=7, Ra=0, Sb=0, icmp="NE", bop="AND", Pp=7, fmt="U32"),
            sm89("lop3_lut__RuIR_RIR", Rd=0, Ra=6, Sb=2, Rc=255, imm8=0xc0, Pp=7),
            sm89("isetp__RsIR_RIR_noEX", Pu=3, Pv=7, Ra=0, Sb=0, icmp="NE", bop="AND", Pp=7, fmt="U32")]
    nop = sm89("nop_")
    sel = lambda rd, sb, pp: sm89("sel__RuIR_RIR", Rd=rd, Ra=255, Sb=sb, Pp=pp, **{"Pp@not": 1})

    def fixture(cls, fields, observe):
        suffix = {"carry": [sel(0, 1, 0), sel(11, 2, 1), sm89("iadd3_noimm__RRR_RRR", Rd=11, Ra=0, Rb=11, Rc=255)],
                  "low": [nop, nop, sm89("mov__RR", Rd=11, Rb=10, PixMaskU04=15)], "high": [nop, nop, sm89("mov__RR", Rd=11, Rb=11, PixMaskU04=15)],
                  "pu": [nop, nop, sel(11, 1, 0)]}.get(observe, [nop] * 3)
        seq = prep + [sm89(cls, **fields)] + suffix
        if cls.startswith("imad_") and ("_hi_" in cls or "_wide_" in cls):
            seq[2:4] = [sm89("mov__RR", Rd=7, Rb=5, PixMaskU04=15), nop]  # pair addend C is R6/R7; the high word is B
        return slot_fixture(f"carry_{cls}_{observe}_{sorted(fields.items())}", seq, vectors, [reference(cls, fields, observe, v) for v in vectors], 0x140)
    out = []
    for ext in (False, True):
        cls = "iadd3_x_noimm__RRR_RRR" if ext else "iadd3_noimm__RRR_RRR"
        for flags in (0, 1, 2, 4, 5, 6):
            fields = dict(Rd=11, Pu=0, Pv=1, Ra=2, Rb=5, Rc=6, **(dict(Pp=2, Pq=3) if ext else {}),
                          **{n + "@" + ("invert" if ext else "negate"): (flags >> i) & 1 for i, n in enumerate(["Ra", "Rb", "Rc"])})
            out += [fixture(cls, fields, observe) for observe in ("result", "carry")]
        out.append(fixture(cls, dict(Rd=11, Pu=0, Pv=0, Ra=2, Rb=5, Rc=6, **(dict(Pp=2, Pq=3) if ext else {})), "pu"))
    for cls in ["lea_lo_noimm__RRR_RRR", "lea_lo_noimm_x__RRR_RRR", "lea_hi_noimm__RRR_RRR", "lea_hi_noimm_x__RRR_RRR", "lea_hi_noimm_sx32__RRR_RRR", "lea_hi_noimm_sx32_x__RRR_RRR"]:
        for shift in (0, 1, 5, 31):
            for flags in (0, 1, 2):
                fields = dict(Rd=11, Pu=0, Ra=2, Rb=5, scaleU5=shift)
                if "_hi_" in cls and "_sx32" not in cls: fields["Rc"] = 6
                if "_x__" in cls: fields["Pp"] = 2
                fields.update({n + "@" + ("invert" if "_x__" in cls else "negate"): (flags >> i) & 1 for i, n in enumerate(["Ra", "Rb"])})
                out += [fixture(cls, fields, observe) for observe in ("result", "pu")]
    for cls in ["imad_x__RRR_RRR", "imad_hi_x__RRR_RRR", "imad_wide_x__RRR_RRR", "imad_hi__RRR_RRR", "imad_wide__RRR_RRR"]:
        for fmt in ("U32", "S32"):
            for invert in (0, 1):
                if "_x__" not in cls and invert: continue
                fields = dict(Rd=10 if "_wide_" in cls else 11, Ra=2, Rb=5, Rc=6, fmt=fmt)
                if "_hi_" in cls or "_wide_" in cls: fields["Pu"] = 0
                if "_x__" in cls: fields.update(Pp=2, **{"Rc@invert": invert})
                out += [fixture(cls, fields, observe) for observe in (("low", "high", "pu") if "_wide_" in cls else ("result", "pu") if "_hi_" in cls else ("result",))]
    for invert in (1, 2, 3):
        fields = dict(Rd=11, Pu=0, Pv=1, Ra=2, Rb=5, Rc=6, Pp=2, Pq=3, **{"Pp@not": invert & 1, "Pq@not": invert >> 1})
        out += [fixture("iadd3_x_noimm__RRR_RRR", fields, observe) for observe in ("result", "carry")]
    return out
