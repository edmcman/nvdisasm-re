"""Packed binary32 pairs: FADD2/FMUL2/FFMA2 (SM100+) and SM107's mixed FHADD2/FHFMA2/FHMUL2, against
softfloat per lane. Source lanes follow ptxas's folding of f32x2 operands (sm_107a, CUDA 13.4): a
register pair is (lane 0 = Rn, lane 1 = Rn+1), .LO_HI swaps it, .F32 and immediates broadcast one
value; |x| applies to both lanes, -x to lane 0 and, XOR NEGA .NP, to lane 1, after the swizzle."""
from tests.harness.manual import retained
import random
from sass.semantics.fp import softfloat
from tests.harness.case import pair, source
from tests.refs.float import readable
from tests.refs import mixed

ROUNDINGS = ("RN", "RZ", "RM", "RP")
OPS = {"FADD2": "add", "FHADD2": "add", "FMUL2": "mul", "FHMUL2": "mul", "FFMA2": "fma", "FHFMA2": "fma"}
HALVES = {"F16x2": "F16", "BF16x2": "BF16"}


def specs(k):
    """(operand, swizzle, NEGA) of A, B and C present in class k."""
    out = []
    for group, swz, neg in (("Ra", "iswzA", "negA"), ("Rb/URb/Sb", "iswzB", None), ("Rc/URc/Sc", "iswzC", "negC")):
        name = source(k, *group.split("/"))
        if name: out.append((name, swz if swz in k.operand_types else None, neg if neg in k.operand_types else None))
    return out


def values(rng):
    edges = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f7fffff, 0xff7fffff,
             0x7f800000, 0xff800000, 0x3effffff, 0x4b800001, 0x38800000, 0x387fe000, 0x47800000, 0x477fe000]
    return edges + [rng.getrandbits(32) & 0xbfffffff for _ in range(10)] + [rng.getrandbits(32) for _ in range(6)]


def half_values(rng):
    return [0, 0x8000, 1, 0x3c00, 0xbc00, 0x7bff, 0x7c00, 0x03ff, 0x3f80, 0xbf80, 0x7f7f, 0x0080] + [rng.getrandbits(16) for _ in range(8)]


@retained('Seeded wide/mixed lane, source-sign/NP and directed-rounding corpora exceed the ordinary packed binary32 form profile.')
def arithmetic(c):
    rng = random.Random("fp32x2")
    f32s, f16s = values(rng), half_values(rng)
    for mnemonic, op in OPS.items():
        for k in c.of(mnemonic):
            ops = specs(k)
            narrow = "d16b" in k.name
            dsts = [HALVES[d] for d in c.labels(k, "Dfmt", tuple(HALVES))] if narrow else [None]
            afmts = [HALVES[a] for a in c.labels(k, "Afmt", tuple(HALVES))] if "a16b" in k.name else [None]
            flush_name = source(k, "ftz", "fmz")
            for n in range(48):
                swizzles = [c.labels(k, s, ("H1_H0", "H0_H0", "H1_H1") if j == 0 and afmts[0] else ("F32x2.HI_LO", "F32x2.LO_HI", "F32"))[(n + j) % 3] if s else None
                            for j, (_, s, _) in enumerate(ops)]
                afmt, dst = afmts[n % len(afmts)], dsts[n % len(dsts)]
                rnd = "RZ" if narrow else ROUNDINGS[n % 4]
                flush = dst == "F16" if narrow else bool(flush_name and n % 5 == 4)   # the md: F16x2 Rd requires FTZ, BF16x2 forbids it
                fields, regs, srcs = {"Rd": 8, "rnd": rnd}, {}, []
                if flush: fields[flush_name] = "FTZ"
                if dst: fields["Dfmt"] = {"F16": "F16x2", "BF16": "BF16x2"}[dst]
                if afmt: fields["Afmt"] = {"F16": "F16x2", "BF16": "BF16x2"}[afmt]
                for j, ((name, swz, neg), reg) in enumerate(zip(ops, (2, 4, 6))):
                    half = afmt if j == 0 else None
                    pool = f16s if half else f32s
                    pick = lambda m: pool[(n * (3 + 2 * j) + m * 5 + j) % len(pool)]
                    typ = k.operand_types[name].type
                    wide = bool(not half and swz and swizzles[j] != "F32" and typ != "F32Imm")
                    value = (pick(0) | pick(1) << 16) if half else (pick(0) | pick(1) << 32) if wide else pick(0)
                    absolute = negate = np_ = 0
                    if typ in ("Register", "UniformRegister"):
                        absolute, negate = (n >> 1) & 1 and j != 1, (n >> 2) & 1
                        fields.update({f"{name}@absolute": int(absolute), f"{name}@negate": negate})
                        if typ == "Register": fields[name] = reg; regs.update(pair(f"R{reg}", value) if wide else {f"R{reg}": value})
                        else: fields[name] = reg + 8; regs.update(pair(f"UR{reg + 8}", value) if wide else {f"UR{reg + 8}": value})
                    else: fields[name] = value
                    if swz: fields[swz] = swizzles[j]
                    if neg:
                        np_ = int(bool((n >> 3) & 1 and (wide or half)))
                        fields[neg] = "NP" if np_ else "PP"
                    srcs.append(mixed.ref_lanes(value, swizzles[j] or "F32x2.HI_LO", absolute, negate, np_, half) if (wide or half) else mixed.ref_lanes(value, "F32", absolute, negate, np_))
                if None in srcs: continue
                lanes = [mixed.lane_result(op, [s[lane] for s in srcs], rnd, bool(flush), dst) for lane in (0, 1)]
                if None in lanes: continue
                want = lanes[0] | lanes[1] << 16 if dst else lanes[0] | lanes[1] << 32
                c.add(k.name, fields, regs, dict(R8=want, __native=True) if dst else dict(pair("R8", want), __native=True))


@retained('Seeded unfused readable pair regressions exceed the ordinary packed form profile.')
def readable_formulas(c):
    """The readable lowering: round to nearest per lane, no flushing, rounding mode ignored."""
    if c.fp != "readable": return
    rng = random.Random("fp32x2_readable")
    f32s = values(rng)
    for mnemonic in ("FADD2", "FMUL2", "FFMA2"):
        op = OPS[mnemonic]
        for k in c.of(mnemonic):
            ops = specs(k)
            if any(k.operand_types[n].type not in ("Register",) for n, _, _ in ops): continue
            for n in range(12):
                inputs = [(f32s[(n * 7 + j) % len(f32s)], f32s[(n * 11 + 3 * j + 1) % len(f32s)]) for j in range(len(ops))]
                fields, regs = {"Rd": 8, "rnd": ROUNDINGS[n % 4]}, {}
                for (name, swz, _), reg, (lo, hi) in zip(ops, (2, 4, 6), inputs):
                    fields[name] = reg; regs.update(pair(f"R{reg}", lo | hi << 32))
                    if swz: fields[swz] = "F32x2.HI_LO"
                lanes = [readable(op, [x[lane] for x in inputs], 32) for lane in (0, 1)]
                if None in lanes: continue
                c.add(k.name, fields, regs, dict(pair("R8", lanes[0] | lanes[1] << 32), __native=True), portable=True)


def emulator_cases(c):
    arithmetic(c); readable_formulas(c)
