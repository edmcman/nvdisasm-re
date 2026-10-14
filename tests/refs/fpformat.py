"""Exact rational minifloat formats and the conversion rules SASS instructions apply on top.

Independent of the generated semantics: values are Fractions, rounding picks among enumerated
encodings. Only sass.semantics.fp.softfloat (the reference IEEE model) is used, to unpack fp16/32/64 sources.
"""
from bisect import bisect_left
from fractions import Fraction
from functools import cache
from typing import NamedTuple
from sass.semantics.fp import softfloat


class Format(NamedTuple):
    ebits: int
    mbits: int
    bias: int
    special: str | None  # "ieee": all-ones exponent is Inf/NaN; "max": all-ones magnitude is NaN


FORMATS = {
    "F16": Format(5, 10, 15, "ieee"), "BF16": Format(8, 7, 127, "ieee"), "E6M9": Format(6, 9, 31, "ieee"),
    "E5M2": Format(5, 2, 15, "ieee"), "E4M3": Format(4, 3, 7, "max"), "E3M4": Format(3, 4, 3, "max"),
    "E3M2": Format(3, 2, 3, None), "E2M3": Format(2, 3, 1, None),
    "E2M1": Format(2, 1, 1, None), "E0M3": Format(0, 3, -2, None),
    "UE5M3": Format(5, 3, 15, "max")}   # unsigned: codes are magnitudes (no sign bit)


def width(fmt): f = FORMATS[fmt]; return 1 + f.ebits + f.mbits


def decode(fmt, code):
    """(kind, sign, magnitude) with kind in nan/inf/finite."""
    f = FORMATS[fmt]; top = 1 << (f.ebits + f.mbits)
    sign, mag = bool(code & top), code & (top - 1)
    exp, mant = mag >> f.mbits, mag & ((1 << f.mbits) - 1)
    if f.special == "ieee" and exp == (1 << f.ebits) - 1: return ("nan" if mant else "inf"), sign, None
    if f.special == "max" and mag == top - 1: return "nan", sign, None
    if exp == 0: return "finite", sign, mant * Fraction(2) ** (1 - f.bias - f.mbits)
    return "finite", sign, ((1 << f.mbits) + mant) * Fraction(2) ** (exp - f.bias - f.mbits)


@cache
def finite(fmt):
    """[(magnitude code, value)] for every finite non-negative encoding, ascending."""
    f = FORMATS[fmt]; out = []
    for code in range(1 << (f.ebits + f.mbits)):
        kind, _, value = decode(fmt, code)
        if kind == "finite": out.append((code, value))
    return out


def round_magnitude(fmt, mag, rnd="RN", negative=False):
    """(code, overflow): the magnitude code `mag` rounds to, and whether it exceeds max finite.
    RN treats max finite + one top-binade ulp as the next (odd/even) encoding for ties."""
    codes, values = finite(fmt), _values(fmt)
    (top, high), (_, below) = codes[-1], codes[-2]
    up = rnd == "RP" and not negative or rnd == "RM" and negative
    if mag > high:
        middle = high + (high - below) / 2
        return top, rnd == "RN" and (mag > middle or mag == middle and top & 1) or up
    i = bisect_left(values, mag)  # values[i-1] < mag <= values[i]
    if values[i] == mag: return codes[i][0], False
    (lo, lv), (hi, hv) = codes[i - 1], codes[i]
    if rnd == "RN": return (lo if mag - lv < hv - mag or mag - lv == hv - mag and not lo & 1 else hi), False
    return (hi if up else lo), False


@cache
def _values(fmt): return [v for _, v in finite(fmt)]


def unpack(bits, src):
    """(kind, sign, magnitude) of an F32/F16/BF16 bit pattern; kind in nan/inf/finite."""
    kind, sign, value = softfloat.unpack(bits << 16 if src == "BF16" else bits, {"F32": 32, "F16": 16, "BF16": 32}[src])
    return ("finite" if kind == "zero" else kind), bool(sign), (abs(value) if value is not None else None)


def encode(fmt, sign, code): return (sign << (width(fmt) - 1)) | code


