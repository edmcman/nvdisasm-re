"""Floating-point instruction references: min/max rules, conversions, comparisons, and the
known Ghidra emulator underflow band."""
import math
from fractions import Fraction
from sass.semantics.fp import softfloat
from tests.refs.bits import bf16, f16, f32, f64, ftz16, ftz32, nan16, nan32, nan_bf16

FCOMPARISONS = ("LT", "EQ", "LE", "GT", "NE", "GE", "LTU", "EQU", "LEU", "GTU", "NEU", "GEU", "NUM", "NAN")


GPU_NAN = {16: 0x7fff, 32: 0x7fffffff, 64: 0xfff8000000000000}  # NaN results without a NaN operand (SM89)


def rounded(op, inputs, width, mode="RN", ftz=False, sat=False):
    """FADD/FMUL/FFMA (and DADD..., HADD2... lanes): exactly rounded. NaN results are canonical,
    except that FP64 propagates a NaN operand, quieted (SM89; which of two different NaNs is untested)."""
    result = softfloat.arithmetic(op, inputs, width, mode, ftz, sat)
    if softfloat.unpack(result, width)[0] != "nan": return result
    nans = [x for x in inputs if softfloat.unpack(x, width)[0] == "nan"]
    return nans[0] | 1 << 51 if width == 64 and nans else GPU_NAN[width]


def fmz(op, inputs, mode="RN", sat=False, scale=0):
    """FMUL/FFMA.FMZ (SM89): .FTZ, except that a factor with a zero exponent (zero or subnormal)
    makes the product +0, even times Inf or NaN; FFMA then adds Rc to that +0 (so -0 under RM)."""
    a, b = inputs[:2]
    if a & 0x7f800000 and b & 0x7f800000: r = softfloat.arithmetic(op, inputs, 32, mode, True, sat, scale)
    elif op == "mul": return 0
    else: r = softfloat.arithmetic("add", [0, inputs[2]], 32, mode, True, sat)
    return GPU_NAN[32] if softfloat.unpack(r, 32)[0] == "nan" else r


def half2(op, inputs, **kw):
    """Packed F16 arithmetic on [low, high] lane pairs."""
    return sum(rounded(op, [x[lane] for x in inputs], 16, **kw) << (16 * lane) for lane in (0, 1))


def hmul2(a, b, mode=None, sat=False):
    """HMUL2 F16_V2 with FTZ or FMZ (None: neither). SM89 HMUL2.FMZ returns canonical +0 if either
    source is zero/subnormal, even for zero*Inf or signed-zero products."""
    out = 0
    for shift in (0, 16):
        x, y = (a >> shift) & 0xffff, (b >> shift) & 0xffff
        if mode == "FMZ" and (x & 0x7c00 == 0 or y & 0x7c00 == 0): continue
        out |= softfloat.arithmetic("mul", [x, y], 16, ftz=mode in ("FMZ", "FTZ"), sat=sat) << shift
    return out


def ordered(bits, width):
    """The encoding's value for fcompare: a float, or None for NaN."""
    x = f64(bits) if width == 64 else f32(bits)
    return None if math.isnan(x) else x


def fcompare(a, c, cmp):
    """FSETP/DSETP comparison of two numbers (None marks NaN); U suffix is true when unordered."""
    unordered = a is None or c is None
    match cmp:
        case "T": return True
        case "F": return False
        case "NUM": return not unordered
        case "NAN": return unordered
        case _ if unordered: return cmp.endswith("U")
        case _:
            return {"LT": a < c, "EQ": a == c, "LE": a <= c, "GT": a > c, "NE": a != c, "GE": a >= c}[cmp.removesuffix("U")]


def dsetp_compare(a, c, cmp, width=64):
    """DSETP's comparison of two encodings. MIN/MAX (SM89 GPU) order a before c (MIN: a <= c, MAX: a > c)
    by totalOrder, so -0 < +0 and subnormals count, but are true for any NaN c and false for a NaN a.
    Their Pv output is 1 only when both are NaN, whatever the combiner and Pp."""
    if cmp not in ("MIN", "MAX"): return fcompare(ordered(a, width), ordered(c, width), cmp)
    if ordered(a, width) is None: return False
    if ordered(c, width) is None: return True
    magnitude = (1 << width - 1) - 1
    key = lambda bits: ~(bits & magnitude) if bits >> width - 1 else bits
    return key(a) <= key(c) if cmp == "MIN" else key(a) > key(c)


BOPS = {"AND": lambda x, y: x and y, "OR": lambda x, y: x or y, "XOR": lambda x, y: x != y}


