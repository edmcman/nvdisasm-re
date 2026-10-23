"""Defined operators (family.definition, bitvec.lowered): the names arrive in lifted instructions,
an operator computes exactly its function, and names survive substitution and constants."""
import random
import pytest
from sass.assemble import assemble_instruction
from sass.semantics import bitvec, lifter
from sass.semantics.bitvec import const, param, reg
from sass.semantics.evaluate import evaluate
from sass.semantics.family import definition
from sass.semantics.families import convert, f2fp, f2i, floating
from sass.semantics.fp import fpformat

R = lambda n: reg("R", n, 1)
SPECIAL32 = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f7fffff,
             0x7f800000, 0xff800000, 0x7fc00000, 0xffc00001, 0x7f800001, 0x477fe000, 0x477ff000, 0x33000000]
VALUES32 = SPECIAL32 + [random.Random(7).getrandbits(32) for _ in range(200)]
F16_DEST = fpformat.Dest(10, -14, 0x7bff, 0, 0x7fff, 1, 0x7c00)
X, Y, P = param("x", 32), param("y", 32), param("p", 1)


def defined(e):
    """The defined operator nodes in e."""
    seen, out, stack = set(), [], [e]
    while stack:
        n = stack.pop()
        if not isinstance(n, bitvec.Expr) or n in seen: continue
        seen.add(n)
        if n.op in bitvec.DEFINED: out.append(n)
        stack.extend(n.args)
    return out


def operators(arch, text):
    """{name: [arguments]} of the defined operators in a lifted instruction's writes."""
    found = {}
    for _, v in lifter.lift(arch, assemble_instruction(arch, text)).writes:
        for n in defined(v): found.setdefault(n.op, []).append(n.args)
    return found


@pytest.mark.parametrize("text, name, arguments", [
    ("FFMA.FTZ R0, R2, R3, R4", "ftz", [(R(2),), (R(3),), (R(4),)]),
    ("FMNMX R0, R2, R3, PT", "min.f32", [(R(2), R(3))]),
    ("FMNMX R0, R2, R3, !PT", "max.f32", [(R(2), R(3))]),
    ("FMNMX R0, R2, R3, P0", "fmnmx", [(reg("P", 0, 1, element=1), R(2), R(3))]),
    ("F2F.F16.F32 R0, R2", "cvt.rn.f16.f32", [(R(2),)]),
    ("F2F.F16.F32.RZ R0, R2", "cvt.rz.f16.f32", [(R(2),)]),
    ("F2F.F64.F32 R0, R2", "cvt.f64.f32", [(R(2),)]),
    ("F2FP.F16.F32.PACK_AB R0, R2, R3", "f2fp.pack.rn.f16x2.f32", [(R(2), R(3))]),
    ("F2FP.BF16.F32.PACK_AB.RZ R0, R2, R3", "f2fp.pack.rz.bf16x2.f32", [(R(2), R(3))]),
    ("F2FP.SATFINITE.RELU.BF16.F32.PACK_AB R0, R2, R3", "f2fp.pack.rn.satfinite.relu.bf16x2.f32", [(R(2), R(3))]),
    ("FMNMX.NAN R0, R2, R3, PT", "min.NaN.f32", [(R(2), R(3))]),
    ("FMNMX.NAN R0, R2, R3, P1", "fmnmx.NaN", [(reg("P", 1, 1, element=1), R(2), R(3))]),
    ("FADD.SAT R0, R2, R3", "sat", None),
    ("FRND.FLOOR R0, R2", "cvt.rmi.f32.f32", [(R(2),)]),
    ("F2I.TRUNC.NTZ R0, R2", "cvt.rzi.ntz.s32.f32", [(R(2),)]),
    ("F2I.U32.CEIL R0, R2", "cvt.rpi.u32.f32", [(R(2),)]),
    ("F2IP.S8.F32.NTZ R0, R2, R3, RZ", "cvt.rni.sat.s8.f32", [(R(2),), (R(3),)]),
    ("FRND.FTZ.FLOOR R0, R2", "ftz", [(R(2),)]),
    ("DSETP.MIN.AND P0, PT, R2, R4, PT", "setp.min.f64", [(reg("R", 2, 2), reg("R", 4, 2))]),
    ("HMUL2.FMZ R0, R2, R3", "fmz", None),
    ("FCHK P0, R2, R3", "fchk.divide", [(R(2), R(3))]),
    ("I2F R0, R2", "cvt.rn.f32.s32", [(R(2),)]),
    ("I2F.F16.U32.RZ R0, R2", "cvt.rz.f16.u32", [(R(2),)]),
    ("BMSK R0, R2, R3", "bmsk.clamp.b32", [(R(2), R(3))]),
    ("IABS R0, R2", "abs.s32", [(R(2),)]),
    ("IMAD.WIDE R0, R2, R3, R4", "mul.wide.s32", [(R(2), R(3))]),
    ("PRMT R0, R2, 0x3210, R3", "prmt.b32", [(R(2), const(0x3210, 16), R(3))]),
    ("PRMT R0, R2, R4, R3", "prmt.b32", [(R(2), R(4), R(3))]),
    ("SHF.L.W.U32.HI R0, R2, R4, R3", "shf.l.wrap.hi.u32", [(R(2), R(3), R(4))]),
    ("BREV R0, R2", "brev.b32", [(R(2),)]),
    ("POPC R0, R2", "popc.b32", [(R(2),)]),
    ("FLO.U32.SH R0, R2", "bfind.shiftamt.u32", [(R(2),)]),
    ("SGXT R0, R2, 0x8", "szext.clamp.s32", [(R(2), const(8, 32))]),
    ("IMNMX R0, R2, R3, PT", "min.s32", [(R(2), R(3))]),
    ("IMNMX.U32 R0, R2, R3, P1", "imnmx.u32", [(reg("P", 1, 1, element=1), R(2), R(3))]),
    ("VABSDIFF4.U8 R0, R2, R3, R4", "vabsdiff4.u8.merge", [(R(2), R(3))]),
    ("IDP.4A.S8.S8 R0, R2, R3, R4", "dp4a.s32.s32", [(R(2), R(3), R(4))]),
    ("I2IP.S8.S32.SAT R0, R2, R3, R4", "cvt.pack.sat.s8.s32", [(R(2), R(3), R(4))]),
])
def test_names_arrive(text, name, arguments):
    found = operators("SM89", text)
    assert name in found, found
    for want in arguments or (): assert any(len(got) == len(want) and all(g is w for g, w in zip(got, want)) for got in found[name])


