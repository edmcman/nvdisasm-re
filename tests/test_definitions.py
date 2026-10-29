"""Defined operators (family.definition, bitvec.lowered): the names arrive in lifted instructions,
an operator computes exactly its function, and names survive substitution and constants."""
import random
from itertools import product
import pytest
from sass.assemble import assemble_instruction
from sass.semantics import bitvec, family, lifter
from sass.semantics.bitvec import const, param, reg
from sass.semantics.evaluate import State, evaluate, execute
from sass.semantics.family import definition
from sass.semantics.families import convert, f2fp, f2i, floating, integer
from sass.semantics.fp import fpformat

R = lambda n: reg("R", n, 1)
SPECIAL32 = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x3f800000, 0xbf800000, 0x7f7fffff,
             0x7f800000, 0xff800000, 0x7fc00000, 0xffc00001, 0x7f800001, 0x477fe000, 0x477ff000, 0x33000000]
VALUES32 = SPECIAL32 + [random.Random(7).getrandbits(32) for _ in range(200)]
F16_DEST = fpformat.Dest(10, -14, 0x7bff, 0, 0x7fff, 1, 0x7c00)
X, Y, P = param("x", 32), param("y", 32), param("p", 1)


def test_lut3_complete_domain():
    """Check the named lookup and its expansion against all eight entries of every table."""
    table = param("table", 8)
    p, q, r = (param(name, 1) for name in ("p", "q", "r"))
    expression = integer.lut3(table, p, q, r)
    assert expression.op == "lut3" and expression.w == 1
    assert expression.args == (table, p, q, r)
    expanded = bitvec.lowered(expression)
    for value in range(256):
        for a, b, c in product((0, 1), repeat=3):
            inputs = dict(table=value, p=a, q=b, r=c)
            expected = value >> (4 * a + 2 * b + c) & 1
            assert evaluate(expression, None, inputs) == expected
            assert evaluate(expanded, None, inputs) == expected
    constant = integer.lut3(const(0xa8, 8), const(1, 1), const(0, 1), const(1, 1))
    assert constant.op == "lut3"
    assert bitvec.lowered(constant) is const(1, 1)


@pytest.mark.parametrize("arch", ("SM75", "SM80", "SM86", "SM89", "SM90",
                                  "SM100", "SM101", "SM103", "SM107", "SM120"))
@pytest.mark.parametrize("fp", ("exact", "readable"))
@pytest.mark.parametrize("instruction,file,third", [
    ("PLOP3", "P", "P4"), ("PLOP3", "P", "UP4"), ("UPLOP3", "UP", "UP4"),
])
@pytest.mark.parametrize("aliased,constant", [(False, False), (True, False), (False, True)])
def test_plop3_lut3_names_and_writes(arch, fp, instruction, file, third, aliased, constant):
    prefix = "UP" if file == "UP" else "P"
    dest = 2 if aliased else 0
    second = dest if aliased else 1
    source = ("UPT" if file == "UP" else "PT") if constant else f"{prefix}2"
    text = f"{instruction}.LUT {prefix}{dest}, {prefix}{second}, !{source}, {prefix}3, !{third}, 0xa8, 0x96"
    sem = lifter.lift(arch, assemble_instruction(arch, text), fp=fp)
    calls = [node for _, value in sem.writes for node in defined(value)]
    assert len(calls) == 2
    assert all(node.op == "lut3" and node.w == 1 and
               tuple(arg.w for arg in node.args) == (8, 1, 1, 1) for node in calls)
    assert {node.args[0].args[0] for node in calls} == {0xa8, 0x96}
    for p, q, r in product((0, 1), repeat=3):
        third_file = "UP" if third.startswith("UP") else "P"
        state = State({(file, 2): p, (file, 3): q, (third_file, 4): r})
        index = 4 * ((1 if constant else p) ^ 1) + 2 * q + (r ^ 1)
        got = execute(sem, state)
        assert got.read(file, dest, 1) == (0xa8 >> index & 1)
        if not aliased:
            assert got.read(file, second, 1) == (0x96 >> index & 1)


