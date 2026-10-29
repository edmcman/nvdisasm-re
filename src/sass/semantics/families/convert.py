"""IEEE conversions on sass.semantics.fp.fpformat: FRND (round to integral), I2F (integer to float), F2F (float
to float) and F2I's general forms (float to integer). Rounding is exact in every mode; NaN and
out-of-range results follow SM89 hardware (tests/cases/conversions.py GPU fixtures)."""
from functools import wraps

from sass.semantics.fp import fpformat
from sass.semantics.fp.fpformat import FORMATS
from sass.semantics.bitvec import *
from sass.semantics.family import definition, family, float_abs, float_neg, helper, outputs, same, unmodelled
from sass.semantics.families.floating import IEEE, flushed, readable

FLOATS = {16: "F16", 32: "F32", 64: "F64"}
MODES = {"ROUND": "RN", "FLOOR": "RM", "CEIL": "RP", "TRUNC": "RZ", "RN": "RN", "RM": "RM", "RP": "RP", "RZ": "RZ"}


def rounding(i):
    """The rnd selector's mode, chosen at decode time (one body serves every mode)."""
    return fpformat.chosen_rounding(i, "rnd", {l: MODES[l] for l in i.labels("rnd") if l in MODES})


def lookup(i, name, table, w):
    """select_lookup_const over the labels the class has."""
    return i.select_lookup_const(name, {l: v for l, v in table.items() if l in i.labels(name)}, w)


def flush(i, x, fmt):
    """The fmt value x flushed (ftz) under .FTZ: an input flush (SM89 FRND/F2I)."""
    if not (i.has_operand("ftz") and i.select_fork("ftz") == "FTZ"): return x
    return flushed(x, None if IEEE.get(x.w) == fmt else fmt)


def source(i, names, w, modified=True):
    """The float source of width w (F16 from the register's H0/H1 half), with |x| and -x unless not `modified`."""
    name = i.resolve(names)
    v = i.value(name, modifiers=False)
    if w == 16 and v.w > 16:
        match str(i.select_fork("hsel")) if i.has_operand("hsel") else "H0":
            case "H0": v = extract(v, 15, 0)
            case "H1": v = extract(v, 31, 16)
            case other: unmodelled(f"hsel {other}")
    if v.w != w: unmodelled(f"{v.w}-bit source for F{w}")
    if not modified: return v
    v = i.modified(name, "absolute", v, float_abs)
    return i.modified(name, "negate", v, float_neg)


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


def integral_name(a):
    """cvt.r<mode>i.<fmt>.<fmt> of the source (and the register whose NaN it keeps, when another);
    without a constant mode, the mode's flags are arguments."""
    m, fmt, sources = fpformat.mode(a["r"]), a["fmt"].lower(), ["x", *(["raw"] if a["raw"] is not None else [])]
    return (f"cvt.{m.lower()}i.{fmt}.{fmt}", sources) if m else (f"cvt.ri.{fmt}.{fmt}", ["r", *sources])


@definition(integral_name)
def integral(fmt, r, x, raw=None):
    """PTX cvt.rni/rzi/rmi/rpi to an integral value in the same format, the sign of a zero kept and
    infinity unchanged; a NaN gives 0x7fffffff (F32), else the source register's sign and payload,
    quieted (the GPU's).

    raw: that register, when it isn't x."""
    nan = const(0x7fffffff, 32) if fmt == "F32" else quiet(x if raw is None else raw, "F64", "F64")
    return nan if is_nan(x, fmt) else round_integral(x, fmt, r)


@family("FRND", "UFRND")
def frnd(i):
    """Rd = Rb rounded to an integral value (ROUND/FLOOR/CEIL/TRUNC), .FTZ flushing a denormal input.
    NaN: F32 gives 0x7fffffff, F64 keeps the register's sign and payload, quieted, ignoring -/|.| (SM89 GPU). F16/BF16 are unmodelled."""
    fmt = str(i.select_fork("fmt"))
    if fmt not in ("F32", "F64"): unmodelled(fmt)
    w = int(fmt[1:])
    x = flush(i, source(i, "Rb|Sb", w), fmt)
    raw = source(i, "Rb|Sb", w, modified=False)
    return outputs(Rd=integral(fmt, rounding(i), x, None if w == 32 or raw is x else raw))


INTEGERS = {"S32": (32, True), "U32": (32, False), "S64": (64, True), "U64": (64, False)}


def int_name(a):
    """cvt.r<mode>i[.ntz].<s|u><n>.<fmt> (.ntz: NaN gives 0) for constant choices, else cvt.i<n>.<fmt>
    of them too."""
    n, fmt, m, signed, nan = a["n"], a["fmt"].lower(), fpformat.mode(a["r"]), a["signed"], a["nan"]
    bits = a["limit_bits"]
    logical = int(value(bits)) if bits is not None and is_const(bits) else n
    if m and is_const(signed) and is_const(nan) and value(nan) in (0, 1 << logical - 1):
        return f"cvt.{m.lower()}i{'.ntz' if value(nan) == 0 else ''}.{'s' if value(signed) else 'u'}{logical}.{fmt}", ["x"]
    return f"cvt.i{n}.{fmt}", ["signed", "r", "nan", "x", *(["limit_bits"] if bits is not None else [])]