def fset(a, b, cmp, bop="AND", p=True, ftz=False):
    """FSET.BF: 1.0f where (a cmp b) bop p, else +0; FTZ flushes the inputs."""
    if ftz: a, b = ftz32(a), ftz32(b)
    return 0x3f800000 if BOPS[bop](fcompare(ordered(a, 32), ordered(b, 32), cmp), p) else 0


HALF_VIEWS, HALF_ONE = {"F16": f16, "BF16": bf16}, {"F16": 0x3c00, "BF16": 0x3f80}


def half_compares(a, b, cmp, ftz=False, fmt="F16"):
    """The comparison of each 16-bit lane of a with b's (low lane first); FTZ flushes F16 inputs."""
    def value(x):
        v = HALF_VIEWS[fmt](ftz16(x) if ftz else x)
        return None if math.isnan(v) else v
    return [fcompare(value(a >> s & 0xffff), value(b >> s & 0xffff), cmp) for s in (0, 16)]


def hset2(a, b, cmp, bop="AND", p=True, ftz=False, bf=True, fmt="F16"):
    """HSET2: per lane 1.0 in the lane format (.BF) or 0xffff (.BM) where (lane cmp) bop p, else 0."""
    true = HALF_ONE[fmt] if bf else 0xffff
    return sum((true if BOPS[bop](c, p) else 0) << s for c, s in zip(half_compares(a, b, cmp, ftz, fmt), (0, 16)))


def hsetp2(a, b, cmp, bop="AND", p=True, ftz=False, h_and=False, fmt="F16"):
    """HSETP2 (Pu, Pv): each lane's comparison bop p; .H_AND: both lanes' AND and its negation."""
    lo, hi = half_compares(a, b, cmp, ftz, fmt)
    if h_and: lo, hi = lo and hi, not (lo and hi)
    return BOPS[bop](lo, p), BOPS[bop](hi, p)


def fmnmx(x, y, is_min, nan_mode=False, ftz=False):
    """FMNMX: numeric operand wins over NaN, two NaNs give 0x7fffffff (always with .NAN),
    -0 < +0, optional FTZ of both inputs."""
    if ftz: x, y = ftz32(x), ftz32(y)
    if nan_mode and (nan32(x) or nan32(y)): return 0x7fffffff
    if nan32(x): return 0x7fffffff if nan32(y) else y
    if nan32(y): return x
    if f32(x) == f32(y) == 0: return (x | y) if is_min else (x & y)
    return x if (f32(x) < f32(y)) == is_min else y


def fmnmx_xorsign(x, y, is_min, nan_mode=False, ftz=False, absolute=False, encoded=None):
    """FMNMX.XORSIGN: compare signed operands, then replace the result sign with the XOR of the
    encoded signs, before |x| and -x (`encoded`, default x and y; SM89)."""
    ex, ey = encoded or (x, y)
    sign = (ex ^ ey) & 0x80000000
    if ftz: x, y = ftz32(x), ftz32(y)
    if absolute: x, y = x & 0x7fffffff, y & 0x7fffffff
    if nan_mode and (nan32(x) or nan32(y)): return 0x7fffffff
    if nan32(x): return 0x7fffffff if nan32(y) else (y & 0x7fffffff) | sign
    if nan32(y): return (x & 0x7fffffff) | sign
    chosen = fmnmx(x, y, is_min)
    return (chosen & 0x7fffffff) | sign


def fmnmx3_pick(x, y, is_min, exact):
    """One FMNMX3 step; the readable lowering is a plain ordered comparison."""
    if exact:
        if nan32(x): return x if nan32(y) else y
        if nan32(y): return x
        if f32(x) == f32(y) == 0: return (x | y) if is_min else (x & y)
    return y if (f32(x) > f32(y) if is_min else f32(x) < f32(y)) else x


def half_product(a, b, fa, fb, d):
    """PTX mul.d.a.b on F16/BF16 lanes: b converted (RN) to d's format, then a * b rounded RN to d,
    from exact values; None for a NaN result (payloads are not compared)."""
    from tests.refs import fpformat as fpf
    def to_d(kind, sign, value):
        if kind != "finite" or fb == d: return kind, sign, value
        code, over = fpf.round_magnitude(d, value, "RN", sign)
        return ("inf", sign, None) if over else fpf.decode(d, code)[:1] + (sign,) + fpf.decode(d, code)[2:]
    (ka, sa, va), (kb, sb, vb) = fpf.unpack(a, fa), to_d(*fpf.unpack(b, fb))
    sign = sa ^ sb
    if "nan" in (ka, kb) or "inf" in (ka, kb) and 0 in (va, vb): return None
    inf = fpf.encode(d, sign, (0x1f << 10) if d == "F16" else (0xff << 7))
    if "inf" in (ka, kb): return inf
    code, over = fpf.round_magnitude(d, va * vb, "RN", sign)
    return inf if over else fpf.encode(d, sign, code)


