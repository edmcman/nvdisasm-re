"""Both explicit C++ floating-point backends (softfloat and fp_host) compute the IR's definition
(`bitvec.OPS`, Python softfloat) bit for bit: the special-value cross product, random encodings,
near-cancelling pairs and exact ties; --exhaustive takes five times the random inputs and every FP16
source of a conversion."""
import ctypes, itertools, random
from fractions import Fraction
import pytest
from sass.emulate import build, native
from sass.semantics import bitvec
from sass.semantics.fp import softfloat

pytestmark = pytest.mark.native  # opt-in: -m native

WIDTHS = (16, 32, 64)
MODES = native.ROUNDING
RANDOM = 300


@pytest.fixture(params=(native.fp, native.fp_host), ids=("softfloat", "host"))
def backend(request):
    return request.param


@pytest.fixture
def scale(request):
    """How many times RANDOM inputs to draw."""
    return 5 if request.config.getoption('--exhaustive') else 1


def special(w):
    """±0, ±min/max subnormal, ±min normal, ±(1-ulp), ±1, ±(1+ulp), ±2, ±max, ±inf, ±qNaN, ±sNaN."""
    e, m = softfloat.FORMATS[w]; one = ((1 << (e - 1)) - 1) << m; inf = ((1 << e) - 1) << m
    magnitudes = [0, 1, (1 << m) - 1, 1 << m, one - 1, one, one + 1, one + (1 << m), inf - 1, inf, inf | 1 << (m - 1), inf | 1]
    return [s << (w - 1) | x for s in (0, 1) for x in magnitudes]


def scaled_products(w, count):
    """Move exact products around overflow, normal/subnormal, and zero-rounding bounds."""
    e, m = softfloat.FORMATS[w]
    bias = (1 << (e - 1)) - 1
    for a, b in binary(w, w + 91, count):
        powers = {-2048, -1024, -513, -512, -2, 0, 3, 512, 513, 1024, 2048}
        x, y = softfloat.unpack(a, w), softfloat.unpack(b, w)
        if x[0] == y[0] == "finite":
            product = abs(x[2] * y[2])
            top = product.numerator.bit_length() - product.denominator.bit_length()
            for bound in (1 - bias - m - 1, 1 - bias, bias):
                powers.update(bound - top + delta for delta in (-2, -1, 0, 1, 2))
        for power in sorted(powers):
            yield a, b, power


@pytest.fixture(scope="module")
def scaled_multiply():
    lib = build.library("""
#include "sass/fp_host.hpp"
extern "C" uint64_t scaled_product(int w, uint64_t a, uint64_t b, int mode, int scale) {
  const auto rounding = sass::fp_common::Rnd(mode);
  return w == 32 ? sass::fp_host::mul<32>(a, b, rounding, scale)
                 : sass::fp_host::mul<64>(a, b, rounding, scale);
}
""", "fp-scaled-product")
    fn = lib.scaled_product
    fn.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int, ctypes.c_int]
    fn.restype = ctypes.c_uint64
    return fn


@pytest.mark.parametrize("w", (32, 64))
@pytest.mark.parametrize("mode", MODES)
def test_scaled_product_boundaries(w, mode, scale, scaled_multiply):
    for a, b, power in scaled_products(w, 60 * scale):
        expected = softfloat.arithmetic("mul", [a, b], w, mode, scale=power)
        assert scaled_multiply(w, a, b, MODES.index(mode), power) == expected, (w, a, b, mode, power)
    # Very large int scales use bounded exponent arithmetic; ±4096 already determines
    # overflow/underflow for every finite FP32/FP64 product in the independent oracle.
    for a, b in binary(w, w + 92, 20):
        for power in (-2147483648, 2147483647):
            expected = softfloat.arithmetic("mul", [a, b], w, mode, scale=-4096 if power < 0 else 4096)
            assert scaled_multiply(w, a, b, MODES.index(mode), power) == expected
            assert native.fp_host("mul", w, a, b, mode=mode, scale=power) == expected


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


