"""Floating-point instruction references: min/max rules, conversions, comparisons, and the
known Ghidra emulator underflow band."""
import math
from fractions import Fraction
from sass import softfloat
from tests.refs.bits import bf16, f16, f32, ftz16, ftz32, nan16, nan32, nan_bf16

FCOMPARISONS = ("LT", "EQ", "LE", "GT", "NE", "GE", "LTU", "EQU", "LEU", "GTU", "NEU", "GEU", "NUM", "NAN")


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


def fmnmx(x, y, is_min, nan_mode=False, ftz=False):
    """FMNMX: numeric operand wins over NaN, two NaNs give 0x7fffffff (always with .NAN),
    -0 < +0, optional FTZ of both inputs."""
    if ftz: x, y = ftz32(x), ftz32(y)
    if nan_mode and (nan32(x) or nan32(y)): return 0x7fffffff
    if nan32(x): return 0x7fffffff if nan32(y) else y
    if nan32(y): return x
    if f32(x) == f32(y) == 0: return (x | y) if is_min else (x & y)
    return x if (f32(x) < f32(y)) == is_min else y


def fmnmx_xorsign(x, y, is_min, nan_mode=False, ftz=False):
    """min/max.xorsign.abs: select on magnitudes, then the XOR of the source signs unless NaN."""
    if ftz: x, y = ftz32(x), ftz32(y)
    sign = (x ^ y) & 0x80000000
    if nan_mode and (nan32(x) or nan32(y)): return 0x7fffffff
    if nan32(x): return 0x7fffffff if nan32(y) else (y & 0x7fffffff) | sign
    if nan32(y): return (x & 0x7fffffff) | sign
    ax, ay = x & 0x7fffffff, y & 0x7fffffff
    return (ax if (f32(ax) < f32(ay)) == is_min else ay) | sign


def fmnmx3_pick(x, y, is_min, exact):
    """One FMNMX3 step; the readable lowering is a plain ordered comparison."""
    if exact:
        if nan32(x): return x if nan32(y) else y
        if nan32(y): return x
        if f32(x) == f32(y) == 0: return (x | y) if is_min else (x & y)
    return y if (f32(x) > f32(y) if is_min else f32(x) < f32(y)) else x


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
    """True where Ghidra 12.1.4's FloatFormat.getEncoding rounds an RN result strictly between
    half and one minimum subnormal to zero (IEEE gives the minimum subnormal)."""
    parts = [softfloat.unpack(x, width) for x in inputs]
    if any(kind not in ("finite", "zero") for kind, _, _ in parts): return False
    v = [p[2] for p in parts]
    exact = abs(v[0] + v[1] if op == "add" else v[0] * v[1] * Fraction(2) ** scale + (v[2] if op == "fma" else 0))
    e, m = softfloat.FORMATS[width]
    tiny = Fraction(2) ** (2 - (1 << (e - 1)) - m)
    return tiny / 2 < exact < tiny


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