def test_ftz_inside_fmnmx():
    found = operators("SM89", "FMNMX.FTZ R0, R2, R3, PT")
    assert set(found) == {"min.f32", "ftz"}
    (args,) = found["min.f32"]
    assert [a.op for a in args] == ["ftz", "ftz"]


def same(operator, function, *xs, values=VALUES32):
    """The operator evaluates as the undecorated function, on every combination drawn from values."""
    assert operator.op in bitvec.DEFINED
    for k in range(len(values)):
        env = {x.args[0]: values[(k * (j + 1) * 7) % len(values)] for j, x in enumerate(xs)}
        assert evaluate(operator, None, env) == evaluate(function, None, env), env


@pytest.mark.parametrize("arch, text, names", [
    ("SM90", "HMNMX2 R0, R2, R3, PT", {"min.f16"}),
    ("SM90", "HMNMX2.BF16_V2 R0, R2, R3, !PT", {"max.bf16"}),
    ("SM90", "HMNMX2.NAN R0, R2, R3, P1", {"hmnmx.NaN.f16"}),
    ("SM90", "HMNMX2.XORSIGN R0, |R2|, |R3|, PT", {"min.xorsign.f16"}),
    ("SM90", "FMNMX.XORSIGN R0, |R2|, |R3|, PT", {"min.xorsign.abs.f32"}),
    ("SM90", "FMNMX.NAN.XORSIGN R0, |R2|, |R3|, !PT", {"max.NaN.xorsign.abs.f32"}),
    ("SM90", "FMNMX.XORSIGN R0, R2, -R3, P2", {"fmnmx.xorsign"}),
    ("SM90", "F2I.FTZ.BF16 R0, R2", {"cvt.rni.s32.bf16", "ftz.bf16"}),
    ("SM90", "VIMNMX.S16x2.RELU R0, R2, R3, !PT", {"relu.s32", "max.s32"}),
])
def test_more_names(arch, text, names):
    assert names <= operators(arch, text).keys()


