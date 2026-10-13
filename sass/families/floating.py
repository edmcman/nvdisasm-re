"""Scalar floating-point families: FP32 (FADD/FMUL/FFMA/FSETP/FSEL/FMNMX/FMNMX3) and FP64
(DADD/DMUL/DFMA/DSETP).

The exact lowering computes what the hardware does: rounding modes, .FTZ (denormal inputs and
results to signed zero), .SAT (to [+0, 1], NaN to +0) and the min/max NaN and signed-zero rules.
The readable lowering is the plain formula: round to nearest, no flushing, min/max a comparison
(.SAT and the sign modifiers stay)."""
from sass.sem import *
from sass.sem_family import family, helper, outputs, unmodelled
from sass.families.integer import boolean

SCALES = {'noscale': 0, 'D2': -1, 'D4': -2, 'D8': -3, 'M2': 1, 'M4': 2, 'M8': 3}
SIGN_EXPONENT = {16: (0x8000, 0x7c00), 32: (0x80000000, 0x7f800000), 64: (1 << 63, 0x7ff0000000000000)}
ONE = {16: 0x3c00, 32: 0x3f800000, 64: 0x3ff0000000000000}


def widen_half(x): return fconv(x, 32)
CANONICAL_NAN = 0x7fffffff   # min/max of two NaNs (SM89 GPU)


def readable(i): return i.fp == 'readable'


def flushed(x):
    """A denormal to zero of the same sign, without a branch: x & (sign | (exponent != 0 ? ~sign : 0))."""
    sign, exponent = SIGN_EXPONENT[x.w]
    return x & (zext((x & exponent) != 0, x.w) * (sign - 1) | sign)


def flushes(i):
    """The class flushes denormals: .FTZ, in the exact lowering. .FMZ (FTZ plus 0 * x = 0) is
    unmodelled there; the readable lowering ignores both."""
    for name in ('fmz', 'ftz'):
        if i.has_operand(name):
            label = i.select_fork(name)
            if readable(i): return False
            if name == 'fmz' and label == 'FMZ': unmodelled('FMZ')
            return label == 'FTZ'
    return False


def float_inputs(i, *names):
    """(flushing, values): the operands with their float modifiers (|x|, -x), flushed under .FTZ."""
    ftz = flushes(i)
    return ftz, [flushed(x) if ftz else x for x in (i.fvalue(n) for n in names)]


def rounding(i):
    if not i.has_operand('rnd'): return 'RN'
    mode = i.select_fork('rnd')
    if mode not in ROUNDING: unmodelled(f'{mode} rounding')
    return 'RN' if readable(i) else str(mode)


def fused(i, a, b, c):
    """a * b + c: rounded once, or the unfused formula in the readable lowering."""
    rnd = rounding(i)   # asked either way: the readable lowering ignores it
    return fadd(fmul(a, b), c) if readable(i) else ffma(a, b, c, rnd)


@helper
def saturated(r):
    """.SAT: clamp to [+0, 1]; NaN and -0 give +0."""
    one, zero = const(ONE[r.w], r.w), const(0, r.w)
    return (r if fle(r, one) else one) if lnot(fnan(r)) & flt(zero, r) else zero


def result(i, ftz, r):
    if ftz: r = flushed(r)
    if i.has_operand('sat') and i.select_fork('sat') == 'SAT': r = saturated(r)
    if r.w != i.width('Rd'): unmodelled('width')
    return outputs(Rd=r)


def operand_width(i, w, *names):
    if any(i.width(n) != w for n in ('Rd', *names)): unmodelled('width')


@family('FADD', fallback='prim')
def fadd_(i):
    """Rd = Ra + Rc."""
    operand_width(i, 32, 'Ra|Sa', 'Rc|Sc')
    ftz, (a, c) = float_inputs(i, 'Ra|Sa', 'Rc|Sc')
    return result(i, ftz, fadd(a, c, rounding(i)))


@family('FMUL', fallback='prim')
def fmul_(i):
    """Rd = Ra * Rb * 2^scale (.D2-.D8, .M2-.M8), rounded once."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    ftz, (a, b) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    scale = SCALES.get(str(i.select_fork('scale'))) if i.has_operand('scale') else 0
    if scale is None: unmodelled('scale')
    rnd = rounding(i)
    if readable(i):   # the formula: the product, then the scaling multiply
        r = fmul(a, b)
        return result(i, ftz, fmul(r, const((127 + scale) << 23, 32)) if scale else r)
    return result(i, ftz, fmul(a, b, rnd, scale))


@family('FFMA', fallback='prim')
def ffma_(i):
    """Rd = Ra * Rb + Rc, rounded once."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    ftz, (a, b, c) = float_inputs(i, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    return result(i, ftz, fused(i, a, b, c))


