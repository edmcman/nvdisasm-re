"""The C++ floating point (sass/emulate/include/sass/softfloat.hpp) computes the IR's definition
(`bitvec.OPS`, Python softfloat) bit for bit: the special-value cross product, random encodings,
near-cancelling pairs and exact ties; --exhaustive takes five times the random inputs and every FP16
source of a conversion."""
import itertools, random
from fractions import Fraction
import pytest
from sass.emulate import native
from sass.semantics import bitvec
from sass.semantics.fp import softfloat

pytestmark = pytest.mark.native  # opt-in: -m native

WIDTHS = (16, 32, 64)
MODES = native.ROUNDING
RANDOM = 300


@pytest.fixture
def scale(request):
    """How many times RANDOM inputs to draw."""
    return 5 if request.config.getoption('--exhaustive') else 1


def special(w):
    """±0, ±min/max subnormal, ±min normal, ±(1-ulp), ±1, ±(1+ulp), ±2, ±max, ±inf, ±qNaN, ±sNaN."""
    e, m = softfloat.FORMATS[w]; one = ((1 << (e - 1)) - 1) << m; inf = ((1 << e) - 1) << m
    magnitudes = [0, 1, (1 << m) - 1, 1 << m, one - 1, one, one + 1, one + (1 << m), inf - 1, inf, inf | 1 << (m - 1), inf | 1]
    return [s << (w - 1) | x for s in (0, 1) for x in magnitudes]


def near(rng, w):
    """A pair whose sum nearly cancels: equal exponents, opposite signs, or one tiny."""
    a = rng.getrandbits(w); flip = 1 << (w - 1)
    return [(a, a ^ flip ^ rng.getrandbits(rng.randrange(1, softfloat.FORMATS[w][1] + 1))), (a, (a ^ flip) - 1),
            (a, rng.getrandbits(w) & ~(0x7ff << (w - 12)) if w == 64 else rng.getrandbits(w) >> 4)][rng.randrange(3)]


def tie(rng, w):
    """(a, b): b is exactly half an ulp of a, so a + b rounds a tie (b may be subnormal)."""
    e, m = softfloat.FORMATS[w]
    a = rng.randrange(1 << m, ((1 << e) - 2) << m) | rng.getrandbits(1) << (w - 1)
    ulp = Fraction(2) ** (((a >> m) & ((1 << e) - 1)) - ((1 << (e - 1)) - 1) - m - 1)
    return a, softfloat.pack(ulp * rng.choice((1, -1)), w)


def binary(w, seed, n):
    rng = random.Random(seed)
    return [*itertools.product(special(w), repeat=2), *((rng.getrandbits(w), rng.getrandbits(w)) for _ in range(n)),
            *(near(rng, w) for _ in range(n)), *(tie(rng, w) for _ in range(n))]


def ternary(w, seed, n):
    rng = random.Random(seed); s = special(w)[::2] + special(w)[1::3]
    def cancelling():
        a, b = rng.getrandbits(w), rng.getrandbits(w)
        p = softfloat.arithmetic("mul", [a, b], w, rng.choice(MODES))
        return a, b, p ^ 1 << (w - 1) ^ rng.getrandbits(3)
    return [*itertools.product(s, repeat=3), *((rng.getrandbits(w), rng.getrandbits(w), rng.getrandbits(w)) for _ in range(n)),
            *(cancelling() for _ in range(n))]


def check(name, inputs, ours, theirs):
    bad = [(x, hex(ours(*x)), hex(theirs(*x))) for x in inputs if ours(*x) != theirs(*x)]
    assert not bad, f"{name}: {len(bad)}/{len(inputs)} differ, first {[tuple(map(hex, b[0])) + b[1:] for b in bad[:5]]}"


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
def test_add(w, mode, scale):
    check(f"fadd{w}.{mode}", binary(w, w, RANDOM * scale), lambda a, b: native.fp("add", w, a, b, mode=mode),
          lambda a, b: bitvec.OPS["fadd"]([a, b, mode], [w, w, None], w))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("power", (0, -2, 1, 3))
def test_mul(w, mode, power, scale):
    check(f"fmul{w}.{mode}.{power}", binary(w, w + 1, RANDOM * scale), lambda a, b: native.fp("mul", w, a, b, mode=mode, scale=power),
          lambda a, b: bitvec.OPS["fmul"]([a, b, mode, power], [w, w, None, None], w))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
def test_fma(w, mode, scale):
    check(f"ffma{w}.{mode}", ternary(w, w + 2, RANDOM * scale), lambda a, b, c: native.fp("fma", w, a, b, c, mode=mode),
          lambda a, b, c: bitvec.OPS["ffma"]([a, b, c, mode], [w, w, w, None], w))


@pytest.mark.parametrize("src, dst", [(s, d) for s in WIDTHS for d in WIDTHS if s != d])
@pytest.mark.parametrize("mode", MODES)
def test_convert(src, dst, mode, scale):
    rng = random.Random(src * dst)
    inputs = [(x,) for x in (range(1 << 16) if src == 16 and scale > 1 else [*special(src), *(rng.getrandbits(src) for _ in range(4 * RANDOM * scale))])]
    check(f"fconv{src}->{dst}.{mode}", inputs, lambda x: native.fp("convert", src, x, mode=mode, dst=dst),
          lambda x: bitvec.OPS["fconv"]([x, mode], [src, None], dst))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", ("lt", "le", "eq"))
def test_compare(w, op, scale):
    check(f"f{op}{w}", binary(w, w + 3, RANDOM * scale), lambda a, b: native.fp(op, w, a, b),
          lambda a, b: bitvec.OPS["f" + op]([a, b], [w, w], 1))


@pytest.mark.parametrize("w", WIDTHS)
def test_nan(w):
    check(f"fnan{w}", [(x,) for x in special(w)], lambda x: native.fp("nan", w, x), lambda x: bitvec.OPS["fnan"]([x], [w], 1))
