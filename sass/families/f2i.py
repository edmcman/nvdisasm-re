"""Float to integer conversions: F2I (F32 to S32 toward zero) and F2IP (two F32 to S8/U8 bytes)."""
from sass.sem import *
from sass.sem_family import family, helper, outputs, unmodelled
from sass.families.floating import readable


@helper
def f32_to_s32_trunc(bits):
    """F32 -> S32 toward zero, saturating; NaN gives 0 (SM89 GPU, F2I.TRUNC.NTZ)."""
    sign, exponent, fraction = extract(bits, 31, 31) == 1, extract(bits, 30, 23), extract(bits, 22, 0)
    e = zext(exponent, 32) - 127
    significand = zext(zext(fraction, 32) | 0x800000, 64)
    magnitude = (significand << zext(e - 23, 64)) if sle(const(23, 32), e) else (significand >> zext(23 - e, 64))
    limit = const(0x80000000, 32) if sign else const(0x7fffffff, 32)
    negative = const(0x80000000, 32) if const(0x80000000, 64) < magnitude else -trunc(magnitude, 32)
    positive = const(0x7fffffff, 32) if const(0x7fffffff, 64) < magnitude else trunc(magnitude, 32)
    finite = const(0, 32) if (exponent == 0) | slt(e, 0) else (limit if sle(const(31, 32), e) else (negative if sign else positive))
    special = const(0, 32) if fraction != 0 else limit
    return special if exponent == 0xff else finite


@family('F2I', fallback='prim')
def f2i(i):
    """Rd = Rb (F32) converted to S32 toward zero with saturation, NaN to 0 (.TRUNC.NTZ; the
    readable lowering takes any rounding mode as TRUNC). Other formats and modes are unmodelled."""
    if i.select_fork('srcfmt') != 'F32' or i.select_fork('dstfmt') != 'S32' or i.select_fork('ntz') != 'NTZ': unmodelled('format')
    rnd = i.select_fork('rnd')
    if rnd != 'TRUNC' and not readable(i): unmodelled(f'{rnd} rounding')
    if any(i.has_operand(n) for n in ('Pu', 'Pv')): unmodelled('predicate outputs')
    # FTZ cannot change the result: toward zero, every |x| < 1 (denormals included) gives 0.
    if i.has_operand('ftz') and i.select_fork('ftz') not in ('FTZ', 'noftz'): unmodelled('ftz')
    if i.width('Rd') != 32: unmodelled('width')
    return outputs(Rd=f32_to_s32_trunc(i.fvalue('Rb|Sb|Ra|Sa')))


@helper
def f32_to_8bit(bits, signed):
    """F32 -> S8/U8, round to nearest even, saturating (negative to 0 for U8); NaN gives 0."""
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
