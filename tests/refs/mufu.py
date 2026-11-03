"""SM89 MUFU observations and independent SIN/COS and reciprocal references.

`samples()` gives each op's raw MUFU.<op> R11, R2 result for a fixed set of inputs (specials, powers of two and their
neighbours, values around 1, large arguments, random patterns); `survey()` the per-op histogram of the distance in ulps
to the correctly rounded result over every 32-bit input (ulps are measured on the high word for RCP64H/RSQ64H)."""
import json
import math
import struct
from fractions import Fraction
from functools import cache
from pathlib import Path
from sass.semantics.fp import softfloat


@cache
def samples():
    """(inputs, {op: outputs}) as lists of 32-bit words."""
    data = json.loads((Path(__file__).parent / "mufu_samples.json").read_text())
    words = lambda text: [int(text[i:i + 8], 16) for i in range(0, len(text), 8)]
    return words(data["inputs"]), {op: words(text) for op, text in data["outputs"].items()}


@cache
def survey():
    return json.loads((Path(__file__).parent / "mufu_survey.json").read_text())


@cache
def coefficients():
    """Hardware-recovered data, independent of the production coefficient copy."""
    path = Path(__file__).parents[2] / 'research/mufu/sin-fit/quantized-model.json'
    return json.loads(path.read_text())['coefficients']


def retained_square(local, width):
    cutoff = 2 * width - 15
    return sum(1 << (2 * i - cutoff) for i in range(width) if local & (1 << i) and 2 * i >= cutoff) + sum(
        1 << (i + j + 1 - cutoff) for i in range(width) for j in range(i)
        if local & (1 << i) and local & (1 << j) and i + j + 1 >= cutoff)


@cache
def sincos(bits, op, absolute=False, negate=False):
    """SM89 SIN/COS model: enumerate retained square terms, then exact rational RZ.

    The coefficient data and arithmetic are established in docs/hardware-facts.md
    (2026-10-19). This deliberately uses neither the implementation's bit-derived
    phase extraction nor its fast high/low square or float conversion.
    """
    if op not in ('SIN', 'COS'):
        raise ValueError(op)
    bits = ((bits & 0x7fffffff) if absolute else bits) ^ (0x80000000 if negate else 0)
    x = struct.unpack('<f', struct.pack('<I', bits & 0xffffffff))[0]
    if not math.isfinite(x):
        return 0x7fffffff
    turn = 1 << 25
    phase = int(Fraction(abs(x)) * turn) % turn
    if op == 'COS':
        phase = (phase + (1 << 23)) % turn
    quadrant, offset = divmod(phase, 1 << 23)
    if quadrant in (1, 3):
        offset = (1 << 23) - 1 - offset
    negative = (quadrant in (2, 3)) ^ (op == 'SIN' and bool(bits >> 31))
    segment, local = divmod(offset, 1 << 17)
    square = retained_square(local, 17)
    a, b, c = coefficients()[segment]
    value = Fraction((a << 12) + b * local + c * square, 1 << 37)
    return softfloat.pack(value, 32, 'RZ') | (int(negative) << 31)


def reciprocal_ideal(bits, op, fmt='F32'):
    match fmt:
        case 'F32': width, mantissa, exponent, flush = 32, 23, 8, True
        case '64H': width, mantissa, exponent, flush = 64, 20, 11, True
        case 'F16': width, mantissa, exponent, flush = 16, 10, 5, False
        case _: raise ValueError(fmt)
    sign = bits >> (31 if fmt == '64H' else width - 1)
    magnitude = bits & ((1 << (mantissa + exponent)) - 1)
    exp, fraction = divmod(magnitude, 1 << mantissa)
    infinity = ((1 << exponent) - 1) << mantissa
    nan = (1 << (mantissa + exponent)) - 1
    rsq = op.startswith('RSQ')
    if exp == (1 << exponent) - 1 and fraction: return nan
    if magnitude == 0 or flush and exp == 0: return infinity | sign << (mantissa + exponent)
    if rsq and sign: return nan
    if magnitude == infinity: return sign << (mantissa + exponent)
    _, _, value = softfloat.unpack(bits << 32 if fmt == '64H' else bits, width)
    value = abs(value)
    if rsq:
        # Rational bracketing avoids the production integer square-root algorithm.
        from decimal import Decimal, localcontext
        with localcontext() as context:
            context.prec = 100
            exact = 1 / (Decimal(value.numerator) / Decimal(value.denominator)).sqrt()
            result = Fraction(exact)
    else:
        result = 1 / value
    if sign: result = -result
    if fmt == 'F16' and abs(result) > 65504: return infinity | sign << 15
    rounded = softfloat.pack(result, width, 'RZ' if fmt == 'F16' else 'RN', ftz=flush)
    return rounded >> 32 if fmt == '64H' else rounded