@pytest.mark.parametrize("reverse", [False, True])
def test_narrow_exception_policies_have_independent_definitions(reverse):
    policies = [{}, {"nan": (True, 0x7fff)}, {"overflow": (False, 0x7bff)},
                {"nan": (False, 0x7e00), "overflow": (True, 0x7bff)}]
    if reverse: policies.reverse()
    expressions = [fpformat.narrow(X, "F32", "F16", "RZ", **policy) for policy in policies]
    assert len({e.op for e in expressions}) == len(policies)
    for policy, expr in zip(policies, expressions):
        nan_signed, nan_magnitude = policy.get("nan", (False, 0x7fff))
        overflow_signed, overflow_magnitude = policy.get("overflow", (True, 0x7c00))
        for value, expected in [
            (0xffc00001, (int(nan_signed) << 15) | nan_magnitude),
            (0xff800000, (int(overflow_signed) << 15) | overflow_magnitude),
            (0x3f800000, 0x3c00),
        ]:
            assert evaluate(expr, None, {"x": value}) == expected


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


def operators(arch, text, fp="exact"):
    """{name: [arguments]} of the defined operators in a lifted instruction's writes."""
    found = {}
    for _, v in lifter.lift(arch, assemble_instruction(arch, text), fp=fp).writes:
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
    ("FADD.SAT R0, R2, R3", "sat.f32", None),
    ("HADD2.SAT R0, R2, R3", "sat.f16", None),
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
    ("PRMT R0, R2, 0x3210, R3", "prmt.b32", [(R(2), const(0x3210, 32), R(3))]),
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


COMPARISONS = "LT EQ LE GT NE GE NUM NAN LTU EQU LEU GTU NEU GEU".split()


@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("arch, text, width, arguments, endpoints", [
    ("SM89", "FSETP.{cmp}.AND P0, P1, R2, R3, P2", 32, (R(2), R(3)), ("F", "T")),
    ("SM89", "FSET.BF.{cmp}.AND R0, R2, R3, P2", 32, (R(2), R(3)), ("F", "T")),
    ("SM89", "DSETP.{cmp}.AND P0, P1, R2, R4, P2", 64, (reg("R", 2, 2), reg("R", 4, 2)), ("MIN", "MAX")),
    ("SM120", "UFSETP.{cmp}.AND UP0, UP1, UR2, UR3, UP2", 32, (reg("UR", 2, 1), reg("UR", 3, 1)), ("F", "T")),
])
def test_comparison_names(fp, arch, text, width, arguments, endpoints):
    for label in [*COMPARISONS, *endpoints]:
        name = f"setp.{label.lower()}.f{width}"
        found = operators(arch, text.format(cmp=label), fp)
        assert set(found) == {name}, (label, found)
        assert all(len(args) == 2 and all(g is w for g, w in zip(args, arguments)) for args in found[name])


@pytest.mark.parametrize("width", [32, 64])
@pytest.mark.parametrize("label", [*COMPARISONS, "F", "T", "MIN", "MAX"])
def test_comparison_reference(width, label):
    from tests.refs import float
    x, y = param("x", width), param("y", width)
    expr = floating.float_test(label, x, y)
    assert expr.op == f"setp.{label.lower()}.f{width}" and expr.w == 1
    assert expr.args == (x, y)
    plain = bitvec.lowered(expr)
    assert not defined(plain)
    assert plain is bitvec.lowered(floating.float_test.body(label, x, y))
    values = SPECIAL32 if width == 32 else [
        0, 0x8000000000000000, 1, 0x8000000000000001, 0x000fffffffffffff,
        0x0010000000000000, 0x3ff0000000000000, 0xbff0000000000000, 0x7fefffffffffffff,
        0x7ff0000000000000, 0xfff0000000000000, 0x7ff8000000000000, 0xfff8000000000001,
        0x7ff0000000000001,
    ]
    for a in values:
        for b in values:
            want = float.dsetp_compare(a, b, label, width)
            env = {"x": a, "y": b}
            assert evaluate(expr, None, env) == evaluate(plain, None, env) == want, (a, b)