def half_minmax(a, b, is_min, nan_mode=False, ftz=False, xorsign=False, abs_a=None, abs_b=None, fmt="F16"):
    """HMNMX2 per 16-bit lane (F16, BF16 or E6M9); XORSIGN implies absolute inputs by default."""
    abs_a = xorsign if abs_a is None else abs_a
    abs_b = xorsign if abs_b is None else abs_b
    is_nan = {"F16": nan16, "BF16": nan_bf16, "E6M9": lambda v: v & 0x7e00 == 0x7e00 and v & 0x1ff != 0}[fmt]
    out = 0
    for shift in (0, 16):
        x, y = (a >> shift) & 0xffff, (b >> shift) & 0xffff
        sign = ((x ^ y) >> 15) & 1
        if abs_a: x &= 0x7fff
        if abs_b: y &= 0x7fff
        if ftz: x, y = ftz16(x), ftz16(y)
        xn, yn = is_nan(x), is_nan(y)
        if nan_mode and (xn or yn) or xn and yn: r = 0x7fff
        elif xn: r = y
        elif yn: r = x
        elif fmt == "E6M9":
            sx, sy = bool(x & 0x8000), bool(y & 0x8000)
            less = sx if sx != sy else ((x & 0x7fff) > (y & 0x7fff) if sx else (x & 0x7fff) < (y & 0x7fff))
            r = x if less == is_min else y
        else:
            view = bf16 if fmt == "BF16" else f16
            xf, yf = view(x), view(y)
            r = ((x | y) if is_min else (x & y)) if xf == yf == 0 else x if (xf < yf) == is_min else y
        if xorsign and not ((xn and yn) or (nan_mode and (xn or yn))): r = (r & 0x7fff) | (sign << 15)
        if ftz: r = ftz16(r)
        out |= r << shift
    return out


def f2i_trunc(bits):
    """F2I.TRUNC.NTZ F32 -> S32: NaN gives 0, out-of-range saturates."""
    kind, sign, value = softfloat.unpack(bits, 32)
    if kind == "nan": return 0
    if kind == "inf": return 0x80000000 if sign else 0x7fffffff
    magnitude = int(abs(value))
    if magnitude >= 1 << 31: return 0x80000000 if sign else 0x7fffffff
    return (-magnitude if sign else magnitude) & 0xffffffff


MANTISSA = {16: 10, 32: 23, 64: 52}


def flush(bits, w):
    """A denormal to zero of the same sign."""
    return bits & (1 << w - 1) if bits >> MANTISSA[w] & ((1 << w - 1 - MANTISSA[w]) - 1) == 0 else bits


def quiet(bits, src, dst):
    """F2F's NaN: sign kept, payload aligned at the top of the fraction, quiet bit set (SM89)."""
    ms, md = MANTISSA[src], MANTISSA[dst]
    payload = bits & ((1 << ms) - 1)
    payload = payload << md - ms if md > ms else payload >> ms - md
    return (bits >> src - 1) << dst - 1 | ((1 << dst - 1) - (1 << md)) | payload | 1 << md - 1


def frnd(bits, w, mode, ftz=False):
    """FRND: round to an integral value, keeping a zero's sign; NaN: F32 0x7fffffff, F64 quieted (SM89)."""
    if ftz: bits = flush(bits, w)
    kind, sign, value = softfloat.unpack(bits, w)
    if kind == "nan": return 0x7fffffff if w == 32 else quiet(bits, w, w)
    if kind == "inf": return bits
    return softfloat.pack(Fraction(softfloat.round_integer(value, mode)), w, "RN", zero_sign=sign)


def f2i(bits, w, n, signed, mode, ntz, ftz=False):
    """F2I: round by mode, saturate to n bits; NaN gives 0 (.NTZ) or the top bit (SM89, also unsigned)."""
    if ftz: bits = flush(bits, w)
    kind, sign, value = softfloat.unpack(bits, w)
    lo, hi = (-(1 << n - 1), (1 << n - 1) - 1) if signed else (0, (1 << n) - 1)
    if kind == "nan": return 0 if ntz else 1 << n - 1
    r = (lo if sign else hi) if kind == "inf" else max(lo, min(hi, softfloat.round_integer(value, mode)))
    return r & ((1 << n) - 1)


def i2f(x, n, signed, w, mode):
    """I2F: the n-bit integer x as a w-bit float, rounded once."""
    x &= (1 << n) - 1
    if signed and x >> n - 1: x -= 1 << n
    return softfloat.pack(Fraction(x), w, mode)


