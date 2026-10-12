"""FP32: FADD/FMUL/FFMA (every rounding mode, FTZ, SAT, FMUL scale) against the exact softfloat
model; FSETP; FSEL/FMNMX/FMNMX3 selection rules; readable-lowering formulas."""
import itertools, random
from sass import softfloat
from tests.harness.case import put, source
from tests.refs.bits import f32, ftz32, nan32
from tests.refs.float import FCOMPARISONS, fcompare, fmnmx, fmnmx3_pick, fmnmx_xorsign, readable, underflow_band

ROUNDINGS = ("RN", "RZ", "RM", "RP")


def ops(flush, mul_flush="FTZ"):
    """(op, class, fields) for FADD/FMUL/FFMA with Rd=R8 and sources R2, R4, R6."""
    return [("add", "fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, **({"ftz": "FTZ"} if flush else {}))),
            ("mul", "fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, **({"fmz": mul_flush} if flush else {}))),
            ("fma", "ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, **({"fmz": "FTZ"} if flush else {})))]


def vectors(floats):
    return [(floats[i], floats[(i * 7 + 3) % len(floats)], floats[(i * 5 + 1) % len(floats)]) for i in range(len(floats))]


def selection(c):
    edges = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x3f800000, 0xbf800000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f800001, 0xffc01234]
    for a, b in itertools.product(edges, repeat=2):
        for pred in (0, 1):
            for ftz in (False, True):
                fields, regs = dict(Rd=2, Ra=2, Rb=4, Pp=0, **({"ftz": "FTZ"} if ftz else {})), dict(R2=a, R4=b, P0=pred)
                native = {"__native": True} if a == b == 0 else {}
                x, y = (ftz32(a), ftz32(b)) if ftz else (a, b)
                c.add("fsel__RRR_RRR", fields, regs, dict(R2=x if pred else y, **native))
                c.add("fmnmx__RRR_RRR", fields, regs, dict(R2=fmnmx(x, y, bool(pred)), **native))
    # Three-input PTX min/max: numeric-wins NaN selection, canonical NaN under .NAN and IEEE
    # signed-zero ordering. Readable mode keeps the plain ordered-comparison formula.
    values = [0x3f800000, 0xbf800000, 0x40000000, 0, 0x80000000, 0x7f800000, 0xff800000, 0x7fc00000, 0x7f812345, 0xffc54321, 1, 0x80000001]
    exact = c.fp == "exact"
    for k in c.of("FMNMX3"):
        names = [source(k, *group.split("/")) for group in ("Ra/Sa", "Rb/Sb/URb", "Rc/Sc/URc")]
        if not all(names): continue
        for i, a in enumerate(values):
            vals = (a, values[(i + 3) % len(values)], values[(i + 5) % len(values)])
            for ftz, nan, predicate, inverted in itertools.product(c.labels(k, "ftz", ("noftz", "FTZ")), c.labels(k, "nan", ("nonan", "NAN")), (0, 1), (0, 1)):
                fields, regs = {"Rd": 8, "Pp": "P0", "Pp@not": inverted, "ftz": ftz, "nan": nan}, {"P0": predicate}
                if not all(put(k, n, v, fields, regs, None, r, ureg=10, imm=("F32Imm",)) for n, v, r in zip(names, vals, (2, 4, 6))): continue
                is_min = bool(predicate ^ inverted)
                prepared = tuple(map(ftz32, vals)) if exact and ftz == "FTZ" else vals
                if exact and nan == "NAN" and any(map(nan32, prepared)): want = 0x7fffffff
                else: want = fmnmx3_pick(fmnmx3_pick(prepared[0], prepared[1], is_min, exact), prepared[2], is_min, exact)
                c.add(k.name, fields, regs, {"R8": want, "__native": True})
    c.add("fmnmx_pred__RRR_RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Pp="PT"), {}, dict(error="sass_opaque_FMNMX"))
    # FMNMX.NAN returns canonical +NaN if either source is NaN, otherwise the ordinary rules.
    nan_values = [0x7fc12345, 0x7f812345, 0xffc54321, 0xff812345, 0x3f800000, 0xc0000000, 0, 0x80000000, 0x7f800000, 0xff800000]
    for k in (k for k in c.of("FMNMX", "fmnmx__") if "nan" in k.operand_types):
        src = source(k, "Rb", "Sb", "URb")
        if src is None or k.operand_types[src].type == "CX": continue
        for i, a in enumerate(nan_values):
            b = nan_values[(i * 3 + 1) % len(nan_values)]
            for ftz in ("noftz", "FTZ"):
                for is_min in (False, True):
                    fields = {"Rd": 8, "Ra": 2, "Pp": "PT", "Pp@not": int(not is_min), "nan": "NAN", "ftz": ftz}
                    if "xorsign" in k.operand_types: fields["xorsign"] = "noxorsign"
                    regs, memory = {"R2": a}, []
                    if not put(k, src, b, fields, regs, memory, 4, bank=(2, 0x100), imm=("F32Imm",)): continue
                    c.add(k.name, fields, regs, {"R8": fmnmx(a, b, is_min, True, ftz == "FTZ"), "__native": True}, memory=memory)
    # PTX min.xorsign.abs: select on magnitudes, then the XOR of the source signs unless NaN.
    for k in (k for k in c.of("FMNMX") if k.name == "fmnmx__RRR_RRR" and "xorsign" in k.operand_types):
        for a, b in [(0x3f800000, 0xc0000000), (0xbf800000, 0xc0400000), (0, 0x80000000), (1, 0x3f800000), (0x7fc12345, 0xbf800000), (0x7fc12345, 0xffc54321)]:
            for ftz, nan, is_min in itertools.product(("noftz", "FTZ"), ("nonan", "NAN"), (False, True)):
                fields = {"Rd": 8, "Ra": 2, "Rb": 4, "Pp": "PT", "Pp@not": int(not is_min), "xorsign": "XORSIGN", "ftz": ftz, "nan": nan}
                c.add(k.name, fields, {"R2": a, "R4": b}, {"R8": fmnmx_xorsign(a, b, is_min, nan == "NAN", ftz == "FTZ"), "__native": True})


def arithmetic(c):
    for cls, fields, regs, expected in [
            ("fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4), dict(R2=0x3f800000, R4=0x40000000), dict(R8=0x40400000)),
            ("fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4), dict(R2=0x3fc00000, R4=0x40000000), dict(R8=0x40400000)),
            ("ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6), dict(R2=0x3fc00000, R4=0x40000000, R6=0xbf800000), dict(R8=0x40000000)),
            ("fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, **{"Ra@negate": 1}), dict(R2=0x3f800000, R4=0x40000000), dict(R8=0x3f800000)),
            ("fadd__RRI_RI", dict(Rd=8, Ra=2, Sc=0x40000000), dict(R2=0x3f800000), dict(R8=0x40400000)),
            ("fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4), dict(R2=0x3f800000, R4=0x40000000), dict(R8=0x40400000, __max_ops=2)),
            ("fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, rnd="RZ"), dict(R2=0x3f800000, R4=0x33800001), dict(R8=0x3f800000))]:
        c.add(cls, fields, regs, expected)
    rng = random.Random("fp32")
    edges = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x3effffff, 0x4b800001]
    vs = vectors(edges + [rng.getrandbits(32) & 0xbfffffff for _ in range(10)] + [rng.getrandbits(32) for _ in range(10)])
    for x, y, z in vs:
        for rnd, flush, sat in itertools.product(ROUNDINGS, (False, True), (False, True)):
            for op, cls, fields in ops(flush):
                inputs = [x, y, z][:3 if op == "fma" else 2]
                want = softfloat.arithmetic(op, inputs, 32, rnd, flush, sat)
                if nan32(want) or (rnd == "RN" and underflow_band(op, inputs, 32)): continue
                c.add(cls, dict(fields, rnd=rnd, sat="SAT" if sat else "nosat"), dict(R2=x, R4=y, R6=z), dict(R8=want, __native=True))
    for scale, k in [("D2", -1), ("D8", -3), ("M2", 1), ("M8", 3)]:
        for x, y, _ in vs:
            for rnd in ROUNDINGS:
                want = softfloat.arithmetic("mul", [x, y], 32, rnd, False, False, k)
                if not (nan32(want) or rnd == "RN" and underflow_band("mul", [x, y], 32, k)):
                    c.add("fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, scale=scale, rnd=rnd), dict(R2=x, R4=y), dict(R8=want, __native=True))
    # Directed cases whose residual lies below fp64 precision, and exact cancellations
    # whose zero sign depends on the rounding mode.
    for op, inputs in [("add", [0x3f800000, 1]), ("add", [0x3f800000, 0x80000001]), ("add", [0x7f7fffff, 0x7f7fffff]),
                       ("add", [0x3f800000, 0xbf800000]), ("add", [0, 0x80000000]),
                       ("fma", [0x3f800000, 0x3f800000, 0xbf800000]), ("fma", [0x3f800001, 0x3f800001, 0x80000001]),
                       ("fma", [0x80000000, 0x3f800000, 0]), ("mul", [0x00800000, 0x3f000001])]:
        _, cls, fields = next(o for o in ops(False) if o[0] == op)
        for rnd in ROUNDINGS:
            c.add(cls, dict(fields, rnd=rnd), dict(zip(("R2", "R4", "R6"), inputs)), dict(R8=softfloat.arithmetic(op, inputs, 32, rnd), __native=True))


def compare(c):
    c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp=2, Ra=2, Rb=4, fcomp="LT", bop="AND"), dict(R2=0x3f800000, R4=0x40000000, P2=1), dict(P0=1, P1=0))
    c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp=0, Ra=2, Rb=4, fcomp="NAN", bop="AND"), dict(R2=0x7fc00000, R4=0, P0=1), dict(P0=1, P1=0))
    for a, b in [(0x3f800000, 0x40000000), (0x40000000, 0x3f800000), (0x3f800000, 0x3f800000), (0x7fc00000, 0x3f800000), (0x3f800000, 0xffc00000), (1, 0)]:
        for flush in (False, True):
            fa, fb = (None if p[0] == "nan" else p[2] for p in (softfloat.unpack(v, 32, flush) for v in (a, b)))
            for cmp in FCOMPARISONS[:12] + ("NUM", "NAN", "T", "F"):
                result = fcompare(fa, fb, cmp)
                c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp="PT", Ra=2, Rb=4, fcomp=cmp, bop="AND", **({"ftz": "FTZ"} if flush else {})),
                      dict(R2=a, R4=b), dict(P0=int(result), P1=int(not result)))


def readable_formulas(c):
    """The readable language's plain unfused formula, which ignores rounding modes and flushing."""
    if c.fp != "readable": return
    rng = random.Random("readable_fp32")
    edges = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f7fffff, 0x7f800000, 0x3effffff, 0x4b800001]
    for i, (x, y, z) in enumerate(vectors(edges + [rng.getrandbits(32) & 0xbfffffff for _ in range(10)] + [rng.getrandbits(32) for _ in range(6)])):
        rnd = ROUNDINGS[i % 4]
        for flush, sat in itertools.product((False, True), repeat=2):
            for op, cls, fields in ops(flush, mul_flush="FMZ"):
                want = readable(op, [x, y, z][:3 if op == "fma" else 2], 32, sat)
                if want is not None:
                    c.add(cls, dict(fields, rnd=rnd, sat="SAT" if sat else "nosat"), dict(R2=x, R4=y, R6=z), dict(R8=want, __native=True), portable=True)
        for scale, k in [("D2", -1), ("M8", 3)]:
            want = readable("mul", [x, y], 32, scale=k)
            if want is not None:
                c.add("fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, scale=scale, rnd=rnd), dict(R2=x, R4=y), dict(R8=want, __native=True), portable=True)
    # One operation per arithmetic operator, plus the result copy.
    c.add("ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, rnd="RZ"), dict(R2=0x3fc00000, R4=0x40000000, R6=0xbf800000), dict(R8=0x40000000, __max_ops=3), portable=True)
    c.add("fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, ftz="FTZ"), dict(R2=0x3f800000, R4=0x40000000), dict(R8=0x40400000, __max_ops=2), portable=True)
    nan = {"nan": "NAN"} if "nan" in c.classes["fmnmx__RRR_RRR"].operand_types else {}
    for a, b in [(1, 0x80000001), (0x3f800000, 0x40000000), (0x40000000, 0x3f800000), (0, 0x80000000), (0x007fffff, 1)]:
        for pred in (0, 1):
            fields = dict(Rd=2, Ra=2, Rb=4, Pp=0, ftz="FTZ")
            c.add("fsel__RRR_RRR", fields, dict(R2=a, R4=b, P0=pred), dict(R2=a if pred else b, __native=True), portable=True)
            c.add("fmnmx__RRR_RRR", dict(fields, **nan), dict(R2=a, R4=b, P0=pred), dict(R2=a if (f32(a) < f32(b)) == bool(pred) else b, __native=True), portable=True)
        c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp="PT", Ra=2, Rb=4, fcomp="LT", bop="AND", ftz="FTZ"),
              dict(R2=a, R4=b), dict(P0=int(f32(a) < f32(b)), P1=int(not f32(a) < f32(b)), __native=True), portable=True)


def emulator_cases(c):
    selection(c); arithmetic(c); compare(c); readable_formulas(c)


def gpu_fixtures():
    from tests.harness import scalar
    out = [scalar.launch(f, underflow=True) for f in ("fadd", "fmul", "ffma")]
    out += scalar.generic("fsetp", "fsel", "fadd_ftz", "ffma_sat", *sorted(f for f in scalar.ROUNDED if f[0] == "f"))
    out += [scalar.launch(f, lambda x, y, _, f=f: fmnmx(x, y, f.startswith("fmin"), f.endswith("_nan"), "ftz" in f))
            for f in ("fmin", "fmax", "fmin_nan", "fmax_nan", "fmin_ftz_nan", "fmax_ftz_nan", "fmin_ftz", "fmax_ftz")]
    out.append(scalar.launch("fmin_xorsign", lambda x, y, _: fmnmx_xorsign(x, y, True)))
    return out