@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_comparison_modifiers_and_show(fp):
    found = operators("SM89", "FSETP.GTU.FTZ.AND P0, PT, -R2, |R3|, PT", fp)
    (args,) = found["setp.gtu.f32"]
    if fp == "exact":
        assert [a.op for a in args] == ["ftz", "ftz"]
        args = tuple(a.args[0] for a in args)
    assert [a.op for a in args] == ["neg.f32", "abs.f32"]
    assert set(found) == {"setp.gtu.f32", "neg.f32", "abs.f32"} | ({"ftz"} if fp == "exact" else set())
    sem = lifter.lift("SM89", assemble_instruction("SM89", "FSETP.GTU.AND P0, PT, R2, R3, PT"), fp=fp)
    assert bitvec.show([(str(l), v) for l, v in sem.writes]) == "P0 := setp.gtu.f32(R2, R3)"
    assert sem.writes[0][1].w == 1
    expanded = bitvec.lowered(sem.writes[0][1])
    assert expanded is (bitvec.fnan(R(2)) | bitvec.fnan(R(3))) | bitvec.flt(R(3), R(2))
    assert floating.float_test("GTU", const(0x7fc00000, 32), const(0, 32)).op == "setp.gtu.f32"
    assert bitvec.lowered(floating.float_test("GTU", const(0x7fc00000, 32), const(0, 32))) is const(1, 1)


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
    ("SM89", "FFMA R0, -R2, |R3|, -|R4|", {"neg.f32", "abs.f32"}),
    ("SM89", "DFMA R0, -R2, R4, R6", {"neg.f64", "fma.rn.f64"}),
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


@pytest.mark.parametrize("mode", integer.PRMT_MODES)
def test_prmt_selector_contract(mode):
    """All source forms have one width signature, and an immediate-first expansion accepts params."""
    from tests.refs.integer import prmt
    label = f"prmt.b32{'' if mode == 'IDX' else '.' + mode.lower()}"
    suffix = "" if mode == "IDX" else "." + mode
    immediate = operators("SM89", f"PRMT{suffix} R0, R2, 0x3210, R3")[label][0]
    register = operators("SM89", f"PRMT{suffix} R0, R2, R4, R3")[label][0]
    assert tuple(a.w for a in immediate) == tuple(a.w for a in register) == (32, 32, 32)
    b = param("b", 32)
    expanded = bitvec.DEFINED[label].expand(X, Y, b)
    assert not bitvec.leaves(expanded, "sel")
    rng = random.Random(42)
    selectors = [0, 1, 3, 0x3210, 0x7654, 0x8888, 0xffff, 0xffff0000, 0xffffffff]
    selectors += [n << (4 * j) for j in range(4) for n in range(16)]
    selectors += [rng.getrandbits(32) for _ in range(32)]
    for selector in selectors:
        for a, c in [(0x80ff017f, 0x7f0081fe), (rng.getrandbits(32), rng.getrandbits(32))]:
            want = prmt(a, selector, c, mode)
            assert evaluate(expanded, None, {"x": a, "y": selector, "b": c}) == want
            constant = integer.permute(X, const(selector, 32), b, mode)
            assert evaluate(constant, None, {"x": a, "b": c}) == want
            dynamic = bitvec.substitute(constant, {const(selector, 32): Y}.get)
            assert evaluate(dynamic, None, {"x": a, "y": selector, "b": c}) == want


@pytest.mark.parametrize("mode", integer.PRMT_MODES)
def test_prmt_decode_time_selector(mode):
    """The widened selector preserves independent choice tables and their byte selections."""
    from sass.sleigh.lower import sel
    from tests.refs.integer import prmt
    bits = integer.prmt_bits(mode)
    choices = [sel((f"s{j}",), [((v,), const(v, bits)) for v in range(1 << bits)], bits)
               for j in range(4 if mode == "IDX" else 1)]
    selector = bitvec.zext(bitvec.concat(*reversed(choices)), 32)
    b = param("b", 32)
    expr = integer.permute(X, selector, b, mode)
    expanded = bitvec.lowered(expr)
    # No run-time shifts survive the static selection path.
    assert expanded.op == "concat"
    assert {n.args[0] for n in bitvec.leaves(expanded, "sel")} == {n.args[0] for n in choices}
    for v in range(1 << bits):
        # The expanded choices contain bytes; bind each key to its corresponding arm.
        concrete = bitvec.substitute(expanded, lambda n: dict(n.args[1])[(v,)] if n.op == "sel" else None)
        s = sum(v << (4 * j) for j in range(4)) if mode == "IDX" else v
        assert evaluate(concrete, None, {"x": 0x80ff017f, "b": 0x7f0081fe}) == prmt(0x80ff017f, s, 0x7f0081fe, mode)
    # The operator registered with decode-time choices also expands over a parameter.
    dynamic = bitvec.op(expr.op, X, Y, b, w=32)
    assert evaluate(dynamic, None, {"x": 0x80ff017f, "y": 0xffffffff, "b": 0x7f0081fe}) == prmt(0x80ff017f, 0xffffffff, 0x7f0081fe, mode)