def full_integer_width(fn):
    """Canonicalize full width when a specialized name omits the width operand."""
    @wraps(fn)
    def call(x, fmt, n, signed, r, nan, limit_bits=None):
        if limit_bits is None:
            args = dict(fmt=fmt, n=n, signed=signed, r=r, nan=nan, limit_bits=None)
            if int_name(args)[1] == ["x"]: limit_bits = const(n, 7)
        return fn(x, fmt, n, signed, r, nan, limit_bits)
    return call


@full_integer_width
@definition(int_name)
def float_to_int(x, fmt, n, signed, r, nan, limit_bits=None):
    """PTX cvt.rni/rzi/rmi/rpi from a float to an n-bit signed or unsigned integer, saturated; NaN
    gives the destination's top bit, or 0 with .NTZ (the GPU's).

    signed: a 1-bit expression; nan: the n-bit NaN result.
    limit_bits: the saturated integer width when smaller than the n-bit register.
    Signed numerical results extend through the register; NaN's sentinel does not."""
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
    low = const(1 << n - 1, 64) if limit_bits is None else const(1, 64) << (zext(limit_bits, 64) - 1)
    high = low - 1 if signed else (low << 1) - 1
    negative = (trunc(-low, n) if huge | (low < magnitude) else trunc(-magnitude, n)) if signed else const(0, n)
    positive = trunc(high, n) if huge | (high < magnitude) else trunc(magnitude, n)
    return nan if is_nan else (negative if sign else positive)


@family("F2I")
def f2i(i):
    """Rd = Rb (F16 or BF16 from H0/H1, F32, F64) rounded to an integer (ROUND/FLOOR/CEIL/TRUNC) and
    saturated to S8/U8/S16/U16/S32/U32/S64/U64, .FTZ flushing a denormal input. NaN gives 0 with .NTZ, else the
    destination's top bit (SM89 GPU, also U64). The readable lowering takes every mode as TRUNC.
    Narrow signed results are sign-extended into Rd, except NaN's sentinel is zero-extended.
    Predicate outputs and narrow BF16 sources are unmodelled."""
    src = str(i.select_fork("srcfmt"))
    if src not in ("F16", "BF16", "F32", "F64"): unmodelled(src)
    if any(i.has_operand(n) for n in ("Pu", "Pv")): unmodelled("predicate outputs")
    n = i.width("Rd")
    formats = {label: (bits, sg) for label, (bits, sg) in SOURCES.items()
               if bits <= n and (src != "BF16" or bits >= 32)}
    signed = lookup(i, "dstfmt", {label: int(sg) for label, (_, sg) in formats.items()}, 1)
    bits = lookup(i, "dstfmt", {label: bits for label, (bits, _) in formats.items()}, 7)
    top = lookup(i, "dstfmt", {label: 1 << bits - 1 for label, (bits, _) in formats.items()}, n)
    nan = i.select_lookup("ntz", {"NTZ": const(0, n), "nontz": top})
    r = fpformat.Rounding(*(const(int(v), 1) for v in fpformat.rounding("RZ"))) if readable(i) and i.labels("rnd") else rounding(i)
    if readable(i): i.ignore("rnd")
    x = flush(i, source(i, "Rb|Sb", FORMATS[src].width), src)
    return outputs(Rd=float_to_int(x, src, n, signed, r, nan, bits))


SOURCES = {"S8": (8, True), "U8": (8, False), "S16": (16, True), "U16": (16, False),
           "S32": (32, True), "U32": (32, False), "S64": (64, True), "U64": (64, False)}


def integer_source(i, n):
    """The n-bit integer source: a byte (bsel) or half (hsel) of a register, or the whole value."""
    name = i.resolve("Rb|Sb")
    x = i.value(name, modifiers=False)
    if x.w < n and i.kind(name) == "immediate":
        # SM89 I2F's 64-bit immediates carry the high word, as F64 immediates do.
        if n == 64 and x.w == 32: return zext(x, 64) << 32
        return (sext if i.atom(name).type == "SImm" else zext)(x, n)
    if n < x.w:
        selector = "bsel" if n == 8 else "hsel"
        part = str(i.select_fork(selector)) if i.has_operand(selector) else ("B0" if n == 8 else "H0")
        if part not in ("B0", "B1", "B2", "B3", "H0", "H1"): unmodelled(f"{selector} {part}")
        lo = int(part[1]) * n
        return extract(x, lo + n - 1, lo)
    if x.w != n: unmodelled(f"{x.w}-bit source")
    return x


DESTS = ("F16", "BF16", "F32", "F64")