def narrow(bits, src, fmt, rnd="RN", satfinite=True, relu=False, nan=None, overflow=None, scale=127):
    """Round an F32/F16/BF16 value, times 2^(127 - scale) (a UE8M0 scale; 255: NaN), to `fmt`.
    nan: the NaN result (default canonical +all-ones); overflow: the out-of-range result without
    satfinite (default signed infinity)."""
    kind, sign, mag = unpack(bits, src)
    if scale == 255: kind = "nan"
    elif kind == "finite": mag *= Fraction(2) ** (127 - scale)
    f = FORMATS[fmt]; top = (1 << (f.ebits + f.mbits)) - 1
    inf = (top >> f.mbits) << f.mbits
    if kind == "nan": return top if nan is None else nan(sign)
    if kind == "inf": code, over = finite(fmt)[-1][0], True
    else: code, over = round_magnitude(fmt, mag, rnd, sign)
    out = (encode(fmt, sign, inf) if overflow is None else overflow(sign)) if over and not satfinite else encode(fmt, sign, code)
    # RELU zeroes negative results, but not a NaN produced by overflow.
    return 0 if relu and out >> (width(fmt) - 1) and decode(fmt, out)[0] != "nan" else out


def widen(fmt, code, dst="F16", nan=0x7fff):
    """Exact widening to F16/BF16; every NaN becomes `nan`."""
    kind, sign, mag = decode(fmt, code)
    if kind == "nan": return nan
    d = FORMATS[dst]
    if kind == "inf": return encode(dst, sign, ((1 << d.ebits) - 1) << d.mbits)
    return encode(dst, sign, round_magnitude(dst, mag)[0])


def f32_to_f16(bits, rnd="RN"):
    """F2FP F16 pack: canonical signed NaN 0x7e00, Inf kept, RZ overflow saturates at max finite."""
    kind, sign, _ = unpack(bits, "F32")
    if kind == "nan": return (sign << 15) | 0x7e00
    if kind == "inf": return (sign << 15) | 0x7c00
    return narrow(bits, "F32", "F16", rnd, satfinite=rnd == "RZ")


def f32_to_bf16(bits, rnd="RN"):
    """F2FP BF16 pack: NaN becomes +0x7fff, otherwise RN-even or truncation of the low half."""
    if softfloat.unpack(bits, 32)[0] == "nan": return 0x7fff
    hi, lo = bits >> 16, bits & 0xffff
    if rnd == "RN" and (lo > 0x8000 or (lo == 0x8000 and hi & 1)): hi += 1
    return hi & 0xffff


def f32_to_e6m9(bits, rnd="RN"):
    kind, sign, _ = unpack(bits, "F32")
    if kind == "nan": return 0x7fff
    if kind == "inf": return (sign << 15) | 0x7e00
    return narrow(bits, "F32", "E6M9", rnd, satfinite=rnd == "RZ")


PACK16 = {"F16": f32_to_f16, "BF16": f32_to_bf16, "E6M9": f32_to_e6m9}


def is_nan16(out, fmt):
    f = FORMATS[fmt]; inf = ((1 << f.ebits) - 1) << f.mbits
    return out & inf == inf and out & ((1 << f.mbits) - 1) != 0


def pack16(bits, fmt, rnd="RN", satfinite=False, relu=False):
    """F2FP 16-bit pack with SATFINITE (Inf -> max finite) and RELU (negative non-NaN -> 0;
    a NaN keeps its sign)."""
    out = PACK16[fmt](bits, rnd)
    f = FORMATS[fmt]; inf = ((1 << f.ebits) - 1) << f.mbits
    if satfinite and out & 0x7fff == inf: out = (out & 0x8000) | (inf - 1)
    return 0 if relu and out & 0x8000 and not is_nan16(out, fmt) else out


def fp8(bits, fmt, rnd="RN", satfinite=True, relu=False, src="F32", scale=127):
    """F2FP E4M3/E5M2 pack: NaN keeps its sign as S.1111111; without SATFINITE E4M3 overflows
    to +NaN and E5M2 to signed Inf; RELU zeroes negative non-NaN values."""
    return narrow(bits, src, fmt, rnd, satfinite, relu, nan=lambda s: (s << 7) | 0x7f,
                  overflow=(lambda s: 0x7f) if fmt == "E4M3" else None, scale=scale)


def fp6(bits, fmt, rnd="RN", relu=False, src="F32", scale=127):
    """F2FP E2M3/E3M2 SATFINITE pack into a byte: NaN becomes +max (0x1f)."""
    code = narrow(bits, src, fmt, rnd, True, relu, nan=lambda s: 0x1f, scale=scale)
    return ((code & 0x20) << 2) | (code & 0x1f)


