"""IEEE conversions on sass.semantics.fp.fpformat: FRND (round to integral), I2F (integer to float), F2F (float
to float) and F2I's general forms (float to integer). Rounding is exact in every mode; NaN and
out-of-range results follow SM89 hardware (tests/cases/conversions.py GPU fixtures)."""
from sass.semantics.fp import fpformat
from sass.semantics.fp.fpformat import FORMATS
from sass.semantics.bitvec import *
from sass.semantics.family import family, helper, outputs, unmodelled
from sass.semantics.families.floating import readable

FLOATS = {16: "F16", 32: "F32", 64: "F64"}
MODES = {"ROUND": "RN", "FLOOR": "RM", "CEIL": "RP", "TRUNC": "RZ", "RN": "RN", "RM": "RM", "RP": "RP", "RZ": "RZ"}


def rounding(i):
    """The rnd selector's mode, chosen at decode time (one body serves every mode)."""
    return fpformat.chosen_rounding(i, "rnd", {l: MODES[l] for l in i.labels("rnd") if l in MODES})


def lookup(i, name, table, w):
    """select_lookup_const over the labels the class has."""
    return i.select_lookup_const(name, {l: v for l, v in table.items() if l in i.labels(name)}, w)


@helper
def flush(i, x, fmt):
    """The fmt value x with a denormal flushed to a zero of its sign under .FTZ (an input flush:
    SM89 FRND/F2I)."""
    if not (i.has_operand("ftz") and i.select_fork("ftz") == "FTZ"): return x
    f = FORMATS[fmt]
    return const(0, x.w) | (x & (1 << f.width - 1)) if extract(x, f.width - 2, f.mbits) == 0 else x


def source(i, names, w):
    """The float source of width w (F16 from the register's H0/H1 half), with |x| and -x."""
    name = i.resolve(names)
    v = i.value(name, modifiers=False)
    if w == 16 and v.w > 16:
        match str(i.select_fork("hsel")) if i.has_operand("hsel") else "H0":
            case "H0": v = extract(v, 15, 0)
            case "H1": v = extract(v, 31, 16)
            case other: unmodelled(f"hsel {other}")
    if v.w != w: unmodelled(f"{v.w}-bit source for F{w}")
    sign = 1 << w - 1
    v = i.modified(name, "absolute", v, lambda x: x & (sign - 1))
    return i.modified(name, "negate", v, lambda x: x ^ sign)


def quiet(x, src, dst):
    """The NaN x as a dst NaN: sign kept, payload aligned at the top of the fraction, quiet bit set."""
    s, d = FORMATS[src], FORMATS[dst]
    payload = extract(x, s.mbits - 1, 0)
    payload = concat(payload, const(0, d.mbits - s.mbits)) if d.mbits > s.mbits else extract(payload, s.mbits - 1, s.mbits - d.mbits)
    return concat(extract(x, s.width - 1, s.width - 1), const(-1, d.ebits), payload | (1 << d.mbits - 1))


def is_nan(x, fmt): return fpformat.classify(x, fmt)[0]


@helper
def round_integral(x, fmt, r):
    """x rounded to an integral value in its format with Rounding r, the sign of a zero kept; NaN
    and infinity unchanged."""
    f = FORMATS[fmt]
    sign, e = extract(x, f.width - 1, f.width - 1) == 1, zext(extract(x, f.width - 2, f.mbits), 32) - f.bias
    magnitude = x & ((1 << f.width - 1) - 1)
    one = const((f.bias << f.mbits), f.width)
    # |x| < 1: a signed zero or one; the whole magnitude is dropped, and encodings order like
    # values, so 0.5's encoding is the half (a tie rounds to the even 0).
    small_up = fpformat.round_up(r, const(0, 1), magnitude, const(f.bias - 1 << f.mbits, f.width), sign)
    small = (x & (1 << f.width - 1)) | (one if small_up else const(0, f.width))
    # 1 <= |x| < 2^mbits: clear the fraction bits below the units, then step up one unit.
    frac = zext(f.mbits - e, f.width)
    unit = const(1, f.width) << frac
    rest, kept = x & (unit - 1), x & ~(unit - 1)
    up = fpformat.round_up(r, kept >> frac, rest, unit >> 1, sign)
    middle = kept + (unit if up else const(0, f.width))
    return small if slt(e, const(0, 32)) else (middle if slt(e, const(f.mbits, 32)) else x)