@definition("test.width.{tag}")
def width_form(values, tag):
    """Concatenate flattened operands for the width-contract tests."""
    return bitvec.concat(*values)


@definition("test.scalarwidth.{tag}")
def scalar_width(x, tag):
    """A scalar operand for the width-contract tests."""
    return x


@pytest.mark.parametrize("first", [16, 32])
@pytest.mark.parametrize("tuple_argument", [False, True])
def test_a_name_determines_operand_widths(first, tuple_argument):
    tag = f"{first}.{int(tuple_argument)}"
    operand = param("width", first)
    call = width_form if tuple_argument else scalar_width
    values = (operand, P) if tuple_argument else operand
    expr = call(values, tag)
    constants = (const(0, first), const(0, 1)) if tuple_argument else const(0, first)
    assert call(constants, tag).w == expr.w
    other = param("other.width", 48 - first)
    with pytest.raises(TypeError, match="operand widths"):
        call((other, P) if tuple_argument else other, tag)
    assert bitvec.lowered(expr) is (bitvec.concat(*values) if tuple_argument else operand)


def test_operand_counts_have_independent_widths():
    assert width_form((param("half", 16),), "arity").w == 16
    pair = (X, Y)
    assert width_form(pair, "arity").w == 64
    assert bitvec.lowered(width_form(pair, "arity")) is bitvec.concat(*pair)


@definition(lambda a: (a["label"], ["x"]))
def labelled(x, label):
    """An identity with a caller-supplied label."""
    return x


@pytest.mark.parametrize("label", ["", "test_bad", "test-bad", "test/bad", "test bad", "test.é", "test.bad\n", None])
def test_invalid_definition_labels(label):
    with pytest.raises(TypeError, match="invalid defined operator label"):
        labelled(X, label)
    assert label not in bitvec.DEFINED


def test_valid_definition_label():
    assert bitvec.lowered(labelled(X, "Test.Label09")) is X


@pytest.mark.parametrize("width, label", [(16, "ftz.b16"), (32, "ftz"), (64, "ftz.b64")])
def test_flush_names_fix_widths(width, label):
    x = param("flush", width)
    expr = floating.flushed(x)
    assert expr.op == label and expr.args == (x,)
    assert bitvec.lowered(expr) is bitvec.lowered(floating.flushed.body(x))


def test_fixed_point_and_lane_choice_labels():
    half, byte, scale = param("half", 16), param("byte", 8), param("scale", 8)
    assert f2fp.to_s2_6(0, X, scale, "F32").op == "f2fp.cvt.rn.satfinite.s2.6.f32.scaled"
    assert f2fp.to_s2_6(P, X, scale, "F32").op == "f2fp.cvt.s2.6.f32.scaled"
    # Register the lifted form first, as definition discovery does in the full suite.
    assert operators("SM100", "F2FP.BF16.S2_6.UNPACK_B R0, R2, R4.H0").keys() == {"f2fp.cvt.rn.bf16.s2.6.scaled"}
    code = const(f2fp.UPCONVERTS["8b.scaled"].index("S2_6"), 8)
    assert f2fp.upconverted_scaled(code, 0, 0, byte, scale, "8b.scaled").op == "f2fp.cvt.rn.bf16.s2.6.scaled"
    assert floating.lane_chose_a(const(0, 2), P, half, half).op == "hmnmx.is.a.f16"


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


