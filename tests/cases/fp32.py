"""FP32: FADD/FMUL/FFMA (every rounding mode, FTZ, SAT, FMUL scale) against the exact softfloat
model; FSETP and FSET; SM120's uniform UFADD/UFMUL/UFFMA/UFSETP/UFSEL; FSEL/FMNMX/FMNMX3 selection rules; FCHK's division slow-path test; readable-lowering
formulas."""
from tests.harness.manual import retained
import itertools, random
from sass.semantics.fp import softfloat
from tests.harness.case import put, source
from tests.refs.bits import f32, ftz32, nan32
from tests.refs.float import FCOMPARISONS, fchk_divide, fcompare, fset, fmnmx, fmnmx3_pick, fmnmx_xorsign, fmz, ordered, readable, rounded, underflow_band

ROUNDINGS = ("RN", "RZ", "RM", "RP")


def ops(flush, mul_flush="FTZ"):
    """(op, class, fields) for FADD/FMUL/FFMA with Rd=R8 and sources R2, R4, R6."""
    return [("add", "fadd__RRR_RR", dict(Rd=8, Ra=2, Rc=4, **({"ftz": "FTZ"} if flush else {}))),
            ("mul", "fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, **({"fmz": mul_flush} if flush else {}))),
            ("fma", "ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, **({"fmz": "FTZ"} if flush else {})))]


def vectors(floats):
    return [(floats[i], floats[(i * 7 + 3) % len(floats)], floats[(i * 5 + 1) % len(floats)]) for i in range(len(floats))]


@retained('Dense Cartesian signed-zero/NaN selection, constant PT, XORSIGN and partial predicate-output regressions exceed the form profile.')
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


@retained('Seeded rounding/cancellation and exponent boundaries, directed residuals, dense FMZ zero/Inf/NaN factor pairs and p-code costs exceed the form profile.')
def arithmetic(c):
    for cls, fields, regs, expected in [
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
    # .FMZ: a zero or subnormal factor gives a +0 product even times Inf or NaN, then FFMA adds Rc.
    factors = [0, 0x80000000, 1, 0x807fffff, 0x3f800000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00001]
    for x, y in itertools.product(factors, repeat=2):
        for rnd in ("RN", "RM"):
            for op, cls, fields in ops(True, mul_flush="FMZ")[1:]:
                for z in ((0x80000000, 0x7fc00000, 1, 0xbf800000) if op == "fma" else (0,)):
                    want = fmz(op, [x, y, z][:3 if op == "fma" else 2], rnd)
                    if not nan32(want):
                        c.add(cls, dict(fields, fmz="FMZ", rnd=rnd), dict(R2=x, R4=y, R6=z), dict(R8=want, __native=True))


@retained('Exceptional comparison pairs and constant PT/aliased-predicate regressions exceed the form profile.')
def compare(c):
    c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp=0, Ra=2, Rb=4, fcomp="NAN", bop="AND"), dict(R2=0x7fc00000, R4=0, P0=1), dict(P0=1, P1=0))
    for a, b in [(0x3f800000, 0x40000000), (0x40000000, 0x3f800000), (0x3f800000, 0x3f800000), (0x7fc00000, 0x3f800000), (0x3f800000, 0xffc00000), (1, 0)]:
        for flush in (False, True):
            fa, fb = (None if p[0] == "nan" else p[2] for p in (softfloat.unpack(v, 32, flush) for v in (a, b)))
            for cmp in FCOMPARISONS[:12] + ("NUM", "NAN", "T", "F"):
                result = fcompare(fa, fb, cmp)
                c.add("fsetp__RRR_RRR", dict(Pu=0, Pv=1, Pp="PT", Ra=2, Rb=4, fcomp=cmp, bop="AND", **({"ftz": "FTZ"} if flush else {})),
                      dict(R2=a, R4=b), dict(P0=int(result), P1=int(not result)))


# Ordered/unordered, signed-zero, denormal (equal to zero under FTZ) and infinity pairs.
SET_PAIRS = [(0x3f800000, 0x40000000), (0x40000000, 0x3f800000), (0x3f800000, 0x3f800000), (0, 0x80000000), (0x7fc00000, 0x3f800000),
             (0x3f800000, 0xffc00000), (1, 0), (0x80000001, 1), (0x7f800000, 0x7f7fffff), (0xff800000, 0xff800000), (0x7fc00000, 0x7fc00000)]


@retained('Exceptional FSET pairs with constant PT and a same-source/output signed-zero alias exceed the form profile.')
def fset_cases(c):
    """Constant-PT exceptional pairs and a same-source/output alias; ordinary forms use profiles."""
    for (a, b), cmp, ftz in itertools.product(SET_PAIRS, FCOMPARISONS + ("T", "F"), (False, True)):
        c.add("fset__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Pp="PT", fcomp=cmp, bop="AND", ftz="FTZ" if ftz else "noftz"), dict(R2=a, R4=b),
              dict(R8=fset(a, b, cmp, ftz=ftz and c.fp == "exact"), __native=True))
    c.add("fset__RRR_RRR", dict(Rd=2, Ra=2, Rb=2, Pp="PT", fcomp="EQ", bop="AND", **{"Rb@negate": 1}), dict(R2=0x80000000), dict(R2=0x3f800000, __native=True))


@retained('Uniform seeded arithmetic, exceptional comparison pairs and constant UPT inputs exceed the generated form profile.')
def uniform(c):
    """SM120's uniform twins of FADD/FMUL/FFMA/FSETP/FSEL: every rounding mode and FTZ over edge
    triples, exceptional comparison pairs and constant UPT inputs."""
    edges = [0, 0x80000000, 1, 0x80000001, 0x00800000, 0x3f800000, 0xbf800000, 0x3fc00000, 0x7f7fffff, 0x7f800000, 0xff800000, 0x4b800001]
    regs = lambda x, y, z: dict(UR2=x, UR4=y, UR6=z)
    for (x, y, z), rnd, flush in itertools.product(vectors(edges), ROUNDINGS, (False, True)):
        for op, cls, fields in [("add", "ufadd__URURUR_UUU", dict(URd=8, URa=2, URc=4, ftz="FTZ" if flush else "noftz")),
                                ("mul", "ufmul__UUU", dict(URd=8, URa=2, URb=4, fmz="FTZ" if flush else "nofmz_hfma2")),
                                ("fma", "uffma__UUU_UUU", dict(URd=8, URa=2, URb=4, URc=6, fmz="FTZ" if flush else "nofmz_hfma2"))]:
            inputs = [x, y, z][:3 if op == "fma" else 2]
            want = softfloat.arithmetic(op, inputs, 32, rnd, flush)
            if nan32(want) or (rnd == "RN" and underflow_band(op, inputs, 32)): continue
            c.add(cls, dict(fields, rnd=rnd), regs(x, y, z), dict(UR8=want, __native=True))
    for (a, b), cmp, bop, p in itertools.product(SET_PAIRS, FCOMPARISONS + ("T", "F"), ("AND", "OR", "XOR"), (0, 1)):
        result = fcompare(ordered(a, 32), ordered(b, 32), cmp)
        combine = {"AND": lambda x: x and p, "OR": lambda x: x or p, "XOR": lambda x: x != p}[bop]
        c.add("ufsetp__URURUR_UUU", dict(UPu=0, UPv=1, URa=2, URb=4, UPp=3, fcomp=cmp, bop=bop), dict(UR2=a, UR4=b, UP3=p),
              dict(UP0=int(combine(result)), UP1=int(combine(not result)), __native=True), portable=True)
    c.add("ufsetp__URsIUR_URIR", dict(UPu=0, UPv=1, URa=2, Sb=0x40000000, UPp="UPT", fcomp="LT", bop="AND"), dict(UR2=0x3f800000),
          dict(UP0=1, UP1=0, __native=True), portable=True)
    for p, inverted in itertools.product((0, 1), (0, 1)):
        want = 0x3f800000 if bool(p) != bool(inverted) else 0xc0000000
        c.add("ufsel__URURUR_UUU", dict(URd=8, URa=2, URb=4, UPp=3, **{"UPp@not": inverted}), dict(UR2=0x3f800000, UR4=0xc0000000, UP3=p),
              dict(UR8=want, __native=True), portable=True)
        c.add("ufsel__URsIUR_URIR", dict(URd=8, URa=2, Sb=0xc0000000, UPp=3, **{"UPp@not": inverted}), dict(UR2=0x3f800000, UP3=p),
              dict(UR8=want, __native=True), portable=True)


@retained('Seeded unfused/underflow, FMZ and p-code-cost regressions exceed the readable form profile.')
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


def fchk_pairs():
    from tests.harness import autocase_profiles
    return list(autocase_profiles.fchk_pairs())


def emulator_cases(c):
    selection(c); arithmetic(c); compare(c); fset_cases(c); uniform(c); readable_formulas(c)


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    out = [scalar.launch(f, lambda *v, op=f[1:]: rounded(op, v[:3 if op == "fma" else 2], 32), underflow=True, arch=arch) for f in ("fadd", "fmul", "ffma")]
    out += [scalar.launch(f, lambda *v, op=f[1:4], m=f[-2:].upper(): rounded(op, v[:3 if op == "fma" else 2], 32, m), arch=arch)
            for f in sorted(f for f in scalar.ROUNDED if f[0] == "f")]
    out += [scalar.launch("fsetp", lambda a, b, _: int(fcompare(ordered(a, 32), ordered(b, 32), "LT")), arch=arch),
            scalar.launch("fsel", lambda a, b, c: a ^ 0x80000000 if c else b, arch=arch),
            scalar.launch("fadd_ftz", lambda a, b, _: rounded("add", [a, b], 32, ftz=True), arch=arch),
            scalar.launch("ffma_sat", lambda *v: rounded("fma", v, 32, sat=True), arch=arch)]
    out += [scalar.launch(f, lambda x, y, _, f=f: fmnmx(x, y, f.startswith("fmin"), f.endswith("_nan"), "ftz" in f), arch=arch)
            for f in ("fmin", "fmax", "fmin_nan", "fmax_nan", "fmin_ftz_nan", "fmax_ftz_nan", "fmin_ftz", "fmax_ftz")]
    out.append(scalar.launch("fmin_xorsign", lambda x, y, _: fmnmx_xorsign(x, y, True, absolute=True), arch=arch))
    out += [scalar.launch(f, lambda a, b, c, s=s: fset(a, b, s["fcomp"], s.get("bop", "AND"), (c != 0) != bool(s.get("Pp@not")), s.get("ftz") == "FTZ"), arch=arch)
            for f, (m, s) in scalar.SETS.items() if m == "FSET"]
    # FCHK P0, R2, R5 in a probe slot, P0 read back through P2R into the stored R11.
    from tests.harness.gpu import encode
    from tests.harness.templates import slot_fixture
    nop, p2r = encode("nop_", arch=arch), encode("p2r__RsIR_RIR", Rd=11, Pr="PR", Ra="RZ", Sb=1, insert="B0", arch=arch)
    pairs = fchk_pairs()
    for n in range(len(pairs) // 128):
        rows = pairs[128 * n:128 * (n + 1)]
        mods = {"Ra@negate": n & 1, "Rb@absolute": n >> 1 & 1}
        fchk = encode("fchk__RRR_RR", Pu=0, Ra=2, Rb=5, arch=arch, **mods)
        out.append(slot_fixture(f"fchk_{n}", [fchk] + [nop] * 7, [(a, b, 0) for a, b in rows],
                                [fchk_divide(a, b) for a, b in rows], 0, final=p2r, arch=arch))
    return out