@family("FRND", "UFRND")
def frnd(i):
    """Rd = Rb rounded to an integral value (ROUND/FLOOR/CEIL/TRUNC), .FTZ flushing a denormal input.
    NaN: F32 gives 0x7fffffff, F64 keeps its payload, quieted (SM89 GPU). F16/BF16 are unmodelled."""
    fmt = str(i.select_fork("fmt"))
    if fmt not in ("F32", "F64"): unmodelled(fmt)
    w = int(fmt[1:])
    x = flush(i, source(i, "Rb|Sb", w), fmt)
    nan = const(0x7fffffff, 32) if w == 32 else quiet(x, "F64", "F64")
    return outputs(Rd=nan if is_nan(x, fmt) else round_integral(x, fmt, rounding(i)))


INTEGERS = {"S32": (32, True), "U32": (32, False), "S64": (64, True), "U64": (64, False)}


@helper
def float_to_int(x, fmt, n, signed, r, nan):
    """x rounded to an integer with Rounding r and saturated to n bits, signed (a 1-bit expression)
    or not; NaN gives the n-bit `nan`."""
    f = FORMATS[fmt]
    sign, exponent = extract(x, f.width - 1, f.width - 1) == 1, extract(x, f.width - 2, f.mbits)
    is_nan, is_inf = fpformat.classify(x, fmt)
    e = (const(1, 32) if exponent == 0 else zext(exponent, 32)) - f.bias            # the leading bit's exponent
    sig = zext(concat(exponent != 0, extract(x, f.mbits - 1, 0)), 64)
    # Exact below 2^64: shifted left, or right with the dropped bits deciding the rounding.
    left = sig << zext(e - f.mbits, 64)
    shift = zext(umin(f.mbits - e, const(63, 32)), 64)
    kept, rest = sig >> shift, sig & ((const(1, 64) << shift) - 1)
    up = fpformat.round_up(r, kept, rest, const(1, 64) << (shift - 1), sign)
    magnitude = left if sle(const(f.mbits, 32), e) else kept + zext(up, 64)
    huge = is_inf | sle(const(n, 32), e)                                             # |x| >= 2^n
    low = 1 << n - 1                                                                 # |signed minimum|
    high = const((1 << n - 1) - 1, 64) if signed else const((1 << n) - 1, 64)
    negative = (const(low, n) if huge | (const(low, 64) < magnitude) else trunc(-magnitude, n)) if signed else const(0, n)
    positive = trunc(high, n) if huge | (high < magnitude) else trunc(magnitude, n)
    return nan if is_nan else (negative if sign else positive)


@family("F2I", fallback="prim")
def f2i(i):
    """Rd = Rb (F16 or BF16 from H0/H1, F32, F64) rounded to an integer (ROUND/FLOOR/CEIL/TRUNC) and
    saturated to S32/U32/S64/U64, .FTZ flushing a denormal input. NaN gives 0 with .NTZ, else the
    destination's top bit (SM89 GPU, also U64). The readable lowering takes every mode as TRUNC.
    8/16-bit destinations and predicate outputs are unmodelled."""
    src = str(i.select_fork("srcfmt"))
    if src not in ("F16", "BF16", "F32", "F64"): unmodelled(src)
    if any(i.has_operand(n) for n in ("Pu", "Pv")): unmodelled("predicate outputs")
    n = i.width("Rd")
    signed = lookup(i, "dstfmt", {f"{s}{n}": int(s == "S") for s in "SU"}, 1)
    nan = lookup(i, "ntz", {"NTZ": 0, "nontz": 1 << n - 1}, n)
    r = fpformat.rounding("RZ") if readable(i) and i.labels("rnd") else rounding(i)
    if readable(i): i.ignore("rnd")
    x = flush(i, source(i, "Rb|Sb", FORMATS[src].width), src)
    return outputs(Rd=float_to_int(x, src, n, signed, r, nan))