def half_boundaries(src, exhaustive=False):
    """Source encodings immediately below/at/above half midpoints, independently built."""
    halves = range(0x7bff) if exhaustive else sorted(set(range(0, 0x7bff, 127)) |
                                                   {0, 1, 2, 1023, 1024, 1025, 0x3bff, 0x3c00, 0x3c01, 0x7bfe})
    for h in halves:
        midpoint = (softfloat.unpack(h, 16)[2] + softfloat.unpack(h + 1, 16)[2]) / 2
        for sign in (1, -1):
            bits = softfloat.pack(sign * midpoint, src)
            yield from (bits - 1, bits, bits + 1)
    for sign in (1, -1):
        bits = softfloat.pack(Fraction(sign * 65520), src)  # last finite/infinite midpoint
        yield from (bits - 1, bits, bits + 1)


def check(name, inputs, ours, theirs):
    bad = [(x, hex(ours(*x)), hex(theirs(*x))) for x in inputs if ours(*x) != theirs(*x)]
    assert not bad, f"{name}: {len(bad)}/{len(inputs)} differ, first {[tuple(map(hex, b[0])) + b[1:] for b in bad[:5]]}"


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
def test_add(w, mode, scale, backend):
    check(f"fadd{w}.{mode}", binary(w, w, RANDOM * scale), lambda a, b: backend("add", w, a, b, mode=mode),
          lambda a, b: bitvec.OPS["fadd"]([a, b, mode], [w, w, None], w))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("power", (0, -2, 1, 3, -512, 512, -513, 513, -1024, 1024, -2048, 2048))
def test_mul(w, mode, power, scale, backend):
    check(f"fmul{w}.{mode}.{power}", binary(w, w + 1, RANDOM * scale), lambda a, b: backend("mul", w, a, b, mode=mode, scale=power),
          lambda a, b: bitvec.OPS["fmul"]([a, b, mode, power], [w, w, None, None], w))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("mode", MODES)
def test_fma(w, mode, scale, backend):
    check(f"ffma{w}.{mode}", ternary(w, w + 2, RANDOM * scale), lambda a, b, c: backend("fma", w, a, b, c, mode=mode),
          lambda a, b, c: bitvec.OPS["ffma"]([a, b, c, mode], [w, w, w, None], w))


@pytest.mark.parametrize("src, dst", [(s, d) for s in WIDTHS for d in WIDTHS])
@pytest.mark.parametrize("mode", MODES)
def test_convert(src, dst, mode, scale, backend):
    rng = random.Random(src * dst)
    inputs = [(x,) for x in (range(1 << 16) if src == 16 and scale > 1 else [*special(src), *(rng.getrandbits(src) for _ in range(4 * RANDOM * scale))])]
    check(f"fconv{src}->{dst}.{mode}", inputs, lambda x: backend("convert", src, x, mode=mode, dst=dst),
          lambda x: bitvec.OPS["fconv"]([x, mode], [src, None], dst))


@pytest.mark.parametrize("src", (32, 64))
@pytest.mark.parametrize("mode", MODES)
def test_half_conversion_boundaries(src, mode, scale, backend):
    for bits in half_boundaries(src, scale > 1):
        expected = bitvec.OPS["fconv"]([bits, mode], [src, None], 16)
        assert backend("convert", src, bits, mode=mode, dst=16) == expected, (src, mode, hex(bits))


@pytest.mark.parametrize("w", WIDTHS)
@pytest.mark.parametrize("op", ("lt", "le", "eq"))
def test_compare(w, op, scale, backend):
    check(f"f{op}{w}", binary(w, w + 3, RANDOM * scale), lambda a, b: backend(op, w, a, b),
          lambda a, b: bitvec.OPS["f" + op]([a, b], [w, w], 1))


@pytest.mark.parametrize("w", WIDTHS)
def test_nan(w, backend):
    check(f"fnan{w}", [(x,) for x in special(w)], lambda x: backend("nan", w, x), lambda x: bitvec.OPS["fnan"]([x], [w], 1))
