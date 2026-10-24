"""Float to packed bytes: F2IP (two F32 to S8/U8 bytes). F2I is in src/sass/semantics/families/convert.py."""
from sass.semantics.bitvec import *
from sass.semantics.family import definition, family, outputs, unmodelled


@definition(lambda a: (f"cvt.rni.sat.{'s' if a['signed'] else 'u'}8.f32", ["bits"]))
def f32_to_8bit(bits, signed):
    """PTX cvt.rni.sat from F32 to S8/U8: rounded to nearest even and saturated (negative to 0 for
    U8); NaN gives 0."""
    sign, exponent, fraction = extract(bits, 31, 31) == 1, extract(bits, 30, 23), extract(bits, 22, 0)
    e = zext(exponent, 32) - 127
    significand = zext(fraction, 32) | 0x800000
    shift = 23 - e
    q = significand >> shift
    rest = significand & ((const(1, 32) << shift) - 1)
    half = const(1, 32) << (shift - 1)
    up = (half < rest) | ((rest == half) & ((q & 1) == 1))
    rounded = q + zext(up, 32)
    overflow = const(128 if signed else 255, 32)
    halfway = const(1, 32) if const(0x800000, 32) < significand else const(0, 32)   # e == -1: 0.5 rounds to 0
    big = sle(const(7 if signed else 8, 32), e)
    magnitude = (const(0, 32) if fraction != 0 else overflow) if exponent == 0xff else (
        const(0, 32) if (exponent == 0) | slt(e, -1) else (halfway if e == -1 else (overflow if big else rounded)))
    if signed:
        value = -magnitude if sign else (const(127, 32) if const(127, 32) < magnitude else magnitude)
        return extract(value, 7, 0)
    return const(0, 8) if sign else (const(255, 8) if const(255, 32) < magnitude else extract(magnitude, 7, 0))


@family('F2IP')
def f2ip(i):
    """Rd = C[15:0] << 16 | convert(Ra) << 8 | convert(Rb): two F32 values rounded to nearest even
    and saturated to S8/U8 (.ROUND.NTZ, no RELU)."""
    if i.select_fork('srcfmt') != 'F32' or i.select_fork('rnd') != 'ROUND' or i.select_fork('ntz') != 'NTZ' or i.select_fork('relu') != 'norelu': unmodelled('mode')
    fmt = str(i.select_fork('dstfmt'))
    if fmt not in ('S8', 'U8'): unmodelled(fmt)
    if i.width('Rd') != 32: unmodelled('width')
    signed = fmt == 'S8'
    a, b = f32_to_8bit(i.fvalue('Ra'), signed), f32_to_8bit(i.fvalue('Rb|Sb'), signed)
    return outputs(Rd=concat(extract(i.value('Rc|Sc', modifiers=False), 15, 0), a, b))