@family('DADD')
def dadd_(i):
    """Rd = Ra + Rc (FP64)."""
    operand_width(i, 64, 'Ra|Sa', 'Rc|Sc')
    a, c = i.fvalue('Ra|Sa'), i.fvalue('Rc|Sc')
    return result(i, False, fadd(a, c, rounding(i)))


@family('DMUL')
def dmul_(i):
    """Rd = Ra * Rb (FP64)."""
    operand_width(i, 64, 'Ra|Sa', 'Rb|Sb')
    a, b = i.fvalue('Ra|Sa'), i.fvalue('Rb|Sb')
    return result(i, False, fmul(a, b, rounding(i)))


@family('DFMA')
def dfma_(i):
    """Rd = Ra * Rb + Rc (FP64), rounded once (via binary128: rare midpoint double rounding)."""
    operand_width(i, 64, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    a, b, c = i.fvalue('Ra|Sa'), i.fvalue('Rb|Sb'), i.fvalue('Rc|Sc')
    return result(i, False, fused(i, a, b, c))


@helper
def float_test(label, x, y):
    """The md comparison `label`: ordered tests are false with a NaN, ...U ones true; NEU is
    plain inequality, NUM/NAN test for NaN."""
    unordered = fnan(x) | fnan(y)
    ordered = {'LT': lambda: flt(x, y), 'EQ': lambda: feq(x, y), 'LE': lambda: fle(x, y),
               'GT': lambda: flt(y, x), 'GE': lambda: fle(y, x), 'NE': lambda: lnot(unordered) & lnot(feq(x, y))}
    match str(label):   # FSETP's fcomp and DSETP's test spell the same comparisons
        case 'T': return const(1, 1)
        case 'F': return const(0, 1)
        case 'NUM': return lnot(unordered)
        case 'NAN': return unordered
        case 'NEU': return lnot(feq(x, y))
        case 'LTU' | 'EQU' | 'LEU' | 'GTU' | 'GEU' as name: return unordered | ordered[name[:2]]()
        case 'LT' | 'EQ' | 'LE' | 'GT' | 'GE' | 'NE' as name: return ordered[name]()
    return None   # unmodelled


def comparison(i, names, x, y):
    """The comparison the selector chooses, as one value for both predicates."""
    return i.select_lookup(names, {label: float_test(label, x, y) for label in i.labels(names)})


def set_predicates(i, cond):
    """Pu = cond bop Pp, Pv = !cond bop Pp (or Pu = cond without a bop)."""
    if not i.has_operand('bop'): return outputs(Pu=cond)
    p, bop = i.pred('Pp'), i.select_fork('bop')
    return outputs(Pu=boolean(bop, cond, p), Pv=boolean(bop, lnot(cond), p))


@family('FSETP', fallback='prim')
def fsetp_(i):
    """Pu/Pv from comparing Ra with Rb (FTZ flushes the inputs)."""
    if i.width('Ra|Sa') != 32 or i.width('Rb|Sb') != 32: unmodelled('width')
    _, (x, y) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    return set_predicates(i, comparison(i, 'fcomp', x, y))


@family('DSETP')
def dsetp_(i):
    """Pu/Pv from comparing Ra with Rc (FP64)."""
    if i.width('Ra|Sa') != 64 or i.width('Rc|Sc') != 64: unmodelled('width')
    return set_predicates(i, comparison(i, 'test', i.fvalue('Ra|Sa'), i.fvalue('Rc|Sc')))


@family('FSEL')
def fsel_(i):
    """Rd = Pp ? Ra : Rb (FTZ flushes)."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    _, (a, b) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    return outputs(Rd=a if i.pred('Pp') else b)


@helper
def pick(p, x, y):
    """min(x, y) if p else max(x, y) by a float comparison (the second wins ties and NaNs)."""
    return x if flt(x, y) == p else y


@helper
def ordered_minmax(p, x, y):
    """min/max of two numbers: equal zeros give -0 for min, +0 for max (OR/AND of the encodings)."""
    zeros = ((x | y) & 0x7fffffff) == 0
    return ((x | y) if p else (x & y)) if zeros else pick(p, x, y)


@helper
def numeric_minmax(p, x, y):
    """PTX min/max: a number wins over NaN (two NaNs: the canonical NaN)."""
    chosen = ordered_minmax(p, x, y)
    return ((const(CANONICAL_NAN, 32) if fnan(y) else y) if fnan(x) else (x if fnan(y) else chosen))


@family('FMNMX')
def fmnmx_(i):
    """Rd = Pp ? min(Ra, Rb) : max(Ra, Rb). .NAN: a NaN input gives the canonical NaN. .XORSIGN:
    the magnitudes are compared and the result takes the XOR of the encoded signs (before |x|)."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    _, (a, b) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    p = i.pred('Pp')
    xorsign = i.has_operand('xorsign') and i.select_fork('xorsign') == 'XORSIGN'
    nan = i.has_operand('nan') and i.select_fork('nan') == 'NAN'
    if readable(i): return outputs(Rd=pick(p, a, b))
    if xorsign: a, b = a & 0x7fffffff, b & 0x7fffffff
    # .NAN: any NaN gives the canonical NaN, so only numbers reach the comparison.
    r = (const(CANONICAL_NAN, 32) if fnan(a) | fnan(b) else ordered_minmax(p, a, b)) if nan else numeric_minmax(p, a, b)
    if xorsign:
        sign = (i.value('Ra|Sa', modifiers=False) ^ i.value('Rb|Sb', modifiers=False)) & 0x80000000
        r = r if fnan(r) else (r & 0x7fffffff) | sign
    return outputs(Rd=r)


@helper
def pick_right(p, x, y):
    """FMNMX3's pairwise choice: y if it is smaller (Pp) or larger (!Pp), else x."""
    return y if (p & flt(y, x)) | (lnot(p) & flt(x, y)) else x


@helper
def numeric_pair3(p, x, y):
    zeros = ((x | y) & 0x7fffffff) == 0
    chosen = ((x | y) if p else (x & y)) if zeros else pick_right(p, x, y)
    return ((x if fnan(y) else y) if fnan(x) else (x if fnan(y) else chosen))


@family('FMNMX3')
def fmnmx3_(i):
    """Rd = the min (Pp) or max (!Pp) of Ra, Rb, Rc, pairwise; .NAN: any NaN gives the canonical NaN."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    _, (a, b, c) = float_inputs(i, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    p = i.pred('Pp')
    nan = i.has_operand('nan') and i.select_fork('nan') == 'NAN'
    if readable(i): return outputs(Rd=pick_right(p, pick_right(p, a, b), c))
    r = numeric_pair3(p, numeric_pair3(p, a, b), c)
    if nan: r = const(CANONICAL_NAN, 32) if fnan(a) | fnan(b) | fnan(c) else r
    return outputs(Rd=r)


# ---- packed binary16 (HADD2, HFMA2, HMUL2) ---------------------------------------------------------

HALF_SIGNS, HALF_ABS = 0x80008000, 0x7fff7fff


def half_lanes(i, names, swizzle):
    """(low, high) binary16 inputs: an immediate pair (the `...1` operand is the low lane), or a
    packed register after |x| / -x on both halves, its lanes picked by the swizzle selector."""
    name = i.resolve(names)
    if i.has_operand(name + '1'):
        return [trunc(i.value(n, modifiers=False), 16) for n in (name + '1', name)]
    v = i.value(name, modifiers=False)
    if v.w == 16: return [v, v]   # HADD2.F32's single binary16 immediate
    v = i.modified(name, 'absolute', v, lambda x: x & HALF_ABS)
    v = i.modified(name, 'negate', v, lambda x: x ^ HALF_SIGNS)
    return swizzled(i, swizzle, extract(v, 15, 0), extract(v, 31, 16))


def swizzled(i, swizzle, low, high):
    """(low, high) lanes picked by the swizzle selector (H1_H0, H0_H0, H1_H1): values chosen at
    decode time, not a branch of the family per swizzle."""
    if not i.has_operand(swizzle): return [low, high]
    return [i.select_lookup(swizzle, {'H1_H0': low, 'H0_H0': low, 'H1_H1': high}),
            i.select_lookup(swizzle, {'H1_H0': high, 'H0_H0': low, 'H1_H1': high})]


def half_swizzles(i, fma):
    b = 'iswzB' if i.has_operand('iswzB') else 'iswzC_as_B'
    c = 'iswzC' if i.has_operand('iswzC') else 'iswzB_as_C'
    return ['iswzA', b, c] if fma else ['iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C']


def half_saturating(i):
    name = 'satrelu' if i.has_operand('satrelu') else 'sat'
    if not i.has_operand(name): return False
    label = i.select_fork(name)
    if label not in ('nosat', 'SAT'): unmodelled(f'{label}')
    return label == 'SAT'


def no_flush(i):
    """HADD2/HFMA2: FTZ/FMZ are unmodelled in the exact lowering, ignored by the readable one."""
    for name in ('ftz', 'fmz'):
        if i.has_operand(name):
            label = i.select_fork(name)
            if not readable(i) and not str(label).startswith(('noftz', 'nofmz')): unmodelled(f'{label}')


@family('HADD2', 'HFMA2')
def half_arithmetic(i):
    """Rd = per lane a + c (HADD2) or a * b + c (HFMA2) on binary16, rounded once (the readable
    lowering: unfused), with .SAT. HADD2.F32 adds the low lanes, rounds to binary16 and widens the
    result to binary32 (FTZ flushes inputs and the binary16 result)."""
    fma = i.opcode == 'HFMA2'
    names = ['Ra|Sa', 'Rb|Sb', 'Rc|Sc'] if fma else ['Ra|Sa', 'Rc|Sc']
    if i.width('Rd') != 32: unmodelled('width')
    if not fma and i.cls.startswith('hadd2_F32'):
        if i.select_fork('ofmt') != 'F32': unmodelled('ofmt')
        ftz = i.select_fork('ftz') == 'FTZ' and not readable(i) if i.has_operand('ftz') else False
        a, c = (half_lanes(i, n, s)[0] for n, s in zip(names, ('iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C')))
        if ftz: a, c = flushed(a), flushed(c)
        r = fadd(a, c)
        if ftz: r = flushed(r)
        if half_saturating(i): r = saturated(r)
        return outputs(Rd=widen_half(r))
    if i.select_fork('ofmt') != 'F16_V2': unmodelled('ofmt')
    no_flush(i)
    sat = half_saturating(i)
    inputs = [half_lanes(i, n, s) for n, s in zip(names, half_swizzles(i, fma))]
    lanes = []
    for lane in (0, 1):
        x = [v[lane] for v in inputs]
        r = (fadd(fmul(x[0], x[1]), x[2]) if readable(i) else ffma(*x)) if fma else fadd(*x)
        lanes.append(saturated(r) if sat else r)
    return outputs(Rd=concat(lanes[1], lanes[0]))


@helper
def half_flushed_fmz(x):
    """.FMZ on an input: a denormal or zero becomes +0."""
    return x if (x & 0x7c00) != 0 else const(0, 16)


@family('HMUL2')
def hmul2(i):
    """Rd = per lane a * b on binary16, .SAT; .FTZ flushes inputs and results keeping the sign,
    .FMZ flushes inputs to +0 and makes the product +0 when either source is zero or denormal
    (even 0 * Inf)."""
    if i.width('Rd') != 32: unmodelled('width')
    if i.select_fork('ofmt') != 'F16_V2': unmodelled('ofmt')
    mode = str(i.select_fork('fmz')) if i.has_operand('fmz') else 'nofmz'
    if mode not in ('nofmz', 'nofmz_hfma2', 'FMZ', 'FTZ'): unmodelled(mode)
    if readable(i): mode = 'nofmz'
    sat = half_saturating(i)
    a, b = (half_lanes(i, n, s) for n, s in zip(('Ra|Sa', 'Rb|Sb'), ('iswzA', 'iswzB')))
    lanes = []
    for lane in (0, 1):
        x, y = a[lane], b[lane]
        fx, fy = {'FTZ': (flushed(x), flushed(y)), 'FMZ': (half_flushed_fmz(x), half_flushed_fmz(y))}.get(mode, (x, y))
        r = fmul(fx, fy)
        if sat: r = saturated(r)
        if mode in ('FTZ', 'FMZ'): r = flushed(r)
        if mode == 'FMZ': r = const(0, 16) if ((x & 0x7c00) == 0) | ((y & 0x7c00) == 0) else r
        lanes.append(r)
    return outputs(Rd=concat(lanes[1], lanes[0]))


# ---- HMNMX2: packed min/max on F16, BF16 or E6M9 lanes ----------------------------------------------

LANE_FORMATS = {'F16_V2': (0x7c00, 0x03ff), 'BF16_V2': (0x7f80, 0x007f), 'E6M9_V2': (0x7e00, 0x01ff)}
CANONICAL_HALF_NAN = 0x7fff


@helper
def lane_nan(x, fmt):
    exponent, mantissa = LANE_FORMATS[fmt]
    return ((x & exponent) == exponent) & ((x & mantissa) != 0)


@helper
def lane_less(x, y, fmt):
    """x < y for two non-NaN lanes: F16 via binary32, BF16 as the high half of a binary32, E6M9 by
    sign and magnitude."""
    match fmt:
        case 'F16_V2': return flt(fconv(x, 32), fconv(y, 32))
        case 'BF16_V2': return flt(concat(x, const(0, 16)), concat(y, const(0, 16)))
    mx, my = x & 0x7fff, y & 0x7fff
    sign_x = extract(x, 15, 15) == 1
    return (sign_x if ((x ^ y) & 0x8000) != 0 else ((my < mx) if sign_x else (mx < my)))


@helper
def hmnmx2_lane(i, p, a, c, ar, cr, ftz, nan, xorsign):
    """(result, A chosen) of one lane: a, c with modifiers, ar, cr as encoded (NaN tests, XORSIGN).
    The lane format's NaN test and order are values chosen by ofmt, not a branch per format."""
    formats = [f for f in LANE_FORMATS if f in i.labels('ofmt')]
    by_format = lambda f: i.select_lookup('ofmt', {fmt: f(fmt) for fmt in formats})
    nan_a, nan_c = by_format(lambda fmt: lane_nan(ar, fmt)), by_format(lambda fmt: lane_nan(cr, fmt))
    if ftz: a, c = flushed(a), flushed(c)
    zeros = ((a | c) & 0x7fff) == 0
    zero_pick = (a | c) if p else (a & c)
    take_a = by_format(lambda fmt: lane_less(a, c, fmt)) == p
    ordered = (zero_pick if zeros else (a if take_a else c))
    chose_a = ((a == zero_pick) if zeros else take_a)
    both = nan_a & nan_c
    canonical = const(CANONICAL_HALF_NAN, 16)
    if nan: r = canonical if nan_a | nan_c else ordered
    else: r = canonical if both else (c if nan_a else (a if nan_c else ordered))
    chose_a = const(1, 1) if both else (const(0, 1) if nan_a else (const(1, 1) if nan_c else chose_a))
    if xorsign:
        keep = (both | (nan_a | nan_c)) if nan else both
        r = r if keep else (r & 0x7fff) | ((ar ^ cr) & 0x8000)
    if ftz: r = flushed(r)
    return r, chose_a


@family('HMNMX2')
def hmnmx2(i):
    """Rd = per lane Pp ? min : max of Ra, Rb on F16/BF16/E6M9 lanes (.NAN: a NaN gives the
    canonical NaN, else a number wins over NaN; equal zeros -0 for min, +0 for max; .XORSIGN: the
    XOR of the encoded signs; .FTZ (F16) flushes inputs and result). With .IS_A, Pu/Pv say which
    lane took A."""
    if i.width('Rd') != 32: unmodelled('width')
    ftz = i.select_fork('ftz') == 'FTZ'
    if ftz and i.select_fork('ofmt') != 'F16_V2': unmodelled('FTZ of a non-F16 format')   # md-disallowed
    nan = i.select_fork('nan') == 'NAN'
    xorsign = i.has_operand('xorsign') and i.select_fork('xorsign') == 'XORSIGN'
    predicates = i.has_operand('Pu')
    if predicates and (not i.has_operand('isA') or i.select_fork('isA') != 'IS_A' or nan): unmodelled('source predicates')
    names, swizzles = ('Ra|Sa', 'Rb|Sb'), ('iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C')
    if xorsign:
        absolute = [n for n in names if i.has_operand(n) and (i.resolve(n), 'absolute') in i._encoded]
        if len(absolute) == 2 and not all(i.operand_absolute(n) for n in absolute): unmodelled('XORSIGN without both |x|')
    lanes = [half_lanes(i, n, s) for n, s in zip(names, swizzles)]
    raws = [raw_half_lanes(i, n, s) for n, s in zip(names, swizzles)]
    p = i.pred('Pp')
    # No readable variant, as before: FTZ and the NaN rules apply in both lowerings.
    results = [hmnmx2_lane(i, p, lanes[0][k], lanes[1][k], raws[0][k], raws[1][k], ftz, nan, xorsign) for k in (0, 1)]
    writes = dict(Rd=concat(results[1][0], results[0][0]))
    if predicates: writes.update(Pu=results[0][1], Pv=results[1][1])
    return outputs(**writes)


def raw_half_lanes(i, names, swizzle):
    """half_lanes without the |x| / -x modifiers (NaN tests and XORSIGN read the encoded signs)."""
    name = i.resolve(names)
    if i.has_operand(name + '1'): return [trunc(i.value(n, modifiers=False), 16) for n in (name + '1', name)]
    v = i.value(name, modifiers=False)
    if v.w == 16: return [v, v]
    return swizzled(i, swizzle, extract(v, 15, 0), extract(v, 31, 16))
