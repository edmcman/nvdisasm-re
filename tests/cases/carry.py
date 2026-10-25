"""Carry chains: IADD3(.X), LEA(.X/.HI/.SX32), IMAD(.HI/.WIDE)(.X) and uniform aliases.
References use full unsigned sums, keeping negate's +1 and the high product/addend;
overlapping predicate destinations capture their inputs first."""
import random
from itertools import product
from tests.refs.bits import MASK32
from tests.harness.manual import retained
from tests.refs.integer import carry as reference


@retained("Constant UPT carry inputs and aliased predicate-output precedence require explicit register choices.")
def emulator_cases(c):
    c.add("uiadd3_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, URc=6, UPp="UPT", UPq="UPT"), {}, dict(UR8=2, __native=True))
    c.add("uimad_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, URc=6, UPp="UPT"), {}, dict(UR8=1, __native=True))
    c.add("ulea_lo_noimm_x__URURUR_URURUR", dict(URd=8, URa=2, URb=4, scaleU5=3, UPp="UPT"), {}, dict(UR8=1, __native=True))
    for a,b,d in ((0,0,0),(MASK32,MASK32,MASK32),(0x80000000,0x80000000,0),(MASK32,1,0),(0,MASK32,1)):
        for ext in (False, True):
            cls = "iadd3_x_noimm__RRR_RRR" if ext else "iadd3_noimm__RRR_RRR"
            total = a+b+d+(1 if ext else 0)
            with_uniform_twin(c, cls, dict(Rd=2, Pu=0, Pv=0, Ra=2, Rb=4, Rc=6, **(dict(Pp=0, Pq=1) if ext else {})),
                dict(R2=a,R4=b,R6=d,P0=1,P1=0), dict(R2=total & MASK32,P0=int(total>MASK32),__native=True))


# Per-thread classes and their uniform-datapath twins, where the architecture has them (UIMAD.HI: SM120).
UNIFORM = {"iadd3_noimm__RRR_RRR": "uiadd3__URURUR_URURUR", "iadd3_x_noimm__RRR_RRR": "uiadd3_x__URURUR_URURUR",
           **{f"lea_{k}__RRR_RRR": f"ulea_{k}__URURUR_URURUR" for k in ("lo_noimm", "lo_noimm_x", "hi_noimm", "hi_noimm_x", "hi_noimm_sx32", "hi_noimm_sx32_x")},
           **{f"imad{k}__RRR_RRR": f"uimad{k}__URURUR_URURUR" for k in ("_x", "_hi", "_hi_x", "_wide", "_wide_x")}}


def with_uniform_twin(c, cls, fields, regs, expected):
    """The per-thread case and the same case on its uniform twin, with Rn/Pn as URn/UPn."""
    c.add(cls, fields, regs, expected)
    if (twin := UNIFORM.get(cls)) not in c.classes: return
    operands = c.classes[twin].operand_types   # URd, UPu, ...: rename the fields (and modifiers) the twin has
    fields = {"U" + k if "U" + k.partition("@")[0] in operands else k: v for k, v in fields.items()}
    state = lambda d: {k if k.startswith("__") else "U" + k: v for k, v in d.items()}   # R2 -> UR2, P0 -> UP0
    c.add(twin, fields, state(regs), state(expected))



def gpu_fixtures(arch="SM89"):
    """Patched carry kernel: four slots set P2/P3 from input C, then the target instruction, then
    readback of its result, carry predicates, or high/low words into R11."""
    import itertools
    from tests.harness.gpu import encode
    from tests.harness.templates import slot_fixture
    rng = random.Random(44); edges = [0, 1, 2, 3, MASK32, 0xfffffffe, 0x7fffffff, 0x80000000, 0x80000001, 0xffff0000]
    vectors = [(a, b, edges[(i * 7 + 4) % len(edges)]) for i, (a, b) in enumerate(product(edges, repeat=2))]
    vectors += [(MASK32, MASK32, MASK32), (MASK32, MASK32, 3), (0x80000000, 0x80000000, 0)] + [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(25)]
    prep = [encode("lop3_lut__RuIR_RIR", Rd=0, Ra=6, Sb=1, Rc=255, imm8=0xc0, Pp=7, arch=arch),
            encode("isetp__RsIR_RIR_noEX", Pu=2, Pv=7, Ra=0, Sb=0, icmp="NE", bop="AND", Pp=7, fmt="U32", arch=arch),
            encode("lop3_lut__RuIR_RIR", Rd=0, Ra=6, Sb=2, Rc=255, imm8=0xc0, Pp=7, arch=arch),
            encode("isetp__RsIR_RIR_noEX", Pu=3, Pv=7, Ra=0, Sb=0, icmp="NE", bop="AND", Pp=7, fmt="U32", arch=arch)]
    nop = encode("nop_", arch=arch)
    sel = lambda rd, sb, pp: encode("sel__RuIR_RIR", Rd=rd, Ra=255, Sb=sb, Pp=pp, arch=arch, **{"Pp@not": 1})

    def fixture(cls, fields, observe):
        suffix = {"carry": [sel(0, 1, 0), sel(11, 2, 1), encode("iadd3_noimm__RRR_RRR", Rd=11, Ra=0, Rb=11, Rc=255, arch=arch)],
                  "low": [nop, nop, encode("mov__RR", Rd=11, Rb=10, PixMaskU04=15, arch=arch)], "high": [nop, nop, encode("mov__RR", Rd=11, Rb=11, PixMaskU04=15, arch=arch)],
                  "pu": [nop, nop, sel(11, 1, 0)]}.get(observe, [nop] * 3)
        seq = prep + [encode(cls, arch=arch, **fields)] + suffix
        if cls.startswith("imad_") and ("_hi_" in cls or "_wide_" in cls):
            seq[2:4] = [encode("mov__RR", Rd=7, Rb=5, PixMaskU04=15, arch=arch), nop]  # pair addend C is R6/R7; the high word is B
        return slot_fixture(f"carry_{cls}_{observe}_{sorted(fields.items())}", seq, vectors, [reference(cls, fields, observe, v) for v in vectors], 4, arch=arch)
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
