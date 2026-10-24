"""Floating-point formats and conversions as `sass.semantics.bitvec` bitvector expressions.

One rounding routine serves every narrowing: a value sig * 2^lsb is rounded to the destination's
precision at its exponent (or at the minimum normal exponent, for subnormal results), and the
encoding falls out of the arithmetic. Overflow, infinity and NaN rules are parameters, because
SASS conversions differ there (SATFINITE, E4M3 without infinity, canonical NaNs).

Independent of tests/refs/fpformat.py, which is the oracle.
"""
from typing import NamedTuple
from sass.semantics.bitvec import Expr, concat, const, extract, is_const, ite, lnot, signed, sle, slt, smax, umin, zext
from sass.semantics.family import definition

W = 32  # width of the exponent and significand arithmetic; 64 for formats wider than 32 bits


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
    "E2M1": Format(2, 1, 1, None), "E0M3": Format(0, 3, -2, None), "F64": Format(11, 52, 1023, "ieee"),
    "UE5M3": Format(5, 3, 15, "max")}   # unsigned: its code is the magnitude alone (NaN 0xff)

ROUNDING = ("RN", "RZ", "RP", "RM")


def fields(x, fmt):
    """(sign, exponent field, fraction) of an encoding (x is exactly the format's width); a format
    without exponent bits (E0M3) has a 1-bit zero field."""
    f = FORMATS[fmt]
    exponent = extract(x, f.width - 2, f.mbits) if f.ebits else const(0, 1)
    return extract(x, f.width - 1, f.width - 1), exponent, extract(x, f.mbits - 1, 0)


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
    format chosen by the instruction, via `i.select_lookup_const`): the conversion is then one computation."""
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
    """Which rounding mode applies: Python bools (one mode) or 1-bit expressions (`i.select_lookup_const`)."""
    rn: object
    rz: object
    rp: object
    rm: object


def rounding(rnd): return Rounding(*(rnd == m for m in ROUNDING))


def chosen_rounding(i, name, modes):
    """The Rounding an instruction's selector `name` chooses at decode time; modes maps its labels
    to RN/RZ/RP/RM (other labels are unmodelled)."""
    return Rounding(*(i.select_lookup_const(name, {l: int(m == mode) for l, m in modes.items()}, 1) for mode in ROUNDING))


def constant(x):
    """x as a signed Python int when it is a constant (a decode-time choice the lifter resolved), else None."""
    if not isinstance(x, Expr): return int(x)
    return signed(x.args[0], x.w) if is_const(x) else None


def mode(r):
    """The rounding mode a Rounding of constants chooses, else None."""
    flags = [constant(f) for f in r]
    return None if None in flags or sum(map(bool, flags)) != 1 else ROUNDING[[bool(f) for f in flags].index(True)]


def format_of(d):
    """The format whose mbits, emin and max_code a Dest of constants has, else None."""
    key = tuple(constant(x) for x in (d.mbits, d.emin, d.max_code))
    return next((n for n, f in FORMATS.items() if (f.mbits, f.emin, f.max_code) == key), None)


def round_up(r, kept, rest, half, negative):
    """Whether Rounding r adds one unit to `kept`, given the dropped bits `rest` and half a unit."""
    return value((r.rn & ((rest > half) | ((rest == half) & (extract(kept, 0, 0) == 1))))
                 | (r.rp & (rest != 0) & lnot(negative)) | (r.rm & (rest != 0) & negative), 1)


def value(x, w=W): return x if isinstance(x, Expr) else const(int(x), w)


def arith(*fmts):
    """The arithmetic width for values of these formats."""
    return 64 if any(FORMATS[f].width > 32 for f in fmts) else W


class Parts(NamedTuple):
    """A value sign * sig * 2^lsb (sig, lsb, top of one width, W or 64; lsb signed); top is the exponent of sig's leading
    bit, or an upper bound of it where that gives the same rounding (see parts)."""
    sign: Expr
    sig: Expr
    lsb: Expr
    top: Expr
    nan: Expr
    inf: Expr

    def scaled(self, k):
        """The value times 2^k (k a signed expression of the parts' width): exact, only the exponents move."""
        return self._replace(lsb=self.lsb + k, top=self.top + k)


def leading(sig, bits):
    """The position of the highest set bit of sig (< 2^bits); 0 for 0. A binary search: log2(bits) steps."""
    lead, step = const(0, sig.w), 1 << (bits - 1).bit_length() - 1
    while step:
        high = (sig >> (lead + step)) != 0
        lead = ite(high, lead + step, lead)
        step >>= 1
    return lead


def parts(x, fmt, normalized=False):
    """The Parts of the `fmt` encoding x. A subnormal's top is that of the implicit bit unless
    normalized: a narrowing's result puts such values below its minimum exponent anyway."""
    f = FORMATS[fmt]
    sign, e, m = fields(x, fmt)
    is_nan, is_inf = classify(x, fmt)
    w = arith(fmt)
    sig = zext(concat(e != 0, m), w) if f.ebits else zext(m, w)        # the implicit bit, then the fraction
    lsb = (ite(e == 0, const(1, w), zext(e, w)) if f.ebits else const(1, w)) - (f.bias + f.mbits)
    top = lsb + (leading(sig, f.mbits + 1) if normalized else const(f.mbits, w))
    return Parts(sign, sig, lsb, top, is_nan, is_inf)


def fixed_parts(x, frac_bits):
    """The Parts of the two's complement fixed-point number x with frac_bits fraction bits (S2_6)."""
    sign = extract(x, x.w - 1, x.w - 1)
    sig = zext(ite(sign == 1, -x, x), W)                                # the most negative value stays exact unsigned
    lsb = const(-frac_bits, W)
    return Parts(sign, sig, lsb, lsb + leading(sig, x.w + 1), const(0, 1), const(0, 1))


def int_parts(x, signed):
    """The Parts of the integer x (signed: two's complement), at width 64."""
    sign = slt(x, const(0, x.w)) if signed else const(0, 1)            # a comparison of x itself: reads only x's bytes
    sig = zext(ite(sign == 1, -x, x), 64)                               # the most negative value stays exact unsigned
    lsb = const(0, 64)
    return Parts(sign, sig, lsb, leading(sig, x.w), const(0, 1), const(0, 1))


def narrow_parts(x, src, d, r, satfinite=False):
    """(sign, magnitude, source is NaN) of the narrowing of the `src` value x to Dest d with
    Rounding r; sign is 1 bit, magnitude of the arithmetic width. Requires the destination's minimum exponent at
    least src's (a narrowing)."""
    return round_parts(parts(x, src), d, r, satfinite)


def round_parts(p, d, r, satfinite=False, general=False):
    """(sign, magnitude, NaN) of the Parts p rounded to Dest d with Rounding r. Rounding toward zero
    (RZ, RP of a negative, RM of a positive value) never overflows: it gives the largest finite
    value; infinite values always overflow. general: p may be any value (scaled): it may
    need fewer bits than d keeps (shifted left, exact), and zero needs its own case; otherwise p
    comes from a wider format by parts(), whose zero and subnormals lie below d's minimum exponent."""
    sign, sig, lsb, is_nan, is_inf = p.sign, p.sig, p.lsb, p.nan, p.inf
    w = sig.w
    q = smax(p.top, value(d.emin, w)) - d.mbits                         # exponent of the result's ulp
    shift = umin(q - lsb, const(w - 2, w))                              # clamped: tiny values keep 0
    kept, rest = sig >> shift, sig & ((const(1, w) << shift) - 1)
    half = const(1, w) << (shift - 1)
    negative = sign == 1
    up = round_up(r, kept, rest, half, negative)
    code = ((q - (d.emin - d.mbits)) << d.mbits) + kept + zext(up, w)   # a carry renormalizes by itself
    if general: code = ite(sle(q, lsb), exact_code(p, d), code)
    toward_zero = value(r.rz | (r.rp & negative) | (r.rm & lnot(negative)), 1)
    signed = lambda keep: ite(keep, negative, const(0, 1))
    away_sign = ite(satfinite, negative, signed(d.ovf_signed))
    away_mag = ite(satfinite, value(d.max_code, w), value(d.ovf_mag, w))
    big = code > value(d.max_code, w)
    choose = lambda nan, inf, finite: ite(is_nan, nan, ite(is_inf, inf, finite))
    out_sign = choose(signed(d.nan_signed), away_sign, ite(big & lnot(toward_zero), away_sign, negative))
    out_mag = choose(value(d.nan_mag, w), away_mag, ite(big, ite(toward_zero, value(d.max_code, w), away_mag), code))
    return out_sign, out_mag, is_nan


def exact_code(p, d):
    """The magnitude code of the Parts p when d holds it exactly (no bits below d's ulp): the
    significand shifted into place, its leading bit carrying into the exponent field."""
    w = p.sig.w
    q = smax(p.top, value(d.emin, w)) - d.mbits
    return ite(p.sig == 0, const(0, w), ((q - (d.emin - d.mbits)) << d.mbits) + (p.sig << (p.lsb - q)))


def widen(x, src, dst, nan=None):
    """The `dst` encoding of the `src` value x, exactly (dst holds every src value): a normal value
    is re-biased, a subnormal shifted onto dst's subnormal grid, or normalized where dst's range
    makes it normal. NaN: the (signed, magnitude) result, default positive canonical."""
    s, f, d = FORMATS[src], FORMATS[dst], dest(dst, nan)
    if f.emin > s.emin or f.mbits < s.mbits: raise ValueError(f"{src} -> {dst} is not a widening")
    sign, e, m = fields(x, src)
    is_nan, is_inf = classify(x, src)
    w = arith(src, dst)
    normal = ((zext(e, w) + (f.bias - s.bias)) << f.mbits) | (zext(m, w) << (f.mbits - s.mbits))
    shift = (s.emin - s.mbits) - (f.emin - f.mbits)
    if s.emin == f.emin: sub = zext(m, w) << shift                       # subnormals stay subnormal
    elif s.emin - s.mbits >= f.emin:                                    # subnormals are normal in dst
        # m * 2^(s.emin - s.mbits) with leading bit b: exponent b + s.emin - s.mbits, fraction below b.
        sub = const(0, w)
        for b in range(s.mbits):
            sub = ite(extract(m, b, b) == 1, ((b + s.emin - s.mbits + f.bias) << f.mbits)
                      | ((zext(m, w) << (f.mbits - b)) & ((1 << f.mbits) - 1)), sub)
    else: sub = exact_code(parts(x, src, normalized=True), d)
    code = ite(e == 0, sub, normal) if s.ebits else sub
    mag = ite(is_nan, value(d.nan_mag, w), ite(is_inf, const(f.inf_code or f.max_code, w), code))
    return concat(ite(is_nan, const(int(d.nan_signed), 1) & sign, sign), extract(mag, f.ebits + f.mbits - 1, 0))


def narrow_name(a):
    """narrow.<mode>[.satfinite].<dst>.<src>: the NaN and overflow results are its definition's."""
    return f"narrow.{a['rnd'].lower()}{'.satfinite' if a['satfinite'] else ''}.{a['dst'].lower()}.{a['src'].lower()}", ["x"]


@definition(narrow_name)
def narrow(x, src, dst, rnd="RN", satfinite=False, nan=None, overflow=None):
    """A float narrowed to a smaller format, rounded once; a NaN or overflow gives the format's
    (signed, magnitude) results, default positive canonical NaN and signed infinity (FADD2/FHADD2
    narrowing, HMUL2's BF16 forms).

    nan, overflow: (signed, magnitude) results, see dest()."""
    s, f = FORMATS[src], FORMATS[dst]
    if f.emin < s.emin or f.mbits >= s.mbits: raise ValueError(f"{src} -> {dst} is not a narrowing")
    d = dest(dst, nan, overflow)
    # An unsigned destination NaN replaces every source NaN: which NaN the arithmetic gave is moot.
    if x.op == "nanresult" and not isinstance(d.nan_signed, Expr) and not d.nan_signed: x = x.args[0]
    sign, mag, _ = narrow_parts(x, src, d, rounding(rnd), satfinite)
    return concat(sign, extract(mag, f.ebits + f.mbits - 1, 0))


def relu(x, fmt):
    """RELU: negative results become +0, NaNs stay."""
    is_nan, _ = classify(x, fmt)
    return ite((extract(x, x.w - 1, x.w - 1) == 1) & lnot(is_nan), const(0, x.w), x)
