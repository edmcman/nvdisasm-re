"""MUFU outputs measured on the SM89 GPU (research/mufu/survey.py; hardware observations) and independent SIN/COS reference.

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
    square = 0
    for i in range(17):
        if not local & (1 << i):
            continue
        if 2 * i >= 19:
            square += 1 << (2 * i - 19)
        for j in range(i):
            if local & (1 << j) and i + j + 1 >= 19:
                square += 1 << (i + j + 1 - 19)
    a, b, c = coefficients()[segment]
    value = Fraction((a << 12) + b * local + c * square, 1 << 37)
    return softfloat.pack(value, 32, 'RZ') | (int(negative) << 31)