def ue5m3(bits, src="F32", rnd="RN", satfinite=False, scale=127):
    """CUDA's UE5M3 conversion (cuda_fp8.hpp): the sign dropped first, no infinity; NaN (and a 0xff
    scale) gives 0xff; Inf and an overflow rounding away from zero give 0xff, or 0xfe (the largest
    finite) under SATFINITE; RZ/RM never overflow."""
    kind, _, mag = unpack(bits, src)
    if kind == "nan" or scale == 255: return 0xff
    if kind == "inf": return 0xfe if satfinite else 0xff
    code, over = round_magnitude("UE5M3", mag * Fraction(2) ** (127 - scale), rnd)
    return (0xfe if satfinite else 0xff) if over else code


def pzo(byte):
    """.PZO: a -0 result (sign bit 7, zero magnitude) becomes +0."""
    return 0 if byte == 0x80 else byte


def fp4(bits, fmt, relu=False, src="F32", relu_nan=False):
    """F2FP E2M1/E0M3 SATFINITE RN pack: NaN becomes +max (7); relu_nan: RELU also zeroes -NaN."""
    if relu_nan and relu and unpack(bits, src)[1]: return 0
    return narrow(bits, src, fmt, "RN", True, relu, nan=lambda s: 7)


def tf32(bits, rn):
    """F2FP TF32: keep 10 fraction bits; Inf/NaN truncate."""
    sign, mag = bits & 0x80000000, bits & 0x7fffffff
    if mag & 0x7f800000 == 0x7f800000: return bits & 0xffffe000
    base, discarded = mag & 0xffffe000, mag & 0x1fff
    if rn and (discarded > 0x1000 or (discarded == 0x1000 and (base >> 13) & 1)): base += 0x2000
    return sign | base


def e8m0_to_bf16(byte):
    """UE8M0 scale to BF16: 0xff is NaN, 0 is 2^-127."""
    return 0x7fff if byte == 0xff else 0x0040 if byte == 0 else byte << 7


def source(fmt, code):
    """(kind, sign, magnitude) of a scaled-upconvert source byte, including S2_6 fixed point."""
    if fmt == "S2_6":
        signed = code - 256 if code & 0x80 else code
        return "finite", signed < 0, Fraction(abs(signed), 64)
    return decode(fmt, code)


def scaled_bf16(code, fmt, scale, relu=False, satfinite=True):
    """Scaled upconvert to BF16: value * 2^(scale-127) (scale 0 -> 2^-127, 255 -> NaN), RN."""
    kind, sign, mag = source(fmt, code)
    if kind == "nan" or scale == 0xff: return 0x7fff
    if kind == "inf": out = encode("BF16", sign, 0x7f80)
    else:
        code, over = round_magnitude("BF16", mag * Fraction(2) ** (-127 if scale == 0 else scale - 127))
        out = encode("BF16", sign, 0x7f80 if over else code)
    if satfinite and out & 0x7fff == 0x7f80: out = (out & 0x8000) | 0x7f7f
    if relu and out & 0x8000 and not is_nan16(out, "BF16"): return 0
    return out


def s2_6(bits, scale, relu=False):
    """F32/BF16 to S2_6 fixed point: x * 2^(127-scale), clamped to +-127/64, RN-even."""
    kind, sign, mag = unpack(bits, "F32")
    if kind == "nan" or scale == 255: return 0x7f
    if kind == "inf": integer = 127
    else:
        scaled = mag * Fraction(2) ** (127 - scale) * 64
        integer = 127 if scaled > 127 else _round_even(scaled)
    if sign: integer = -integer
    return 0 if relu and integer < 0 else integer & 0xff


def _round_even(value):
    q, r = divmod(value.numerator, value.denominator)
    return q + (2 * r > value.denominator or (2 * r == value.denominator and q & 1))


def same_fp8(got, want, fmt):
    """Equal high halves, and each low FP8 byte equal or both NaN (NaN signs are not compared)."""
    nan = lambda v: decode(fmt, v)[0] == "nan"
    return got >> 16 == want >> 16 and all(g == w or nan(g) and nan(w) for g, w in ((got >> s & 0xff, want >> s & 0xff) for s in (0, 8)))