def f2f(bits, src, dst, mode):
    """F2F: rounded once; F16/F32 conversions canonicalize NaN, other widths quiet its payload (SM89)."""
    kind, sign, value = softfloat.unpack(bits, src)
    if kind == "nan": return (1 << (dst - 1)) - 1 if {src, dst} == {16, 32} else quiet(bits, src, dst)
    if kind == "inf": return sign << dst - 1 | ((1 << dst - 1) - (1 << MANTISSA[dst]))
    return softfloat.pack(value, dst, mode, sign)


def fchk_divide(a, b):
    """FCHK.DIVIDE: 1 when a / b needs the slow path, from the biased exponents only (SM89)."""
    ea, eb = a >> 23 & 255, b >> 23 & 255
    return int(ea <= 24 or ea == 255 or eb <= 1 or eb >= 252 or ea - eb >= 127 or eb - ea >= 125)


def f2ip_byte(bits, signed):
    """F2IP: round to nearest even, saturate to S8/U8; NaN gives 0."""
    x = f32(bits)
    if math.isnan(x): return 0
    lo, hi = (-128, 127) if signed else (0, 255)
    return (hi if x > 0 else lo) & 0xff if math.isinf(x) else max(lo, min(hi, round(x))) & 0xff


def widen_half(bits):
    """Exact F16 -> F32; None for NaN (payloads are not compared)."""
    kind, sign, value = softfloat.unpack(bits, 16)
    if kind == "nan": return None
    if kind == "inf": return (sign << 31) | 0x7f800000
    return softfloat.pack(value, 32, zero_sign=sign)


def underflow_band(op, inputs, width, scale=0):
    """True where Ghidra's (12.1.4, 12.2) FloatFormat.getEncoding rounds an RN result strictly between
    half and one minimum subnormal to zero (IEEE gives the minimum subnormal)."""
    parts = [softfloat.unpack(x, width) for x in inputs]
    if any(kind not in ("finite", "zero") for kind, _, _ in parts): return False
    v = [p[2] for p in parts]
    exact = abs(v[0] + v[1] if op == "add" else v[0] * v[1] * Fraction(2) ** scale + (v[2] if op == "fma" else 0))
    e, m = softfloat.FORMATS[width]
    tiny = Fraction(2) ** (2 - (1 << (e - 1)) - m)
    return tiny / 2 < exact < tiny


def conversion_underflow(bits, src, dst):
    """underflow_band for a conversion's RN narrowing (the readable lowering's float2float)."""
    kind, _, value = softfloat.unpack(bits, src)
    e, m = softfloat.FORMATS[dst]
    tiny = Fraction(2) ** (2 - (1 << (e - 1)) - m)
    return kind == "finite" and tiny / 2 < abs(value) < tiny


def same_halves(got, want, fmt="F16"):
    """Equal 16-bit lanes, treating any two F16 NaNs as equal (BF16 compares exactly)."""
    return all(g == w or fmt == "F16" and nan16(g) and nan16(w)
               for g, w in (((got >> s) & 0xffff, (want >> s) & 0xffff) for s in (0, 16)))


def readable(op, inputs, width, sat=False, scale=0):
    """The readable lowering's plain formula: unfused round-to-nearest without flushing.
    None where a NaN or the emulator's underflow band makes the comparison meaningless."""
    steps = [("mul", inputs[:2]), ("add", [None, inputs[2]])] if op == "fma" else [(op, inputs)]
    if scale: steps.append(("mul", [None, {32: 0x3f800000, 64: 0x3ff0000000000000}[width] + (scale << softfloat.FORMATS[width][1])]))
    value = None
    for i, (o, xs) in enumerate(steps):
        xs = [value if x is None else x for x in xs]
        if underflow_band(o, xs, width): return None
        value = softfloat.arithmetic(o, xs, width, sat=sat and i == len(steps) - 1)
        if softfloat.unpack(value, width)[0] == "nan": return None
    return value


def fmnmx_readable(x, y, is_min):
    """Readable FMNMX's documented ordered comparison; ties/unordered choose the second source for min."""
    return x if (f32(x) < f32(y)) == is_min else y


def source_modifiers(bits, width, absolute=False, negate=False):
    """Apply floating-source absolute value before negation, preserving payload and magnitude bits."""
    sign = 1 << (width - 1)
    if absolute: bits &= sign - 1
    return bits ^ sign if negate else bits


def conversion_source(bits, width, absolute=False, negate=False):
    """The source a conversion sees: a NaN ignores `|.|` and `-` and keeps the register's own sign (SM89 FRND.F64, F2F)."""
    return bits if softfloat.unpack(bits, width)[0] == "nan" else source_modifiers(bits, width, absolute, negate)