def test_f64_nan_sources():
    """FP64 arithmetic's NaN result comes from the registers before -/|.|: they follow the operands
    when one is modified, and the expansion matches the arithmetic on special and random values."""
    D = lambda n: reg("R", n, 2)
    assert operators("SM89", "DFMA R0, R2, R4, R6")["fma.rn.f64"] == [(D(2), D(4), D(6))]
    (args,) = operators("SM89", "DFMA R0, -R2, R4, R6")["fma.rn.f64"]
    assert args[3:] == (D(2), D(4), D(6)) and args[0] is not D(2)
    assert operators("SM89", "DADD R0, R2, -R4")["add.rn.f64"][0][2:] == (D(2), D(4))
    a, b, c = (param(n, 64) for n in "abc")
    values = [v << 32 | v for v in SPECIAL32] + [0x7ff0000000000001, 0xfff8000000000123, 0x8000000000000000] + [random.Random(5).getrandbits(64) for _ in range(150)]
    for nans in ((), (a, b, c)):
        neg = a ^ (1 << 63)
        same(floating.fma_f64(neg, b, c, "RN", nans), floating.fma_f64.body(neg, b, c, "RN", nans), a, b, c, values=values)
        same(floating.add_f64(neg, c, "RZ", nans[::2]), floating.add_f64.body(neg, c, "RZ", nans[::2]), a, c, values=values)
        same(floating.mul_f64(neg, b, "RM", nans[:2]), floating.mul_f64.body(neg, b, "RM", nans[:2]), a, b, values=values)


def test_readable_names():
    sem = lifter.lift("SM89", assemble_instruction("SM89", "FMNMX R0, R2, R3, PT"), fp="readable")
    assert {n.op for _, v in sem.writes for n in defined(v)} == {"min.readable.f32"}


def test_expansion_integer():
    from sass.semantics.families import integer
    same(integer.reversed_bits(X), integer.reversed_bits.body(X), X)
    same(integer.population(X), integer.population.body(X), X)
    for signed in (False, True):
        for shift in (False, True): same(integer.find_leading(X, signed, shift), integer.find_leading.body(X, signed, shift), X)
        same(integer.dot_product(X, Y, X, "4A", signed, True), integer.dot_product.body(X, Y, X, "4A", signed, True), X, Y)
        same(integer.byte_differences(X, Y, signed), integer.byte_differences.body(X, Y, signed), X, Y)
    for mode in ("wrap", "clamp"):
        same(integer.bit_mask(X, Y, mode), integer.bit_mask.body(X, Y, mode), X, Y, values=list(range(40)) + VALUES32)
        same(integer.extended(X, Y, mode, True), integer.extended.body(X, Y, mode, True), X, Y, values=list(range(40)) + VALUES32)
    for mode in integer.PRMT_MODES: same(integer.permute(X, Y, Y, mode), integer.permute.body(X, Y, Y, mode), X, Y)


def test_chosen():
    """A choice of constants becomes the same choice of f's values, keys and rows kept."""
    from sass.sleigh.lower import sel
    code = sel(("k",), [((0,), const(0, 2)), ((1,), const(1, 2)), ((3,), const(2, 2))], 2)
    chosen = bitvec.chosen(code, lambda v: X + v)
    assert chosen.op == "sel" and chosen.args[0] == ("k",) and [v for v, _ in chosen.args[1]] == [(0,), (1,), (3,)]
    assert [t for _, t in chosen.args[1]] == [X, X + 1, X + 2]
    assert bitvec.chosen(const(2, 2), lambda v: X + v) is X + 2
    assert bitvec.chosen(code, lambda v: X) is X


def test_fmnmx3_names():
    assert operators("SM100", "FMNMX3 R0, R2, R3, R4, PT").keys() == {"min3.f32"}
    assert operators("SM100", "FMNMX3.NAN R0, R2, R3, R4, !PT").keys() == {"max3.NaN.f32"}


@pytest.mark.parametrize("nan", (False, True))
def test_expansion_floating(nan):
    Z = param("z", 32)
    same(floating.flushed(X), floating.flushed.body(X), X)
    same(floating.saturated(X), floating.saturated.body(X), X)
    for p in (const(1, 1), const(0, 1)):
        same(floating.fmnmx(p, X, Y, nan), floating.fmnmx.body(p, X, Y, nan), X, Y)
        same(floating.fmnmx3(p, X, Y, Z, nan), floating.fmnmx3.body(p, X, Y, Z, nan), X, Y, Z)
    for p in (0, 1):
        same(floating.fmnmx(P, X, Y, nan), floating.fmnmx.body(P, X, Y, nan), X, Y, P, values=[v | p for v in VALUES32])


