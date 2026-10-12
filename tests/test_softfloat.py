"""Layer 0: the softfloat reference model against host libm (fp16/fp32/fp64) and hand values."""
import ctypes, math, random
from fractions import Fraction
import pytest
from sass import softfloat
from tests.refs.bits import bits32, bits64, f16, f16_bits, f32, f64

LIBM = ctypes.CDLL("libm.so.6")


def host(value, pack, overflow):
    try: return pack(value)
    except OverflowError: return overflow(value)


def test_directed_ties():
    one, half = Fraction(1), Fraction(1, 1 << 24)
    assert softfloat.pack(one + half) == 0x3f800000
    assert softfloat.pack(one + half, mode="RP") == 0x3f800001
    assert softfloat.pack(-one - half, mode="RM") == 0xbf800001
    assert softfloat.pack(-one - half, mode="RZ") == 0xbf800000


def test_specials():
    assert softfloat.arithmetic("add", [0x80000000, 0x80000000]) == 0x80000000
    assert softfloat.arithmetic("add", [0x7f800000, 0xff800000]) == 0x7fc00000
    assert softfloat.arithmetic("mul", [0x7f800000, 0]) == 0x7fc00000
    assert softfloat.arithmetic("add", [1, 0], ftz=True) == 0
    assert softfloat.arithmetic("add", [0x7fc00000, 0], sat=True) == 0
    assert softfloat.pack(Fraction(1, 1 << 149)) == 1


def test_conversions():
    for rnd, expected in [("RN", 2), ("RM", 1), ("RP", 2), ("RZ", 1)]:
        assert softfloat.float_to_int(bits32(1.5), 32, 32, True, rnd) == expected
    with pytest.raises(NotImplementedError): softfloat.float_to_int(0x7fc00000, 32, 32, True)
    assert softfloat.pack(Fraction(65504), 16) == 0x7bff
    assert softfloat.unpack(0x3ff0000000000000, 64)[2] == 1


def test_host_fp32():
    rng = random.Random(42)
    LIBM.fmaf.argtypes = [ctypes.c_float] * 3; LIBM.fmaf.restype = ctypes.c_float
    for _ in range(1000):
        xs = [rng.getrandbits(32) for _ in range(3)]; a, b, c = map(f32, xs)
        if not all(map(math.isfinite, (a, b, c))): continue
        assert softfloat.arithmetic("fma", xs) == bits32(LIBM.fmaf(a, b, c)), [hex(x) for x in xs]
        for op, reference in [("add", a + b), ("mul", a * b)]:
            assert softfloat.arithmetic(op, xs[:2]) == host(reference, bits32, lambda v: 0xff800000 if v < 0 else 0x7f800000)


def test_host_fp64():
    rng = random.Random(64)
    LIBM.fma.argtypes = [ctypes.c_double] * 3; LIBM.fma.restype = ctypes.c_double
    for _ in range(1000):
        xs = [rng.getrandbits(64) for _ in range(3)]; a, b, c = map(f64, xs)
        if not all(map(math.isfinite, (a, b, c))): continue
        for op, reference in [("add", a + b), ("mul", a * b), ("fma", LIBM.fma(a, b, c))]:
            assert softfloat.arithmetic(op, xs if op == "fma" else xs[:2], 64) == bits64(reference)


def test_host_fp16():
    # Half inputs and products are exact in the host double; this checks narrowing, overflow
    # and signed zero.
    rng = random.Random(16)
    for _ in range(1000):
        xs = [rng.getrandbits(16) for _ in range(3)]; a, b, c = map(f16, xs)
        if not all(map(math.isfinite, (a, b, c))): continue
        for op, reference in [("add", a + b), ("fma", a * b + c)]:
            assert softfloat.arithmetic(op, xs if op == "fma" else xs[:2], 16) == f16_bits(reference)