@cache
def reciprocal_halves(op):
    import gzip
    path = Path(__file__).parents[2] / f'research/mufu/reciprocal/SM89-{op.lower()}-H0.json.gz'
    data = json.loads(gzip.decompress(path.read_bytes()))
    assert data['inputs'] == list(range(65536))
    return data['outputs']


@cache
def reciprocal_model(op, fmt, parity):
    path = Path(__file__).parents[2] / 'research/mufu/reciprocal/models.json'
    data = json.loads(path.read_text())
    finding = next(f for f in data['findings'] if
                   (f['op'], f['precision'], f['exponent_parity']) == (op[:3], fmt, parity))
    return next(m for m in finding['integer_models'] if m['coefficient_grid'] == [57, 42, 40] and
                m['final_rounding'] == ('nearest_even' if fmt == 'F32' else 'toward_zero'))


def reciprocal(bits, op, fmt='F32'):
    if fmt == 'F16': return reciprocal_halves(op)[bits & 0xffff]
    mantissa, exponent_bits, bias, width = (23, 8, 127, 32) if fmt == 'F32' else (20, 11, 1023, 64)
    magnitude, sign = bits & 0x7fffffff, bits >> 31
    exponent, fraction = divmod(magnitude, 1 << mantissa)
    rsq = op.startswith('RSQ')
    if exponent in (0, (1 << exponent_bits) - 1) or rsq and sign: return reciprocal_ideal(bits, op, fmt)
    parity = (exponent - bias) % 2 if rsq else 0
    if not fraction and not parity: return reciprocal_ideal(bits, op, fmt)
    model = reciprocal_model(op, fmt, parity)
    local_width = mantissa - (model['segments'].bit_length() - 1)
    segment, local = divmod(fraction, 1 << local_width)
    accumulator, constant, quadratic = model['coefficient_grid']
    a, b, c = model['coefficients'][segment]
    value = Fraction(a, 1 << constant) + Fraction(b * local, 1 << accumulator) + Fraction(
        c * retained_square(local, local_width), 1 << (quadratic + 15))
    scale = -(exponent - bias - parity) // 2 if rsq else -(exponent - bias)
    value *= Fraction(2) ** scale
    if sign: value = -value
    packed = softfloat.pack(value, width, 'RN' if fmt == 'F32' else 'RZ', ftz=True)
    return packed >> 32 if fmt == '64H' else packed


def reciprocal_same(got, want, op, fmt='F32', accuracy='bounded'):
    if accuracy == 'exact': return got == want
    match fmt:
        case 'F32': mantissa, exponent, bound = 23, 8, 2 if op == 'RSQ' else 1
        case '64H': mantissa, exponent, bound = 20, 11, 1
        case 'F16': mantissa, exponent, bound = 10, 5, 0
        case _: return False
    mask = (1 << (mantissa + exponent)) - 1
    special = lambda x: not (x & mask) or (x & mask) >= ((1 << exponent) - 1) << mantissa
    if special(got) or special(want): return got == want
    return got >> (mantissa + exponent) == want >> (mantissa + exponent) and abs(got - want) <= bound