SOURCES = {"S8": (8, True), "U8": (8, False), "S16": (16, True), "U16": (16, False),
           "S32": (32, True), "U32": (32, False), "S64": (64, True), "U64": (64, False)}


def integer_source(i, n):
    """The n-bit integer source: a byte (bsel) or half (hsel) of a register, or the whole value."""
    name = i.resolve("Rb|Sb")
    x = i.value(name, modifiers=False)
    if x.w < n and i.kind(name) == "immediate": return (sext if i.atom(name).type == "SImm" else zext)(x, n)
    if n < x.w:
        selector = "bsel" if n == 8 else "hsel"
        part = str(i.select_fork(selector)) if i.has_operand(selector) else ("B0" if n == 8 else "H0")
        if part not in ("B0", "B1", "B2", "B3", "H0", "H1"): unmodelled(f"{selector} {part}")
        lo = int(part[1]) * n
        return extract(x, lo + n - 1, lo)
    if x.w != n: unmodelled(f"{x.w}-bit source")
    return x


DESTS = ("F16", "BF16", "F32", "F64")


@family("I2F", "I2FP", fallback="prim")
def i2f(i):
    """Rd = Rb (8/16-bit byte/half, 32- or 64-bit, signed or not) as F16/BF16/F32/F64, rounded once
    in rnd's mode; I2FP is the same conversion (S32/U32 to F32, RN/RZ). SM89: exact in every mode,
    F16 overflows to infinity (the largest finite value rounding toward zero), a 16-bit result is
    zero-extended into Rd. The destination format and mode are decode-time choices."""
    src = str(i.select_fork("srcfmt"))
    if src not in SOURCES: unmodelled(src)
    n, signed = SOURCES[src]
    dsts = [l for l in i.labels("dstfmt") if l in DESTS and FORMATS[l].width <= i.width("Rd")]
    field = lambda name, w=64: lookup(i, "dstfmt", {l: int(getattr(fpformat.dest(l), name)) for l in dsts}, w)
    d = fpformat.Dest(field("mbits"), field("emin"), field("max_code"), field("nan_signed", 1), field("nan_mag"), field("ovf_signed", 1), field("ovf_mag"))
    sign, mag, _ = fpformat.round_parts(fpformat.int_parts(integer_source(i, n), signed), d, rounding(i), general=True)
    top = lookup(i, "dstfmt", {l: FORMATS[l].width - 1 for l in dsts}, 64)
    return outputs(Rd=resize((zext(sign, 64) << top) | mag, i.width("Rd")))


@family("F2F")
def f2f(i):
    """Rd = Rb converted between F16, F32 and F64, rounded once in rnd's mode (narrowing); a NaN
    keeps its sign and payload, quieted (SM89 GPU). The readable lowering rounds to nearest.
    .FTZ and the BF16/E8M7/E6M9 formats are unmodelled."""
    dst, src = str(i.select_fork("dstfmt.srcfmt")).split(".")
    if src not in FLOATS.values() or dst not in FLOATS.values(): unmodelled(f"{dst}.{src}")
    if i.select_fork("ftz") != "noftz": unmodelled("ftz")
    s, d = FORMATS[src], FORMATS[dst]
    x = source(i, "Rb|Sb", s.width)
    r = rounding(i)
    if readable(i): r = fconv(x, d.width)
    elif d.width > s.width: r = fpformat.widen(x, src, dst)
    else:
        sign, mag, _ = fpformat.round_parts(fpformat.parts(x, src), fpformat.dest(dst), r)
        r = concat(sign, extract(mag, d.width - 2, 0))
    r = quiet(x, src, dst) if is_nan(x, src) else r
    return outputs(Rd=resize(r, i.width("Rd")))


# SM120's uniform twins; their unmodelled encodings are opaque (the runtime primitives are scalar only).
for uniform, scalar in (("UF2I", f2i), ("UI2F", i2f), ("UI2FP", i2f)): family(uniform)(scalar.__wrapped__)
