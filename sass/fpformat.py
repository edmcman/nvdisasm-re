"""Floating-point formats and conversions as `sass.sem` bitvector expressions.

One rounding routine serves every narrowing: a value sig * 2^lsb is rounded to the destination's
precision at its exponent (or at the minimum normal exponent, for subnormal results), and the
encoding falls out of the arithmetic. Overflow, infinity and NaN rules are parameters, because
SASS conversions differ there (SATFINITE, E4M3 without infinity, canonical NaNs).

Independent of tests/refs/fpformat.py, which is the oracle.
"""
from typing import NamedTuple
from sass.sem import concat, const, extract, ite, lnot, smax, umin, zext

W = 32  # width of the exponent and significand arithmetic


class Format(NamedTuple):
    ebits: int
    mbits: int
    bias: int
    special: str | None   # "ieee": all-ones exponent is Inf/NaN; "max": all-ones magnitude is NaN

    @property
    def width(self): return 1 + self.ebits + self.mbits
    @property
    def emin(self): return 1 - self.bias
    @property
    def max_code(self):
        """Largest finite magnitude encoding."""
        top = (1 << (self.ebits + self.mbits)) - 1
        return {"ieee": top - (1 << self.mbits), "max": top - 1, None: top}[self.special]
    @property
    def inf_code(self): return ((1 << self.ebits) - 1) << self.mbits if self.special == "ieee" else None
    @property
    def nan_code(self): return (1 << (self.ebits + self.mbits)) - 1 if self.special else None


FORMATS = {
    "F32": Format(8, 23, 127, "ieee"), "F16": Format(5, 10, 15, "ieee"), "BF16": Format(8, 7, 127, "ieee"),
    "E6M9": Format(6, 9, 31, "ieee"), "E5M2": Format(5, 2, 15, "ieee"), "E4M3": Format(4, 3, 7, "max"),
    "E3M4": Format(3, 4, 3, "max"), "E3M2": Format(3, 2, 3, None), "E2M3": Format(2, 3, 1, None),
    "E2M1": Format(2, 1, 1, None), "E0M3": Format(0, 3, -2, None)}

ROUNDING = ("RN", "RZ", "RP", "RM")


def fields(x, fmt):
    """(sign, exponent field, fraction) of an encoding (x is exactly the format's width)."""
    f = FORMATS[fmt]
    return extract(x, f.width - 1, f.width - 1), extract(x, f.width - 2, f.mbits), extract(x, f.mbits - 1, 0)


def classify(x, fmt):
    """(is NaN, is infinite)."""
    f = FORMATS[fmt]
    _, e, m = fields(x, fmt)
    match f.special:
        case "ieee": return (e == -1) & (m != 0), (e == -1) & (m == 0)
        case "max": return extract(x, f.width - 2, 0) == -1, const(0, 1)
    return const(0, 1), const(0, 1)


def narrow(x, src, dst, rnd="RN", satfinite=False, nan=None, overflow=None):
    """The `dst` encoding of the `src` value x (exactly src's width).

    nan(sign): result for a NaN source (default: positive canonical all-ones magnitude).
    overflow(sign): result when the rounded value exceeds the largest finite one in a direction away
    from zero, without SATFINITE (default: signed infinity). Infinite sources always overflow.
    Rounding toward zero (RZ, RP of a negative, RM of a positive value) never overflows: it gives
    the largest finite value. Requires dst's minimum exponent at least src's (a narrowing)."""
    s, d = FORMATS[src], FORMATS[dst]
    if d.emin < s.emin or d.mbits >= s.mbits: raise ValueError(f"{src} -> {dst} is not a narrowing")
    mag_bits = d.ebits + d.mbits
    sign, e, m = fields(x, src)
    is_nan, is_inf = classify(x, src)
    sig = zext(concat(e != 0, m), W)                               # the implicit bit, then the fraction
    lsb = ite(e == 0, const(1, W), zext(e, W)) - (s.bias + s.mbits)    # value = sig * 2^lsb (signed)
    q = smax(lsb + s.mbits, const(d.emin, W)) - d.mbits                # exponent of the result's ulp
    # Source subnormals sit below dst's minimum exponent, so the result's ulp is q either way.
    shift = umin(q - lsb, const(30, W))                                # >= 1; clamped: tiny values keep 0
    kept, rest = sig >> shift, sig & ((const(1, W) << shift) - 1)
    half = const(1, W) << (shift - 1)
    negative = sign == 1
    up = {"RN": (rest > half) | ((rest == half) & (extract(kept, 0, 0) == 1)), "RZ": const(0, 1),
          "RP": (rest != 0) & lnot(negative), "RM": (rest != 0) & negative}[rnd]
    code = ((q - (d.emin - d.mbits)) << d.mbits) + kept + zext(up, W)   # a carry renormalizes by itself
    toward_zero = {"RN": const(0, 1), "RZ": const(1, 1), "RP": negative, "RM": lnot(negative)}[rnd]
    largest = concat(sign, const(d.max_code, mag_bits))
    away = largest if satfinite else overflow(sign) if overflow else concat(sign, const(d.inf_code, mag_bits))
    finite = ite(code > d.max_code, ite(toward_zero, largest, away), concat(sign, extract(code, mag_bits - 1, 0)))
    return ite(is_nan, nan(sign) if nan else const(d.nan_code, d.width), ite(is_inf, away, finite))


def relu(x, fmt):
    """RELU: negative results become +0, NaNs stay."""
    is_nan, _ = classify(x, fmt)
    return ite((extract(x, x.w - 1, x.w - 1) == 1) & lnot(is_nan), const(0, x.w), x)
