"""Floating-point formats and conversions as `sass.sem` bitvector expressions.

One rounding routine serves every narrowing: a value sig * 2^lsb is rounded to the destination's
precision at its exponent (or at the minimum normal exponent, for subnormal results), and the
encoding falls out of the arithmetic. Overflow, infinity and NaN rules are parameters, because
SASS conversions differ there (SATFINITE, E4M3 without infinity, canonical NaNs).

Independent of tests/refs/fpformat.py, which is the oracle.
"""
from typing import NamedTuple
from sass.sem import Expr, concat, const, extract, ite, lnot, smax, umin, zext

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


class Dest(NamedTuple):
    """A narrowing's destination. Each field is a Python number (one format) or an expression (a
    format chosen by the instruction, via `i.param`): the conversion is then one computation."""
    mbits: object        # mantissa bits
    emin: object         # minimum normal exponent
    max_code: object     # largest finite magnitude
    nan_signed: object   # a NaN source gives (sign if nan_signed else 0, nan_mag)
    nan_mag: object
    ovf_signed: object   # overflow away from zero without SATFINITE gives (sign if ovf_signed else 0, ovf_mag)
    ovf_mag: object


def dest(fmt, nan=None, overflow=None):
    """The Dest of a format: NaN (signed, magnitude) default positive canonical; overflow default
    signed infinity, or the largest finite value for a format without infinity."""
    f = FORMATS[fmt]
    nan = nan or (False, f.nan_code)
    overflow = overflow or ((True, f.inf_code) if f.inf_code is not None else (True, f.max_code))
    return Dest(f.mbits, f.emin, f.max_code, *nan, *overflow)


class Rounding(NamedTuple):
    """Which rounding mode applies: Python bools (one mode) or 1-bit expressions (`i.param`)."""
    rn: object
    rz: object
    rp: object
    rm: object


def rounding(rnd): return Rounding(*(rnd == m for m in ROUNDING))


def value(x, w=W): return x if isinstance(x, Expr) else const(int(x), w)


def narrow_parts(x, src, d, r, satfinite=False):
    """(sign, magnitude, source is NaN) of the narrowing of the `src` value x to Dest d with
    Rounding r; sign is 1 bit, magnitude W bits. Rounding toward zero (RZ, RP of a negative, RM of a
    positive value) never overflows: it gives the largest finite value; infinite sources always
    overflow. Requires the destination's minimum exponent at least src's (a narrowing)."""
    s = FORMATS[src]
    sign, e, m = fields(x, src)
    is_nan, is_inf = classify(x, src)
    sig = zext(concat(e != 0, m), W)                                    # the implicit bit, then the fraction
    lsb = ite(e == 0, const(1, W), zext(e, W)) - (s.bias + s.mbits)    # value = sig * 2^lsb (signed)
    q = smax(lsb + s.mbits, value(d.emin)) - d.mbits                    # exponent of the result's ulp
    # Source subnormals sit below the destination's minimum exponent: the result's ulp is q either way.
    shift = umin(q - lsb, const(30, W))                                 # >= 1; clamped: tiny values keep 0
    kept, rest = sig >> shift, sig & ((const(1, W) << shift) - 1)
    half = const(1, W) << (shift - 1)
    negative = sign == 1
    up = value((r.rn & ((rest > half) | ((rest == half) & (extract(kept, 0, 0) == 1))))
               | (r.rp & (rest != 0) & lnot(negative)) | (r.rm & (rest != 0) & negative), 1)
    code = ((q - (d.emin - d.mbits)) << d.mbits) + kept + zext(up, W)   # a carry renormalizes by itself
    toward_zero = value(r.rz | (r.rp & negative) | (r.rm & lnot(negative)), 1)
    signed = lambda keep: ite(keep, negative, const(0, 1))
    away_sign = ite(satfinite, negative, signed(d.ovf_signed))
    away_mag = ite(satfinite, value(d.max_code), value(d.ovf_mag))
    big = code > d.max_code
    choose = lambda nan, inf, finite: ite(is_nan, nan, ite(is_inf, inf, finite))
    out_sign = choose(signed(d.nan_signed), away_sign, ite(big & lnot(toward_zero), away_sign, negative))
    out_mag = choose(value(d.nan_mag), away_mag, ite(big, ite(toward_zero, value(d.max_code), away_mag), code))
    return out_sign, out_mag, is_nan


def narrow(x, src, dst, rnd="RN", satfinite=False, nan=None, overflow=None):
    """The `dst` encoding of the `src` value x with rounding `rnd`. nan, overflow: (signed,
    magnitude) results, see dest()."""
    s, f = FORMATS[src], FORMATS[dst]
    if f.emin < s.emin or f.mbits >= s.mbits: raise ValueError(f"{src} -> {dst} is not a narrowing")
    sign, mag, _ = narrow_parts(x, src, dest(dst, nan, overflow), rounding(rnd), satfinite)
    return concat(sign, extract(mag, f.ebits + f.mbits - 1, 0))


def relu(x, fmt):
    """RELU: negative results become +0, NaNs stay."""
    is_nan, _ = classify(x, fmt)
    return ite((extract(x, x.w - 1, x.w - 1) == 1) & lnot(is_nan), const(0, x.w), x)