def i2f_name(a):
    """cvt.<mode>.<dst>.<src> for a constant destination and mode, else cvt.b<result width>.<src> of their fields."""
    fmt, m = fpformat.format_of(a["d"]), fpformat.mode(a["r"])
    if fmt and m and all(same(x, int(y)) for x, y in zip(a["d"], fpformat.dest(fmt))):
        return f"cvt.{m.lower()}.{fmt.lower()}.{a['src'].lower()}", ["x"]
    return f"cvt.b{a['w']}.{a['src'].lower()}", ["d", "r", "top", "x"]


@definition(i2f_name)
def int_to_float(src, d, r, top, w, x):
    """PTX cvt from an integer to F16/BF16/F32/F64, rounded once (exact in every mode, SM89); F16
    overflows to infinity (the largest finite value rounding toward zero), a 16-bit result is
    zero-extended.

    d, top: the destination's Dest and sign bit; w: the result's width."""
    n, signed = SOURCES[src]
    sign, mag, _ = fpformat.round_parts(fpformat.int_parts(x, signed), d, r, general=True)
    return resize((zext(sign, 64) << top) | mag, w)


@family("I2F", "I2FP")
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
    x = integer_source(i, n)
    r = rounding(i)
    top = lookup(i, "dstfmt", {l: FORMATS[l].width - 1 for l in dsts}, 64)
    return outputs(Rd=int_to_float(src, d, r, top, i.width("Rd"), x))


@helper
def with_nan(r, x, raw, src, dst):
    """r, or for a NaN x F2F's NaN: canonical between F16 and F32, else raw's sign and payload, quieted."""
    s, d = FORMATS[src], FORMATS[dst]
    nan = const((1 << (d.width - 1)) - 1, d.width) if {s.width, d.width} == {16, 32} else quiet(raw, src, dst)
    return nan if is_nan(x, src) else r


def cvt_name(a):
    """cvt.<mode>.<dst>.<src> of the source (and the register whose NaN it keeps, when another);
    without a constant mode, the mode's flags are arguments. A widening is exact: no mode."""
    src, dst, rounding = a["src"], a["dst"], fpformat.Rounding(a["rn"], a["rz"], a["rp"], a["rm"])
    m = None if FORMATS[dst].width > FORMATS[src].width else fpformat.mode(rounding)
    flags = [] if FORMATS[dst].width > FORMATS[src].width or m else ["rn", "rz", "rp", "rm"]
    return ".".join(["cvt", *([m.lower()] if m else []), dst.lower(), src.lower()]), [*flags, "x", *(["raw"] if a["raw"] is not None else [])]


@definition(cvt_name)
def converted(src, dst, rn, rz, rp, rm, x, raw=None):
    """PTX cvt between F16, F32 and F64 with the GPU's NaN, rounded once (narrowing) or exact
    (widening); a NaN gives the canonical NaN between F16 and F32, else the source register's sign
    and payload, quieted.

    raw: that register, when it isn't x (a modified source)."""
    s, d = FORMATS[src], FORMATS[dst]
    raw = x if raw is None else raw
    if d.width > s.width: return with_nan(fpformat.widen(x, src, dst), x, raw, src, dst)
    sign, mag, _ = fpformat.round_parts(fpformat.parts(x, src), fpformat.dest(dst), fpformat.Rounding(rn, rz, rp, rm))
    return with_nan(concat(sign, extract(mag, d.width - 2, 0)), x, raw, src, dst)


@family("F2F")
def f2f(i):
    """Rd = Rb converted between F16, F32 and F64, rounded once in rnd's mode (narrowing); a NaN
    is canonicalized for F16/F32 conversions, otherwise keeps the register's quieted sign/payload, ignoring -/|.| (SM89 GPU).
    The readable lowering rounds to nearest.
    .FTZ flushes an F32 input when widening to F64 (SM89); other .FTZ directions
    and the BF16/E8M7/E6M9 formats are unmodelled."""
    dst, src = str(i.select_fork("dstfmt.srcfmt")).split(".")
    if src not in FLOATS.values() or dst not in FLOATS.values(): unmodelled(f"{dst}.{src}")
    ftz = i.select_fork("ftz") == "FTZ"
    if ftz and (src, dst) != ("F32", "F64"): unmodelled("uninvestigated FTZ direction")
    s, d = FORMATS[src], FORMATS[dst]
    x = source(i, "Rb|Sb", s.width)
    if ftz: x = flushed(x)
    r = rounding(i)
    raw = source(i, "Rb|Sb", s.width, modified=False)
    if readable(i): r = with_nan(fconv(x, d.width), x, raw, src, dst)
    else:   # a widening is exact, its mode moot; the canonical NaN reads no register
        exact, canonical = d.width > s.width, {s.width, d.width} == {16, 32}
        r = converted(src, dst, *(fpformat.rounding("RN") if exact else r), x, None if canonical or raw is x else raw)
    return outputs(Rd=resize(r, i.width("Rd")))


# SM120's uniform twins; their unmodelled encodings are opaque (the runtime primitives are scalar only).
for uniform, scalar in (("UF2I", f2i), ("UI2F", i2f), ("UI2FP", i2f)): family(uniform)(scalar.__wrapped__)
