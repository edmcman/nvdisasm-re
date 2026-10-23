"""Defined operators (family.definition, bitvec.lowered): the names arrive in lifted instructions,
an operator computes exactly its function, and names survive substitution and constants."""
import random
import pytest
from sass.assemble import assemble_instruction
from sass.semantics import bitvec, lifter
from sass.semantics.bitvec import const, param, reg
from sass.semantics.evaluate import evaluate
from sass.semantics.family import definition
from sass.semantics.families import convert, f2fp, floating
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
])
def test_names_arrive(text, name, arguments):
    found = operators("SM89", text)
    assert name in found, found
    for want in arguments: assert any(len(got) == len(want) and all(g is w for g, w in zip(got, want)) for got in found[name])


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


def test_expansion_floating():
    same(floating.flushed(X), floating.flushed.body(X), X)
    same(floating.min_f32(X, Y), floating.min_f32.body(X, Y), X, Y)
    same(floating.max_f32(X, Y), floating.max_f32.body(X, Y), X, Y)
    for p in (0, 1):
        same(floating.fmnmx(P, X, Y), floating.fmnmx.body(P, X, Y), X, Y, P, values=[v | p for v in VALUES32])


@pytest.mark.parametrize("mode", fpformat.ROUNDING)
def test_expansion_conversions(mode):
    """The expansion of a name with constants in it (cvt.rz.f16.f32) rebuilds those constants."""
    r = fpformat.rounding(mode)
    same(convert.converted("F32", "F16", *r, X), convert.converted.body("F32", "F16", *r, X), X)
    same(convert.converted("F64", "F32", *r, param("d", 64)), convert.converted.body("F64", "F32", *r, param("d", 64)), param("d", 64),
         values=[v << 32 | v for v in VALUES32])
    same(f2fp.pack16_lane(F16_DEST, r, 0, 0, X), f2fp.pack16_lane.body(F16_DEST, r, 0, 0, X), X)
    same(f2fp.pack16(F16_DEST, r, 1, 1, X, Y), f2fp.pack16.body(F16_DEST, r, 1, 1, X, Y), X, Y)


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
    e = floating.min_f32(X, floating.flushed(Y))
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
    e = bitvec.concat(floating.min_f32(X, floating.flushed(Y)), floating.flushed(X))
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
