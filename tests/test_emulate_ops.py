"""The C++ lowering of the IR (sass.emulate.cxx) computes `sass.semantics.evaluate`'s values on random
expression trees over every operator, at widths 1 to 128, built unsimplified (raw `Expr`) so each
operator reaches the backend: their values, written to registers, match bit for bit."""
import random
import pytest
from sass.emulate import machine
from sass.semantics import bitvec, evaluate, lifter
from sass.semantics.bitvec import Expr

TREES, DEPTH, INPUTS = 600, 4, 16
FLOATS = (16, 32, 64)


def reg_of(rng, w):
    count = -(-w // 32); e = Expr("reg", ("R", rng.randrange(INPUTS - count + 1), count), 32 * count)
    return e if e.w == w else Expr("extract", (e, w - 1, 0), w)


def leaf(rng, w):
    return Expr("const", (rng.getrandbits(w) if rng.random() < 0.6 else rng.choice((0, 1, bitvec.mask(w), 1 << (w - 1))),), w) \
        if rng.random() < 0.3 else reg_of(rng, w)


def tree(rng, w, depth):
    if depth == 0 or rng.random() < 0.12: return leaf(rng, w)
    sub = lambda width: tree(rng, width, depth - 1)
    choices = ["arith", "unary", "shift", "ite", "extract", "concat"] + ["ext"] * (w > 1) + ["compare"] * (w == 1) + ["float"] * (w in FLOATS) + ["fpred"] * (w == 1)
    match rng.choice(choices):
        case "arith": return Expr(rng.choice(("add", "sub", "mul", "and", "or", "xor")), (sub(w), sub(w)), w)
        case "unary": return Expr(rng.choice(("not", "neg")), (sub(w),), w)
        case "shift":
            n = rng.choice((5, 8, 32, 64))
            count = Expr("const", (rng.randrange(w + 3),), n) if rng.random() < 0.6 else sub(n)
            return Expr(rng.choice(("shl", "shr", "sar")), (sub(w), count), w)
        case "ite": return Expr("ite", (sub(1), sub(w), sub(w)), w)
        case "ext": return Expr(rng.choice(("zext", "sext")), (sub(rng.randrange(1, w)),), w)
        case "extract":
            xw = rng.randrange(w, 129); lo = rng.randrange(xw - w + 1)
            return Expr("extract", (sub(xw), lo + w - 1, lo), w)
        case "concat":
            if w == 1: return sub(1)
            cut = rng.randrange(1, w)
            return Expr("concat", (sub(w - cut), sub(cut)), w)
        case "compare":
            k = rng.choice((1, 7, 32, 33, 64, 100, 128))
            return Expr(rng.choice(("eq", "ne", "ult", "ule", "slt", "sle")), (sub(k), sub(k)), 1)
        case "fpred":
            k = rng.choice(FLOATS)
            return Expr("fnan", (sub(k),), 1) if rng.random() < 0.25 else Expr(rng.choice(("flt", "fle", "feq")), (sub(k), sub(k)), 1)
        case "float":
            rnd = rng.choice(bitvec.ROUNDING)
            match rng.choice(("fadd", "fmul", "ffma", "fconv", "nanresult")):
                case "fadd": return Expr("fadd", (sub(w), sub(w), rnd), w)
                case "fmul": return Expr("fmul", (sub(w), sub(w), rnd, rng.choice((0, 0, -1, 2))), w)
                case "ffma": return Expr("ffma", (sub(w), sub(w), sub(w), rnd), w)
                case "fconv": return Expr("fconv", (sub(rng.choice([f for f in FLOATS if f != w])), rnd), w)
                case _: return Expr("nanresult", (Expr("fadd", (sub(w), sub(w), rnd), w), Expr("const", (rng.getrandbits(w),), w)), w)


def semantics(value):
    count = -(-value.w // 32)
    widened = value if value.w == 32 * count else Expr("zext", (value,), 32 * count)
    return lifter.Semantics("SM89", "tree", "tree", 0x1000, "native", writes=[(bitvec.reg("R", 16, count), widened)])


@pytest.mark.parametrize("seed", range(4))
def test_random_trees(seed):
    rng = random.Random(seed)
    trees = [tree(rng, rng.choice((1, 2, 7, 16, 31, 32, 33, 63, 64, 65, 100, 127, 128)), DEPTH) for _ in range(TREES)]
    states = [evaluate.State({("R", i): rng.getrandbits(32) for i in range(INPUTS)}) for _ in trees]
    library = machine.Fragments([semantics(t) for t in trees], semantics=True)
    for i, (t, state) in enumerate(zip(trees, states)):
        want = evaluate.execute(semantics(t), state)
        got = library.execute(i, state, {})
        count = -(-t.w // 32)
        assert got.read("R", 16, count) == want.read("R", 16, count), f"tree {i}: {bitvec.show([(None, t)])}"
