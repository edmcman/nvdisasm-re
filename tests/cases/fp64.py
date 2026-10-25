"""FP64 DADD/DMUL/DFMA/DSETP: register pairs, overlap, modifiers and every rounding mode against
exact rational arithmetic; readable-lowering formulas."""
from tests.harness.manual import retained
import random
from sass.semantics.fp import softfloat
from tests.harness.case import cbank, cx_source, pair
from tests.refs.float import FCOMPARISONS, dsetp_compare, fcompare, ordered, readable, rounded, underflow_band
from tests.refs.integer import combine

ONE, TWO, THREE = 0x3ff0000000000000, 0x4000000000000000, 0x4008000000000000
ROUNDINGS = ("RN", "RZ", "RM", "RP")
OPS = [("add", "dadd__RRR_RR", dict(Rd=2, Ra=2, Rc=4)), ("mul", "dmul__RRR_RR", dict(Rd=2, Ra=2, Rb=4)), ("fma", "dfma__RRR_RRR", dict(Rd=2, Ra=2, Rb=4, Rc=6))]


def pairs(x, y, z): return {**pair("R2", x), **pair("R4", y), **pair("R6", z)}


@retained('Seeded 64-bit rounding/cancellation, source signs, guards, zero registers and explicit pair-layout regressions exceed the form profile.')
def arithmetic(c):
    rng = random.Random("fp64")
    edges = [0, 1 << 63, 1, (1 << 63) | 1, 0x000fffffffffffff, 0x0010000000000000, ONE, 0xbff0000000000000,
             0x7fefffffffffffff, 0x7ff0000000000000, 0xfff0000000000000, 0x3ff0000000000001,
             0x7ff0000000001234, 0xfff8000000005678]
    values = edges + [rng.getrandbits(64) for _ in range(30)]
    vectors = [(v, values[(i * 7 + 3) % len(values)], values[(i * 5 + 1) % len(values)]) for i, v in enumerate(values)]
    # This cancellation needs an exact wide product, rather than fp64 multiply/add.
    vectors += [(0x3ff0000000000001, 0x3feffffffffffffe, 0xbff0000000000000), (0x7fefffffffffffff, TWO, 0xffefffffffffffff)]
    for i, (x, y, z) in enumerate(vectors):
        for op, cls, fields in OPS:
            registers = [x, y, z][:3 if op == "fma" else 2]
            inputs = list(registers)
            if i % 3 == 0:
                fields = dict(fields, **{"Ra@absolute": 1, "Ra@negate": 1})
                inputs[0] |= 1 << 63
            for rnd in ROUNDINGS:
                want = rounded(op, inputs, 64, rnd, registers=registers)
                if rnd == "RN" and all(softfloat.unpack(v, 64)[0] == "finite" for v in inputs) and underflow_band(op, inputs, 64): continue
                c.add(cls, dict(fields, rnd=rnd), pairs(x, y, z), dict(pair("R2", want), __native=True))
    # Directed rounding: the residual carries the sign below the wide sum's precision, and an
    # exact cancellation is -0 under RM.
    for op, inputs in [("add", [ONE, 1]), ("add", [ONE, (1 << 63) | 1]), ("add", [ONE, 0xbff0000000000000]),
                       ("fma", [ONE, ONE, 0xbff0000000000000]), ("fma", [0x3ff0000000000001, 0x3ff0000000000001, (1 << 63) | 1])]:
        _, cls, fields = next(o for o in OPS if o[0] == op)
        for rnd in ROUNDINGS:
            c.add(cls, dict(fields, rnd=rnd), pairs(inputs[0], inputs[1], inputs[-1]), dict(pair("R2", softfloat.arithmetic(op, inputs, 64, rnd)), __native=True))
    two = [cbank(0, 0x100, TWO, 8)]
    for cls, fields, regs in [("dadd__RRU_RU", dict(Rd=8, Ra=2, URc=4), {**pair("R2", ONE), **pair("UR4", TWO)}),
                              ("dadd__RRsI_RI", dict(Rd=8, Ra=2, Sc=0x40000000), pair("R2", ONE)),
                              ("dadd__RRC_RC", dict(Rd=8, Ra=2, Sc_bank=0, Sc_addr=0x100), pair("R2", ONE))]:
        c.add(cls, fields, regs, dict(pair("R8", THREE), __native=True), memory=two)
    for cls, fields, result in [("dmul__RCR_RC", dict(Rd=8, Ra=2, Sb_bank=0, Sb_addr=0x100), TWO),
                                ("dfma__RCR_RCR", dict(Rd=8, Ra=2, Sb_bank=0, Sb_addr=0x100, Rc=4), THREE)]:
        c.add(cls, fields, {**pair("R2", ONE), **pair("R4", ONE)}, dict(pair("R8", result), __native=True), memory=two)
    # The same through a bindless cx[][] constant, whose 64-bit value runs on the GPU too.
    for cls, name, fields, regs, result in [("dadd__RRCx_RCx", "Sc", dict(Rd=8, Ra=2), pair("R2", ONE), pair("R8", THREE)),
                                            ("dmul__RCxR_RCx", "Sb", dict(Rd=8, Ra=2), pair("R2", ONE), pair("R8", TWO)),
                                            ("dfma__RCxR_RCxR", "Sb", dict(Rd=8, Ra=2, Rc=4), {**pair("R2", ONE), **pair("R4", ONE)}, pair("R8", THREE)),
                                            ("dfma__RRCx_RRCx", "Sc", dict(Rd=8, Ra=2, Rb=4), {**pair("R2", ONE), **pair("R4", ONE)}, pair("R8", THREE)),
                                            ("dsetp__RRCx_RCx", "Sc", dict(Pu=0, Pv=1, Pp="PT", Ra=2, test="LT", bop="AND"), pair("R2", ONE), dict(P0=1, P1=0)),
                                            ("f2f_f64_downconvert__RCXR_RCXR", "Sb", {"Rd": 8, "dstfmt.srcfmt": "F32.F64", "rnd": "RN"}, {}, dict(R8=0x40000000))]:
        if cls not in c.classes: continue
        cx_fields, header, memory = cx_source(c.arch, c.classes[cls], name, TWO, ur=10, size=8)
        c.add(cls, dict(fields, **cx_fields), dict(regs, **header), dict(result, __native=True), memory=memory)
    c.add("dadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, Pg=0), dict(pair("R2", ONE), P0=0, R8=7, R9=9), dict(R8=7, R9=9))
    c.add("dadd__RRR_RR", dict(Rd=8, Ra="RZ", Rc=4), pair("R4", TWO), dict(pair("R8", TWO), __native=True))


@retained('Exceptional and low-mantissa FP64 comparisons, predicate aliases, inversion and guards exceed the generated profile.')
def compare(c):
    c.add("dsetp__RRC_RC", dict(Pu=0, Pv=1, Pp="PT", Ra=2, Sc_bank=0, Sc_addr=0x100, test="LT", bop="AND"),
          pair("R2", ONE), dict(P0=1, P1=0, __native=True), memory=[cbank(0, 0x100, TWO, 8)])
    for a, b in [(0, 1 << 63), (1, 0), (0x7ff0000000000000, ONE), (0x7ff8000000000000, 0), (0, 0x7ff0000000000001), (ONE, TWO),
                 (ONE, ONE + 1), (ONE + 1, ONE), (0x000fffffffffffff, 0x0010000000000000),
                 (0x8000000000000001, 0), (0xfff0000000000000, 0x7ff0000000000000)]:
        for cmp in FCOMPARISONS + ("MIN", "MAX"):
            result = dsetp_compare(a, b, cmp)
            for bop in ("AND", "OR", "XOR"):
                for p in (0, 1):
                    pv = int(ordered(a, 64) is None and ordered(b, 64) is None) if cmp in ("MIN", "MAX") else combine(not result, p, bop)
                    c.add("dsetp__RRR_RR", dict(Pu=0, Pv=1, Pp=0, Ra=2, Rc=4, test=cmp, bop=bop), dict(pair("R2", a), **pair("R4", b), P0=p),
                          dict(P0=combine(result, p, bop), P1=pv, __native=True))
    # Read Pp before either output is written, invert it before combining, and discard
    # either output through PT. Both predicate destinations can alias the input.
    for pu, pv in [(0, 1), (1, 0), ("PT", 0), (0, "PT")]:
        for bop in ("AND", "OR", "XOR"):
            for p in (0, 1):
                for inverted in (0, 1):
                    expected = {f"P{dest}": int(combine(cond, p ^ inverted, bop))
                                for dest, cond in [(pu, True), (pv, False)] if dest != "PT"}
                    c.add("dsetp__RRR_RR", dict(Pu=pu, Pv=pv, Pp=0, Ra=2, Rc=4, test="LT", bop=bop, **{"Pp@not": inverted}),
                          dict(pair("R2", ONE), **pair("R4", TWO), P0=p, P1=1), dict(expected, __native=True))
    for guard, inverted in [(0, 0), (1, 1)]:
        c.add("dsetp__RRR_RR", dict(Pu=0, Pv=1, Pp="PT", Pg=2, Ra=2, Rc=4, test="LT", bop="AND", **{"Pg@not": inverted}),
              dict(pair("R2", ONE), **pair("R4", TWO), P0=0, P1=1, P2=guard), dict(P0=0, P1=1))


@retained('Seeded FP64 unfused/underflow inputs exceed the generated readable profile.')
def readable_formulas(c):
    if c.fp != "readable": return
    rng = random.Random("readable_fp64")
    values = [0, 1 << 63, 1, 0x0010000000000000, ONE, 0xbff0000000000000, 0x7fefffffffffffff, 0x3ff0000000000001] + [rng.getrandbits(64) for _ in range(16)]
    for i, x in enumerate(values):
        y, z = values[(i * 7 + 3) % len(values)], values[(i * 5 + 1) % len(values)]
        for op, cls, fields in OPS:
            want = readable(op, [x, y, z][:3 if op == "fma" else 2], 64)
            if want is not None:
                c.add(cls, dict(fields, rnd=ROUNDINGS[i % 4]), pairs(x, y, z), dict(pair("R2", want), __native=True), portable=True)


def emulator_cases(c):
    arithmetic(c); compare(c); readable_formulas(c)


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    return [scalar.launch(f, lambda *v, op=f[1:]: rounded(op, v[:3 if op == "fma" else 2], 64), underflow=True, arch=arch) for f in ("dadd", "dmul", "dfma")] + \
           [scalar.launch(f, lambda *v, op=f[1:4], m=f[-2:].upper(): rounded(op, v[:3 if op == "fma" else 2], 64, m), arch=arch)
            for f in sorted(f for f in scalar.ROUNDED if f[0] == "d")] + \
           [scalar.launch("dsetp", lambda a, b, _: int(fcompare(ordered(a, 64), ordered(b, 64), "LTU")), arch=arch)]