@pytest.mark.parametrize("n,fmt", [(32, "F32"), (64, "F64")])
@pytest.mark.parametrize("reverse", [False, True])
def test_float_to_int_full_width_definition_is_order_independent(n, fmt, reverse, monkeypatch):
    label = f"cvt.rni.ntz.s{n}.{fmt.lower()}"
    monkeypatch.setattr(family, "DEFINITIONS", family.DEFINITIONS.copy())
    monkeypatch.setattr(bitvec, "DEFINED", bitvec.DEFINED.copy())
    family.DEFINITIONS.pop(label, None)
    bitvec.DEFINED.pop(label, None)
    x = param("float_to_int_source", n)
    args = (x, fmt, n, const(1, 1), fpformat.rounding("RN"), const(0, n))
    widths = [None, const(n, 7)]
    if reverse: widths.reverse()
    first, second = (convert.float_to_int(*args, limit_bits=width) for width in widths)
    assert first.op == second.op
    assert first.args == second.args
    for value in (0, 0x7f800000 if n == 32 else 0x7ff0000000000000):
        assert evaluate(first, None, {"float_to_int_source": value}) == \
               evaluate(second, None, {"float_to_int_source": value})


def test_float_to_int_narrow_limit_remains_distinct():
    args = (X, "F32", 32, const(1, 1), fpformat.rounding("RN"), const(0, 32))
    full = convert.float_to_int(*args)
    narrow = convert.float_to_int(*args, limit_bits=const(8, 7))
    assert full.op != narrow.op
    assert evaluate(full, None, {"x": 0x43000000}) == 128
    assert evaluate(narrow, None, {"x": 0x43000000}) == 127


def test_float_to_int_dynamic_definition_keeps_optional_width_operand(monkeypatch):
    monkeypatch.setattr(family, "DEFINITIONS", family.DEFINITIONS.copy())
    monkeypatch.setattr(bitvec, "DEFINED", bitvec.DEFINED.copy())
    family.DEFINITIONS.pop("cvt.i32.f32", None)
    bitvec.DEFINED.pop("cvt.i32.f32", None)
    signed = param("float_to_int_signed", 1)
    r = fpformat.Rounding(*(const(int(flag), 1) for flag in fpformat.rounding("RN")))
    nan = const(0, 32)
    args = (X, "F32", 32, signed, r, nan)
    default = convert.float_to_int(*args)
    explicit = convert.float_to_int(*args, limit_bits=const(32, 7))
    assert default.op == explicit.op == "cvt.i32.f32"
    assert default.args == (signed, *r, nan, X)
    assert explicit.args == (*default.args, const(32, 7))
    for signed_value in (0, 1):
        inputs = {"x": 0x43000000, "float_to_int_signed": signed_value}
        assert evaluate(default, None, inputs) == evaluate(explicit, None, inputs)


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
    general = f2fp.pack16_lane(F16_DEST._replace(nan_signed=1), fpformat.rounding("RN"), 0, 0, X)
    assert general.op == "f2fp.cvt"
    assert tuple(a.w for a in general.args) == (32, 32, 32, 1, 32, 1, 32, 1, 1, 1, 1, 1, 1, 32)
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
    assert "ftz(" not in expanded[0]   # once-used expansions inline, pointed at where they are
    assert {"╰ ftz(R2)", "╰ ftz(R3)", "╰ ftz(t0)"} <= {l.strip(" │") for l in expanded}
    fmnmx = bitvec.show([(str(l), v) for l, v in lifter.lift("SM89", assemble_instruction("SM89", "FMNMX.FTZ R0, R2, R3, PT")).writes], expand=True).splitlines()
    assert fmnmx[0].startswith("t0 = R2 & ") and fmnmx[2].strip() == "╰ ftz(R2)"   # a shared tN's line points at its operator


@pytest.mark.parametrize("expand", [False, True])
def test_show_distinguishes_runtime_primitives(expand):
    ballot = bitvec.primitive("ballot", 32, reg("P", 0, 1, element=1))
    assert bitvec.show([(None, ballot)], expand=expand) == "primitive_ballot(P0)"
    assert ballot.op == "primitive" and ballot.args[0] == "ballot"
    assert bitvec.primitive_label("sass_prim_bar") == "primitive_bar"
    assert bitvec.show([(None, param("ballot", 32))]) == "ballot"
    sem = lifter.lift("SM89", assemble_instruction("SM89", "SHFL.BFLY PT, R5, R2, 0x1, 0x1f"))
    text = bitvec.show(sem.writes, expand=expand)
    assert all(name in text for name in ("primitive_lane()", "primitive_active()", "primitive_read_lane(R2,"))


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