@pytest.mark.parametrize("mode", fpformat.ROUNDING)
def test_expansion_conversions(mode):
    """The expansion of a name with constants in it (cvt.rz.f16.f32) rebuilds those constants."""
    r = fpformat.rounding(mode)
    same(convert.converted("F32", "F16", *r, X), convert.converted.body("F32", "F16", *r, X), X)
    same(convert.converted("F64", "F32", *r, param("d", 64)), convert.converted.body("F64", "F32", *r, param("d", 64)), param("d", 64),
         values=[v << 32 | v for v in VALUES32])
    same(f2fp.pack16_lane(F16_DEST, r, 0, 0, X), f2fp.pack16_lane.body(F16_DEST, r, 0, 0, X), X)
    same(f2fp.pack16(F16_DEST, r, 1, 1, X, Y), f2fp.pack16.body(F16_DEST, r, 1, 1, X, Y), X, Y)
    same(convert.integral("F32", r, X), convert.integral.body("F32", r, X), X)
    for signed in (0, 1):
        for nan in (0, 0x80000000):
            args = (X, "F32", 32, const(signed, 1), r, const(nan, 32))
            same(convert.float_to_int(*args), convert.float_to_int.body(*args), X)
    for signed in (False, True): same(f2i.f32_to_8bit(X, signed), f2i.f32_to_8bit.body(X, signed), X)


def test_names_without_constants():
    flags = [param(f"r{k}", 1) for k in range(4)]
    general = convert.converted("F32", "F16", *flags, X)
    assert general.op == "cvt.f16.f32" and general.args == (*flags, X)
    lane = f2fp.pack16_lane(F16_DEST._replace(mbits=param("mbits", 32)), fpformat.rounding("RN"), 0, 0, X)
    assert lane.op == "f2fp.cvt" and lane.args[0] is param("mbits", 32) and lane.args[-1] is X
    assert f2fp.pack16_lane(F16_DEST, fpformat.rounding("RZ"), 0, 0, X).op == "f2fp.cvt.rz.f16.f32"


@definition(lambda a: ("test.lossy", ["x"]))
def lossy(x, k):
    """A namer that drops k without naming it."""
    return x + k


def test_a_name_determines_its_constants():
    """A name stands for the arguments it drops: a call disagreeing with them is an error, and a
    name that isn't F2FP's destination falls back to the general one."""
    assert f2fp.pack16_lane(F16_DEST._replace(nan_signed=1), fpformat.rounding("RN"), 0, 0, X).op == "f2fp.cvt"
    lossy(X, 1)
    with pytest.raises(TypeError, match="doesn't determine"): lossy(X, 2)


def test_substitute_keeps_names():
    e = floating.fmnmx(const(1, 1), X, floating.flushed(Y))
    s = bitvec.substitute(e, lambda n: R(5) if n is X else None)
    assert s.op == "min.f32" and s.args[0] is R(5) and s.args[1].op == "ftz"
    c = bitvec.substitute(e, {X: const(0x3f800000, 32), Y: const(1, 32)}.get)
    assert c.op == "min.f32" and bitvec.lowered(c) is const(0, 32)
    assert bitvec.show([(None, c)]) == "min.f32(0x3f800000, ftz(0x1)) = 0x0"


def test_constants_keep_names():
    e = bitvec.fadd(floating.flushed(const(1, 32)), const(0x3f800000, 32))
    assert bitvec.show([("R0", e)]) == "R0 := nanresult(fadd(ftz(0x1), 0x3f800000, 'RN'), 0x7fffffff) = 0x3f800000"
    assert bitvec.lowered(e) is const(0x3f800000, 32)


def test_lowered_is_the_eager_expansion():
    e = bitvec.concat(floating.fmnmx(const(1, 1), X, floating.flushed(Y)), floating.flushed(X))
    plain = bitvec.lowered(e)
    assert not defined(plain)
    assert plain is bitvec.concat(floating.numeric_minmax(const(1, 1), X, floating.flushed.body(Y)), floating.flushed.body(X))


def test_show():
    sem = lifter.lift("SM89", assemble_instruction("SM89", "FADD.FTZ R0, R2, R3"))
    writes = [(str(l), v) for l, v in sem.writes]
    assert bitvec.show(writes) == "R0 := ftz(nanresult(fadd(ftz(R2), ftz(R3), 'RN'), 0x7fffffff))"
    described = bitvec.show(writes, describe=True).splitlines()
    assert described[1].startswith("# ftz: Flush to zero")
    expanded = bitvec.show(writes, expand=True).splitlines()
    assert "ftz(" not in expanded[0]
    assert {"╰ ftz(R2)", "╰ ftz(R3)", "╰ ftz(t0)"} <= {l.strip(" │") for l in expanded}


@definition("test.duplicate")
def first(x):
    """One."""
    return x + 1


@definition("test.duplicate")
def second(x):
    """Two."""
    return x + 2


def test_a_name_means_one_definition():
    first(X)
    with pytest.raises(TypeError, match="test.duplicate"): second(X)
