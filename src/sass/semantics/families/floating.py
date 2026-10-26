"""Scalar floating-point families: FP32 (FADD/FMUL/FFMA/FSETP/FSET/FSEL/FMNMX/FMNMX3/FCHK) and FP64
(DADD/DMUL/DFMA/DSETP).

The exact lowering computes what the hardware does: rounding modes, .FTZ (denormal inputs and
results to signed zero), .SAT (to [+0, 1], NaN to +0) and the min/max NaN and signed-zero rules.
The readable lowering is the plain formula: round to nearest, no flushing, min/max a comparison
(.SAT and the sign modifiers stay)."""
from sass.semantics.bitvec import *
from sass.semantics.fp import fpformat
from sass.semantics.family import definition, family, float_abs, float_neg, helper, outputs, unmodelled
from sass.semantics.families.integer import boolean

SCALES = {'noscale': 0, 'D2': -1, 'D4': -2, 'D8': -3, 'M2': 1, 'M4': 2, 'M8': 3}
SIGN_EXPONENT = {16: (0x8000, 0x7c00), 32: (0x80000000, 0x7f800000), 64: (1 << 63, 0x7ff0000000000000)}
ONE = {16: 0x3c00, 32: 0x3f800000, 64: 0x3ff0000000000000}


def widen_half(x): return fconv(x, 32)
CANONICAL_NAN = 0x7fffffff   # min/max of two NaNs (SM89 GPU)


def readable(i): return i.fp == 'readable'


IEEE = {16: 'F16', 32: 'F32', 64: 'F64'}


@definition(lambda a: ("ftz" if a["fmt"] is None else f"ftz.{a['fmt'].lower()}", ["x"]))
def flushed(x, fmt=None):
    """Flush to zero: a denormal becomes zero of the same sign, other values are unchanged.

    fmt: x's format when it isn't the IEEE one of its width (BF16). Without a branch:
    x & (sign | (exponent != 0 ? ~sign : 0))."""
    f = fpformat.FORMATS[fmt or IEEE[x.w]]
    sign, exponent = 1 << f.width - 1, ((1 << f.ebits) - 1) << f.mbits
    return x & (zext((x & exponent) != 0, x.w) * (sign - 1) | sign)


def flushes(i):
    """The class flushes denormals: .FTZ, in the exact lowering. .FMZ (FTZ plus 0 * x = 0) is
    unmodelled there, except in FMUL/FFMA (product_inputs); the readable lowering ignores both."""
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


def fused(i, a, b, c, nans=()):
    """a * b + c: rounded once, or the unfused formula in the readable lowering."""
    rnd = rounding(i)   # asked either way: the readable lowering ignores it
    return fadd(fmul(a, b), c) if readable(i) else ffma(a, b, c, rnd, nans)


@definition("sat")
def saturated(r):
    """Saturate: clamp to [+0, 1]; NaN and -0 give +0."""
    one, zero = const(ONE[r.w], r.w), const(0, r.w)
    return (r if fle(r, one) else one) if lnot(fnan(r)) & flt(zero, r) else zero


def result(i, ftz, r):
    if ftz: r = flushed(r)
    if i.has_operand('sat') and i.select_fork('sat') == 'SAT': r = saturated(r)
    if r.w != i.width('Rd'): unmodelled('width')
    return outputs(Rd=r)


def operand_width(i, w, *names):
    if any(i.width(n) != w for n in ('Rd', *names)): unmodelled('width')


@family('FADD')
def fadd_(i):
    """Rd = Ra + Rc."""
    operand_width(i, 32, 'Ra|Sa', 'Rc|Sc')
    ftz, (a, c) = float_inputs(i, 'Ra|Sa', 'Rc|Sc')
    return result(i, ftz, fadd(a, c, rounding(i)))


@helper
def zero_factor(a, b):
    """.FMZ: a factor with a zero exponent (zero or denormal) makes the product +0, even against
    Inf or NaN (SM89 GPU)."""
    exponent = SIGN_EXPONENT[32][1]
    return ((a & exponent) == 0) | ((b & exponent) == 0)


@helper
def zeroed(zero, x): return const(0, 32) if zero else x


def product_inputs(i, *names):
    """(flushing, values): the operands with their modifiers, flushed under .FTZ or .FMZ. .FMZ also
    makes both factors +0 when one has a zero exponent, so the product is +0 and FFMA adds Rc to
    it; a decode-time choice, so .FTZ and .FMZ share one body (a third made SM90 exceed 16 MiB)."""
    if readable(i) or not i.has_operand('fmz'): return float_inputs(i, *names)
    if not i.select_in('fmz', ('FTZ', 'FMZ')): return False, [i.fvalue(n) for n in names]
    values = [flushed(i.fvalue(n)) for n in names]
    # Flushing keeps the exponent, so the unflushed factors are tested: each factor's value then
    # doesn't recompute the other's flush (their modifier tables are shared already).
    zero = i.select_lookup_const('fmz', {'FTZ': 0, 'FMZ': 1}, 1) & zero_factor(*(i.fvalue(n) for n in names[:2]))
    values[:2] = [zeroed(zero, x) for x in values[:2]]
    return True, values


@family('FMUL')
def fmul_(i):
    """Rd = Ra * Rb * 2^scale (.D2-.D8, .M2-.M8), rounded once."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    ftz, (a, b) = product_inputs(i, 'Ra|Sa', 'Rb|Sb')
    scale = SCALES.get(str(i.select_fork('scale'))) if i.has_operand('scale') else 0
    if scale is None: unmodelled('scale')
    rnd = rounding(i)
    if readable(i):   # the formula: the product, then the scaling multiply
        r = fmul(a, b)
        return result(i, ftz, fmul(r, const((127 + scale) << 23, 32)) if scale else r)
    return result(i, ftz, fmul(a, b, rnd, scale))


@family('FFMA')
def ffma_(i):
    """Rd = Ra * Rb + Rc, rounded once."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    ftz, (a, b, c) = product_inputs(i, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    return result(i, ftz, fused(i, a, b, c))


def double_inputs(i, *names):
    """(values with -/|.|, registers without, or () when none is modified): a NaN result propagates
    the unmodified register (SM89 GPU), which is then the value itself."""
    operand_width(i, 64, *names)
    values, registers = [i.fvalue(n) for n in names], [i.fvalue(n, modifiers=False) for n in names]
    return values, () if all(map(lambda v, r: v is r, values, registers)) else tuple(registers)


def f64_name(kind, sources):
    """A namer: <kind>.<mode>.f64 of the operands, then the registers before -/|.| (the NaN sources)
    when an operand is modified."""
    return lambda a: (f"{kind}.{a['rnd'].lower()}.f64", [*sources, *(["nans"] if a["nans"] else [])])


@definition(f64_name("add", ["a", "c"]))
def add_f64(a, c, rnd, nans=()):
    """PTX add.f64 with the GPU's NaN: rounded once; a NaN result is the first NaN of the registers
    before -/|.| (c, then a), quieted, else 0xfff8000000000000.

    nans: those registers, when an operand is modified (else the operands)."""
    return fadd(a, c, rnd, nans)


@definition(f64_name("mul", ["a", "b"]))
def mul_f64(a, b, rnd, nans=()):
    """PTX mul.f64 with the GPU's NaN: rounded once; a NaN result is the first NaN of the registers
    before -/|.| (b, then a), quieted, else 0xfff8000000000000.

    nans: those registers, when an operand is modified (else the operands)."""
    return fmul(a, b, rnd, nans=nans)


@definition(f64_name("fma", ["a", "b", "c"]))
def fma_f64(a, b, c, rnd, nans=()):
    """PTX fma.f64 with the GPU's NaN: a * b + c rounded once (via binary128: rare midpoint double
    rounding); a NaN result is the first NaN of the registers before -/|.| (b, c, then a), quieted,
    else 0xfff8000000000000.

    nans: those registers, when an operand is modified (else the operands)."""
    return ffma(a, b, c, rnd, nans)


@family('DADD')
def dadd_(i):
    """Rd = Ra + Rc (FP64)."""
    (a, c), nans = double_inputs(i, 'Ra|Sa', 'Rc|Sc')
    return result(i, False, add_f64(a, c, rounding(i), nans))


@family('DMUL')
def dmul_(i):
    """Rd = Ra * Rb (FP64)."""
    (a, b), nans = double_inputs(i, 'Ra|Sa', 'Rb|Sb')
    return result(i, False, mul_f64(a, b, rounding(i), nans))


@family('DFMA')
def dfma_(i):
    """Rd = Ra * Rb + Rc (FP64), rounded once (via binary128: rare midpoint double rounding)."""
    (a, b, c), nans = double_inputs(i, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    rnd = rounding(i)
    return result(i, False, fadd(fmul(a, b), c) if readable(i) else fma_f64(a, b, c, rnd, nans))


@helper
def minmax_order(label, x, y):
    """DSETP.MIN/MAX's test (SM89 GPU): x is totalOrder-below-or-equal (MIN) or above (MAX) y, true
    for a NaN y and false for a NaN x."""
    key = lambda v: ite(extract(v, v.w - 1, v.w - 1) != 0, ~v, v | const(1 << v.w - 1, v.w))
    order = key(x) <= key(y) if label == 'MIN' else key(y) < key(x)
    return lnot(fnan(x)) & (fnan(y) | order)


FLOAT_TESTS = frozenset('F LT EQ LE GT NE GE NUM NAN LTU EQU LEU GTU NEU GEU T MIN MAX'.split())


@definition(lambda a: (f"setp.{a['label'].lower()}.f{a['x'].w}", ["x", "y"]))
def float_test(label, x, y):
    """The md comparison `label`: ordered tests are false with a NaN, ...U ones true; NEU is
    plain inequality, NUM/NAN test for NaN, T/F are constant true/false. MIN/MAX use DSETP's
    total-order test, true for a NaN y and false for a NaN x."""
    unordered = fnan(x) | fnan(y)
    ordered = {'LT': lambda: flt(x, y), 'EQ': lambda: feq(x, y), 'LE': lambda: fle(x, y),
               'GT': lambda: flt(y, x), 'GE': lambda: fle(y, x), 'NE': lambda: lnot(unordered) & lnot(feq(x, y))}
    match str(label):   # FSETP's fcomp and DSETP's test spell the same comparisons
        case 'T': return const(1, 1)
        case 'F': return const(0, 1)
        case 'MIN' | 'MAX' as name: return minmax_order(name, x, y)
        case 'NUM': return lnot(unordered)
        case 'NAN': return unordered
        case 'NEU': return lnot(feq(x, y))
        case 'LTU' | 'EQU' | 'LEU' | 'GTU' | 'GEU' as name: return unordered | ordered[name[:2]]()
        case 'LT' | 'EQ' | 'LE' | 'GT' | 'GE' | 'NE' as name: return ordered[name]()


def comparison(i, names, x, y):
    """The comparison the selector chooses, as one value for both predicates."""
    return i.select_lookup(names, {label: float_test(label, x, y) for label in i.labels(names) if label in FLOAT_TESTS})


def combined(i, cond):
    """cond bop Pp, or cond itself without a bop."""
    return boolean(i.select_fork('bop'), cond, i.pred('Pp')) if i.has_operand('bop') else cond


def set_predicates(i, cond, minmax=None):
    """Pu = cond bop Pp, Pv = !cond bop Pp (or Pu = cond without a bop); under `minmax` Pv is the
    uncombined both-inputs-NaN flag."""
    if not i.has_operand('bop'): return outputs(Pu=cond)
    pv = combined(i, lnot(cond))
    return outputs(Pu=combined(i, cond), Pv=pv if minmax is None else ite(minmax[0], minmax[1], pv))


@family('FSETP')
def fsetp_(i):
    """Pu/Pv from comparing Ra with Rb (FTZ flushes the inputs)."""
    if i.width('Ra|Sa') != 32 or i.width('Rb|Sb') != 32: unmodelled('width')
    _, (x, y) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    return set_predicates(i, comparison(i, 'fcomp', x, y))


@family('FSET')
def fset_(i):
    """Rd = 1.0 if (Ra compared with Rb) bop Pp, else +0 (.BF, the only form; FTZ flushes the inputs)."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    i.select_fork('bf')
    _, (x, y) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    return outputs(Rd=const(ONE[32], 32) if combined(i, comparison(i, 'fcomp', x, y)) else const(0, 32))


@family('DSETP')
def dsetp_(i):
    """Pu/Pv from comparing Ra with Rc (FP64)."""
    if i.width('Ra|Sa') != 64 or i.width('Rc|Sc') != 64: unmodelled('width')
    x, y = i.fvalue('Ra|Sa'), i.fvalue('Rc|Sc')
    minmax = i.select_lookup('test', {l: const(int(l in ('MIN', 'MAX')), 1) for l in i.labels('test')})
    return set_predicates(i, comparison(i, 'test', x, y), (minmax, fnan(x) & fnan(y)))


@definition("fchk.divide")
def division_check(a, b):
    """Whether a / b needs the division slow path: from the biased exponents alone (signs, modifiers
    and mantissas don't matter), a zero/denormal/tiny (ea <= 24) or Inf/NaN, b's reciprocal out of
    range (eb <= 1 or eb >= 252), or the quotient's exponent out of range (SM89 GPU)."""
    ea, eb = (zext(extract(x, 30, 23), 32) for x in (a, b))
    return (ea <= 24) | (ea == 255) | (eb <= 1) | (eb >= 252) | sle(const(127, 32), ea - eb) | sle(const(125, 32), eb - ea)


@family('FCHK')
def fchk(i):
    """Pu = Ra / Rb needs the division slow path: from the biased exponents alone (signs, modifiers
    and mantissas don't matter), Ra zero/denormal/tiny (ea <= 24) or Inf/NaN, Rb's reciprocal out of
    range (eb <= 1 or eb >= 252), or the quotient's exponent out of range (SM89 GPU: exact on 1.1M
    random and boundary pairs)."""
    if i.select_fork('mode') != 'DIVIDE': unmodelled('mode')
    return outputs(Pu=division_check(i.fvalue('Ra'), i.fvalue('Rb|Sb')))


@family('FSEL', 'UFSEL')
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


def minmax_name(kind, sources):
    """A namer: min<kind>[.NaN].f32 or max<kind>[.NaN].f32 for a constant predicate (PT, !PT),
    else fmnmx<kind>[.NaN] of the predicate too."""
    def name(a):
        nan, p = ".NaN" if a["nan"] else "", a["p"]
        if is_const(p): return f"{'min' if value(p) else 'max'}{kind}{nan}.f32", sources
        return f"fmnmx{kind}{nan}", ["p", *sources]
    return name


@definition(minmax_name("", ["x", "y"]))
def fmnmx(p, x, y, nan=False):
    """PTX min.f32 when the predicate holds, else max.f32, with the GPU's NaN: a number wins over
    NaN, two NaNs give 0x7fffffff, and -0 is below +0; .NaN: any NaN gives 0x7fffffff."""
    # .NAN: any NaN gives the canonical NaN, so only numbers reach the comparison.
    return (const(CANONICAL_NAN, 32) if fnan(x) | fnan(y) else ordered_minmax(p, x, y)) if nan else numeric_minmax(p, x, y)


def xorsign_name(a):
    """A namer: min/max[.NaN].xorsign[.abs].f32 for a constant predicate, else fmnmx[.NaN].xorsign[.abs]
    of it too; .abs when the operands are the registers' magnitudes (then not arguments)."""
    nan, absolute, p = ".NaN" if a["nan"] else "", a["x"] is None, a["p"]
    head = ("min" if value(p) else "max") if is_const(p) else "fmnmx"
    name = f"{head}{nan}.xorsign{'.abs' if absolute else ''}" + (".f32" if is_const(p) else "")
    return name, [*(["p"] if not is_const(p) else []), *([] if absolute else ["x", "y"]), "rx", "ry"]


@definition(xorsign_name)
def fmnmx_xorsign(p, rx, ry, x=None, y=None, nan=False):
    """PTX min/max.xorsign.abs.f32 with the GPU's NaN: fmnmx of the operands, the result taking the
    XOR of the registers' encoded signs unless it is a NaN.

    x, y: the operands, when not the registers' magnitudes."""
    x, y = (float_abs(rx), float_abs(ry)) if x is None else (x, y)
    r = fmnmx(p, x, y, nan)
    # The result is a NaN when an input is (.NAN) or both are (a number wins otherwise): asked of
    # the inputs, not of r, so testing it doesn't repeat the comparison.
    is_nan = (fnan(x) | fnan(y)) if nan else (fnan(x) & fnan(y))
    return r if is_nan else (r & 0x7fffffff) | ((rx ^ ry) & 0x80000000)


@definition(lambda a: ((f"{'min' if value(a['p']) else 'max'}.readable.f32", ["x", "y"]) if is_const(a["p"]) else ("fmnmx.readable", ["p", "x", "y"])))
def readable_minmax(p, x, y):
    """The readable lowering's FMNMX: min (predicate) or max by a float comparison alone, the second
    operand winning ties and NaNs (no NaN or signed-zero rules)."""
    return pick(p, x, y)


@family('FMNMX')
def fmnmx_(i):
    """Rd = Pp ? min(Ra, Rb) : max(Ra, Rb). .NAN: a NaN input gives the canonical NaN. .XORSIGN:
    the modified operands are compared and the result takes the XOR of the encoded signs (before |x|)."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb')
    _, (a, b) = float_inputs(i, 'Ra|Sa', 'Rb|Sb')
    p = i.pred('Pp')
    xorsign = i.has_operand('xorsign') and i.select_fork('xorsign') == 'XORSIGN'
    nan = i.has_operand('nan') and i.select_fork('nan') == 'NAN'
    if readable(i): return outputs(Rd=readable_minmax(p, a, b))
    if not xorsign: return outputs(Rd=fmnmx(p, a, b, nan))
    ra, rb = i.value('Ra|Sa', modifiers=False), i.value('Rb|Sb', modifiers=False)
    absolute = a is float_abs(ra) and b is float_abs(rb)
    return outputs(Rd=fmnmx_xorsign(p, ra, rb, *((None, None) if absolute else (a, b)), nan))


@helper
def pick_right(p, x, y):
    """FMNMX3's pairwise choice: y if it is smaller (Pp) or larger (!Pp), else x."""
    return y if (p & flt(y, x)) | (lnot(p) & flt(x, y)) else x


@helper
def numeric_pair3(p, x, y):
    zeros = ((x | y) & 0x7fffffff) == 0
    chosen = ((x | y) if p else (x & y)) if zeros else pick_right(p, x, y)
    return ((x if fnan(y) else y) if fnan(x) else (x if fnan(y) else chosen))


@definition(minmax_name("3", ["x", "y", "z"]))
def fmnmx3(p, x, y, z, nan=False):
    """The min (predicate) or max of three F32 values, pairwise: a number wins over NaN, two NaNs
    give the first, -0 is below +0; .NaN: any NaN gives 0x7fffffff."""
    r = numeric_pair3(p, numeric_pair3(p, x, y), z)
    if nan: r = const(CANONICAL_NAN, 32) if fnan(x) | fnan(y) | fnan(z) else r
    return r


@definition(lambda a: ((f"{'min' if value(a['p']) else 'max'}3.readable.f32", ["x", "y", "z"]) if is_const(a["p"]) else ("fmnmx3.readable", ["p", "x", "y", "z"])))
def readable_minmax3(p, x, y, z):
    """The readable lowering's FMNMX3: min (predicate) or max of three by float comparisons alone,
    pairwise, the first operand winning ties and NaNs (no NaN or signed-zero rules)."""
    return pick_right(p, pick_right(p, x, y), z)


@family('FMNMX3')
def fmnmx3_(i):
    """Rd = the min (Pp) or max (!Pp) of Ra, Rb, Rc, pairwise; .NAN: any NaN gives the canonical NaN."""
    operand_width(i, 32, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    _, (a, b, c) = float_inputs(i, 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')
    p = i.pred('Pp')
    nan = i.has_operand('nan') and i.select_fork('nan') == 'NAN'
    if readable(i): return outputs(Rd=readable_minmax3(p, a, b, c))
    return outputs(Rd=fmnmx3(p, a, b, c, nan))


# SM120's uniform twins; their unmodelled encodings are opaque (the runtime primitives are scalar only).
for uniform, scalar in (('UFADD', fadd_), ('UFMUL', fmul_), ('UFFMA', ffma_), ('UFSETP', fsetp_)): family(uniform)(scalar.__wrapped__)


# ---- packed binary16 (HADD2, HFMA2, HMUL2, HSET2, HSETP2) -----------------------------------------

HALF_SIGNS, HALF_ABS = 0x80008000, 0x7fff7fff


def half_lanes(i, names, swizzle):
    """(low, high) binary16 inputs: an immediate pair (the `...1` operand is the low lane), or a
    packed register after |x| / -x on both halves, its lanes picked by the swizzle selector."""
    name = i.resolve(names)
    if i.has_operand(name + '1'):
        return [trunc(i.value(n, modifiers=False), 16) for n in (name + '1', name)]
    raw = i.value(name, modifiers=False)
    if raw.w == 16: return [raw, raw]   # HADD2.F32's single binary16 immediate
    v = i.modified(name, 'absolute', raw, lambda x: x & HALF_ABS)
    v = i.modified(name, 'negate', v, lambda x: x ^ HALF_SIGNS)
    return swizzled(i, swizzle, extract(v, 15, 0), extract(v, 31, 16), single(i, name, swizzle, raw))


def single(i, name, swizzle, raw):
    """SM75 HADD2's .F32 source (`HADD2 R0, R2.F32, -RZ.H0_H0` is ptxas's cvt.rz.f16.f32): the binary32
    register, after |x| and -x on its sign, rounded toward zero to binary16 in both lanes. ptxas also
    fuses cvt.rz.f16.f32 with add.f16x2 into HADD2 with a register, so the sum is of the narrowed value;
    B follows (the sum commutes). Its NaN doesn't matter: the sum's NaN is canonical. None elsewhere:
    ptxas doesn't fuse the conversion into HFMA2/HMUL2, whose .F32 sources (and HSET2/HSETP2's) may act
    in binary32, and SM80+ forbid F32 (no SM75 GPU to tell)."""
    if i.opcode != 'HADD2' or not i.has_operand(swizzle) or 'F32' not in i.legal_labels(swizzle): return None
    x = i.modified(name, 'absolute', raw, lambda v: v & 0x7fffffff)
    x = i.modified(name, 'negate', x, lambda v: v ^ 0x80000000)
    return fpformat.narrow(x, 'F32', 'F16', 'RZ')


def swizzled(i, swizzle, low, high, f32=None):
    """(low, high) lanes picked by the swizzle selector (H1_H0, H0_H0, H1_H1, and F32: the narrowed
    binary32 in both): values chosen at decode time, not a branch of the family per swizzle."""
    if not i.has_operand(swizzle): return [low, high]
    narrowed = {} if f32 is None else {'F32': f32}
    return [i.select_lookup(swizzle, {'H1_H0': low, 'H0_H0': low, 'H1_H1': high, **narrowed}),
            i.select_lookup(swizzle, {'H1_H0': high, 'H0_H0': low, 'H1_H1': high, **narrowed})]


def half_swizzles(i, fma):
    b = 'iswzB' if i.has_operand('iswzB') else 'iswzC_as_B'
    c = 'iswzC' if i.has_operand('iswzC') else 'iswzB_as_C'
    return ['iswzA', b, c] if fma else ['iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C']


def half_saturating(i):
    """(.SAT, the RELU enable: None without .RELU). HFMA2/HFMA2.MMA .RELU applies only while its Pp
    holds (SM89 GPU)."""
    name = 'satrelu' if i.has_operand('satrelu') else 'sat'
    if not i.has_operand(name): return False, None
    label, relu = i.select_fork(name), 'RELU' in i.labels(name)
    if label not in ('nosat', 'SAT', *(('RELU',) if relu else ())): unmodelled(f'{label}')
    return label == 'SAT', i.pred('Pp') if relu and label == 'RELU' else None


@definition(lambda a: (f"relu.f{a['r'].w}", ["r"]))
def float_rectified(r):
    """PTX .relu on a float lane: a result whose sign is set (negative, -0) becomes +0; an arithmetic
    NaN result is already the canonical positive one (SM89 GPU, HFMA2.RELU)."""
    return const(0, r.w) if slt(r, const(0, r.w)) else r


def no_flush(i):
    """HADD2/HFMA2: FTZ/FMZ are unmodelled in the exact lowering, ignored by the readable one."""
    for name in ('ftz', 'fmz'):
        if i.has_operand(name):
            label = i.select_fork(name)
            if not readable(i) and not str(label).startswith(('noftz', 'nofmz')): unmodelled(f'{label}')


def negative_zero_product(i):
    """A is -RZ and B is RZ (ptxas's spelling; RZ, -RZ takes the general path): the product is -0 in
    every lane."""
    return all(i.has_operand(n) and i.is_zero(n) for n in ('Ra', 'Rb')) and i.operand_negated('Ra') and not i.operand_negated('Rb')


def canonical_half(h):
    """h, or the canonical +0x7fff when it is a NaN (bit tests, so an immediate folds)."""
    return ite((extract(h, 14, 10) == 0x1f) & (extract(h, 9, 0) != 0), const(0x7fff, 16), h)


@family('HADD2', 'HFMA2', 'HFMA2.MMA')
def half_arithmetic(i):
    """Rd = per lane a + c (HADD2) or a * b + c (HFMA2) on binary16, rounded once (the readable
    lowering: unfused), with .SAT. HADD2.F32 adds the low lanes, rounds to binary16 and widens the
    result to binary32 (FTZ flushes inputs and the binary16 result). SM89's HADD2.F32 classes add
    -RZ; SM75's .F32 result of the ordinary classes is modelled only with a -RZ operand too
    (ptxas's cvt.f32.f16)."""
    mma = i.opcode == 'HFMA2.MMA'   # HFMA2 on the MMA pipe (same values, SM89 GPU)
    fma = i.opcode in ('HFMA2', 'HFMA2.MMA')
    names = ['Ra|Sa', 'Rb|Sb', 'Rc|Sc'] if fma else ['Ra|Sa', 'Rc|Sc']
    if i.width('Rd') != 32: unmodelled('width')
    ofmt = i.select_fork('ofmt') if i.has_operand('ofmt') else 'F16_V2'
    if not fma and ofmt == 'F32':
        if not i.cls.startswith('hadd2_F32') and not any(i.is_zero(n) and i.operand_negated(n) and not i.operand_absolute(n)
                                                          for n in ('Ra', 'Rc') if i.has_operand(n)):
            unmodelled('F32 result of a sum with a nonzero operand')
        ftz = i.select_fork('ftz') == 'FTZ' and not readable(i) if i.has_operand('ftz') else False
        a, c = (half_lanes(i, n, s)[0] for n, s in zip(names, ('iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C')))
        if ftz: a, c = flushed(a), flushed(c)
        r = fadd(a, c)
        if ftz: r = flushed(r)
        if half_saturating(i)[0]: r = saturated(r)
        return outputs(Rd=widen_half(r))
    if ofmt != 'F16_V2': unmodelled('ofmt')
    ftz = False
    if mma:
        mode = i.select_fork('fmz')
        if mode not in ('nofmz_hfma2', 'nofmz', 'FTZ'): unmodelled(f'MMA {mode}')
        ftz = mode == 'FTZ' and not readable(i)
    else: no_flush(i)
    sat, relu = half_saturating(i)
    if fma and i.has_operand('Sc1') and not (sat or ftz) and relu is None and negative_zero_product(i):
        # -0 * 0 + imm is imm with its NaN halves canonical, computed while decoding (SLEIGH doesn't fold
        # float arithmetic on immediates): ptxas's constant move HFMA2.MMA Rd, -RZ, RZ, imm is one COPY.
        # A word, not a concat: the lanes' choices can't absorb it, so the folded constant is the whole Rd.
        hi, lo = (zext(canonical_half(i.imm(n, 16)), 32) for n in ('Sc', 'Sc1'))
        return outputs(Rd=(hi << 16) | lo)
    inputs = [half_lanes(i, n, s) for n, s in zip(names, half_swizzles(i, fma))]
    lanes = []
    for lane in (0, 1):
        x = [flushed(v[lane]) if ftz else v[lane] for v in inputs]
        r = (fadd(fmul(x[0], x[1]), x[2]) if readable(i) else ffma(*x)) if fma else fadd(*x)
        if ftz: r = flushed(r)
        r = saturated(r) if sat else r
        lanes.append(r if relu is None else float_rectified(r) if relu else r)
    return outputs(Rd=concat(lanes[1], lanes[0]))


@definition("fmz")
def half_flushed_fmz(x):
    """Flush to +0 (.FMZ on an F16 input): a denormal or zero becomes +0, other values are unchanged."""
    return x if (x & 0x7c00) != 0 else const(0, 16)


# Rd, A, B lane formats of an HMUL2 format label (SM107 names all three: Dfmt.Afmt.Bfmt).
MUL_FORMATS = {'F16_V2': ('F16', 'F16', 'F16'), 'BF16_V2': ('BF16', 'BF16', 'BF16'),
               'BF16x2.BF16x2.F16x2': ('BF16', 'BF16', 'F16'), 'F16x2.F16x2.BF16x2': ('F16', 'F16', 'BF16')}


def as_double(x, fmt): return fconv(x if fmt == 'F16' else concat(x, const(0, 16)), 64)


@family('HMUL2')
def hmul2(i):
    """Rd = per lane a * b on binary16, .SAT; .FTZ flushes inputs and results keeping the sign,
    .FMZ flushes inputs to +0 and makes the product +0 when either source is zero or denormal
    (even 0 * Inf). BF16 and SM107's mixed F16/BF16 formats (PTX mul.d.a.b): b rounded (RN) to Rd's
    format, then the exact product (binary64 holds it) rounded RN to it; .SAT/.FTZ/.FMZ unmodelled."""
    if i.width('Rd') != 32: unmodelled('width')
    fmt = i.select_fork('ofmt|Dfmt.Afmt.Bfmt')
    if fmt not in MUL_FORMATS: unmodelled('format')
    mode = str(i.select_fork('fmz')) if i.has_operand('fmz') else 'nofmz'
    if mode not in ('nofmz', 'nofmz_hfma2', 'FMZ', 'FTZ'): unmodelled(mode)
    if readable(i): mode = 'nofmz'
    sat, relu = half_saturating(i)
    if relu is not None: unmodelled('RELU')
    a, b = (half_lanes(i, n, s) for n, s in zip(('Ra|Sa', 'Rb|Sb'), ('iswzA', 'iswzB')))
    if fmt != 'F16_V2':
        if sat or mode not in ('nofmz', 'nofmz_hfma2'): unmodelled(f'{mode} SAT={sat} of {fmt}')
        d, fa, fb = MUL_FORMATS[fmt]   # PTX mul.d.a.b: d = a * convert(b), b first rounded to d's format
        to_d = (lambda y: y) if fb == d else (lambda y: fpformat.narrow(as_double(y, fb), 'F64', d))
        lanes = [fpformat.narrow(fmul(as_double(x, fa), as_double(to_d(y), d)), 'F64', d) for x, y in zip(a, b)]
        return outputs(Rd=concat(lanes[1], lanes[0]))
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


def half_as_single(i, x, ftz):
    """A F16 lane (flushed under .FTZ) or, by SM90+'s ofmt, a BF16 lane as binary32, exactly
    (the md forbids .FTZ with BF16)."""
    f16 = fconv(flushed(x) if ftz else x, 32)
    if not i.has_operand('ofmt'): return f16
    return i.select_lookup('ofmt', {'F16_V2': f16, 'BF16_V2': None if ftz else concat(x, const(0, 16))})


def half_comparisons(i):
    """Per lane (low first), A's lane compared with B's by the cmp selector."""
    swizzles = ('iswzA', 'iswzB' if i.has_operand('iswzB') else 'iswzB_as_C')
    ftz = flushes(i)
    a, b = (half_lanes(i, n, s) for n, s in zip(('Ra|Sa', 'Rc|Sc'), swizzles))
    return [comparison(i, 'cmp', half_as_single(i, x, ftz), half_as_single(i, y, ftz)) for x, y in zip(a, b)]


@family('HSET2')
def hset2(i):
    """Rd lane = (A lane compared with B lane) bop Pp: true gives 1.0 in the lane format (.BF) or
    0xffff (.BM), false +0."""
    if i.width('Rd') != 32: unmodelled('width')
    one = i.select_lookup_const('ofmt', {'F16_V2': 0x3c00, 'BF16_V2': 0x3f80}, 16) if i.has_operand('ofmt') else const(0x3c00, 16)
    true = i.select_lookup('bval', {'BF': one, 'BM': const(0xffff, 16)})
    lanes = [true if combined(i, c) else const(0, 16) for c in half_comparisons(i)]
    return outputs(Rd=concat(lanes[1], lanes[0]))


@family('HSETP2')
def hsetp2(i):
    """Pu = lane 0's comparison bop Pp, Pv = lane 1's; .H_AND: Pu = (both lanes) bop Pp, Pv = !(both lanes) bop Pp."""
    low, high = half_comparisons(i)
    if i.select_fork('h_and') == 'H_AND': return set_predicates(i, low & high)
    return outputs(Pu=combined(i, low), Pv=combined(i, high))


# ---- packed binary32 pairs: FADD2/FMUL2/FFMA2 and the mixed FHADD2/FHFMA2/FHMUL2 (SM107) ------------

SIGN32 = SIGN_EXPONENT[32][0]
PAIR_SWAPPED = {'F32x2': False, 'F32x2.HI_LO': False, 'F32x2.LO_HI': True, 'F32': False}
HALF_FORMATS = {'F16x2': 'F16', 'BF16x2': 'BF16'}
# +0x7fff, as F2FP's F16/BF16 packs (SM89 GPU). F16 nominally keeps the NaN's sign: the narrowed
# arithmetic NaN is always + (nanresult), and an unsigned F16 NaN made SM107 exact 866 KB larger.
NARROW_NAN = {'F16': (True, 0x7fff), 'BF16': (False, 0x7fff)}


def widened(i, fmt, x):
    """A F16/BF16 lane as binary32, exactly; `fmt` names the selector choosing the format."""
    return i.select_lookup(fmt, {'F16x2': fconv(x, 32), 'BF16x2': concat(x, const(0, 16))})


def pair_lanes(i, names, swizzle, neg=None, half=None):
    """(lane 0, lane 1) binary32 inputs: a register pair (.LO_HI swaps it), one register or
    immediate for both (.F32), or a F16x2/BF16x2 register (format selector `half`, H1_H0/H0_H0/H1_H1)
    widened; then |x| on both and, after the swizzle, -x on lane 0 with the negate bit and on lane 1
    with negate XOR NEGA .NP (ptxas folds neg.f32 of either lane so)."""
    name = i.resolve(names)
    v = i.value(name, modifiers=False)
    if half:
        lanes = [widened(i, half, x) for x in swizzled(i, swizzle, extract(v, 15, 0), extract(v, 31, 16))]
    elif v.w == 64:
        lo, hi = extract(v, 31, 0), extract(v, 63, 32)
        lanes = [i.select_lookup(swizzle, {l: (hi if s else lo) for l, s in PAIR_SWAPPED.items()}),
                 i.select_lookup(swizzle, {l: (lo if s else hi) for l, s in PAIR_SWAPPED.items()})]
    else:
        if swizzle and i.has_operand(swizzle) and i.select_fork(swizzle) != 'F32': unmodelled(swizzle)
        lanes = [v, v]
    lanes = [i.modified(name, 'absolute', x, float_abs) for x in lanes]
    lanes = [i.modified(name, 'negate', x, float_neg) for x in lanes]
    if neg and i.has_operand(neg):
        # .NP on a broadcast: no ptxas form (it copies the register instead), so unmodelled.
        lanes[1] = lanes[1] ^ i.select_lookup_const(neg, {'PP': 0, 'NP': SIGN32} if half or v.w == 64 else {'PP': 0})
    return lanes


def pair_operands(i, *specs):
    """The lanes of each (names, swizzle, NEGA) source, flushed under .FTZ, A widened from
    F16x2/BF16x2 when the class has an Afmt selector of those."""
    for name in ('Afmt', 'Bfmt', 'Cfmt', 'Dfmt'):
        if i.has_operand(name) and i.labels(name) == ['F32x2']: i.select_fork(name)
    half = 'Afmt' if i.has_operand('Afmt') and set(i.labels('Afmt')) <= set(HALF_FORMATS) else None
    ftz = flushes(i)
    lanes = [pair_lanes(i, n, s, neg, half if k == 0 else None) for k, (n, s, neg) in enumerate(specs)]
    return ftz, [[flushed(x) if ftz else x for x in l] for l in lanes]


def narrowed(i, x, rnd, ftz):
    """A binary32 lane rounded to the Dfmt half format (F16x2/BF16x2), a F16 denormal flushed
    under .FTZ (the md requires .FTZ for F16x2 and forbids it for BF16x2)."""
    def to(f):
        r = fpformat.narrow(x, 'F32', f, rnd, nan=NARROW_NAN[f])
        return flushed(r) if ftz and f == 'F16' else r
    return i.select_lookup('Dfmt', {l: to(f) for l, f in HALF_FORMATS.items()})


def pair_result(i, ftz, lanes):
    """Rd: the two binary32 lanes (flushed under .FTZ), or, with a F16x2/BF16x2 Dfmt, each rounded
    to it. The narrowing forms are RZ only, and RZ to binary32 then RZ to the half format is RZ once
    (the half format's values are a subset of binary32's)."""
    if i.has_operand('Dfmt') and set(i.labels('Dfmt')) & set(HALF_FORMATS):
        rnd = rounding(i)
        lanes = [narrowed(i, x, rnd, ftz) for x in lanes]
    elif ftz: lanes = [flushed(x) for x in lanes]
    r = concat(lanes[1], lanes[0])
    if r.w != i.width('Rd'): unmodelled('width')
    return outputs(Rd=r)


A2 = ('Ra', 'iswzA', 'negA')
C2 = ('Rc|Sc', 'iswzC', 'negC')


@family('FADD2', 'FHADD2')
def fadd2(i):
    """Rd = per lane a + c, rounded once; FHADD2 widens a F16x2/BF16x2 A, or rounds the sum (RZ)
    to a F16x2/BF16x2 Rd."""
    ftz, (a, c) = pair_operands(i, A2, C2)
    rnd = rounding(i)
    return pair_result(i, ftz, [fadd(x, y, rnd) for x, y in zip(a, c)])


@family('FMUL2', 'FHMUL2')
def fmul2(i):
    """Rd = per lane a * b, rounded once (FHMUL2: RZ to a F16x2/BF16x2 Rd)."""
    ftz, (a, b) = pair_operands(i, A2, ('Rb|Sb', 'iswzB', None))
    rnd = rounding(i)
    return pair_result(i, ftz, [fmul(x, y, rnd) for x, y in zip(a, b)])


@family('FFMA2', 'FHFMA2')
def ffma2(i):
    """Rd = per lane a * b + c, rounded once (the readable lowering: unfused); FHFMA2 widens a
    F16x2/BF16x2 A."""
    ftz, (a, b, c) = pair_operands(i, A2, ('Rb|Sb', 'iswzB', None), C2)
    return pair_result(i, ftz, [fused(i, x, y, z) for x, y, z in zip(a, b, c)])


# ---- HMNMX2: packed min/max on F16, BF16 or E6M9 lanes ----------------------------------------------

LANE_FORMATS = {'F16_V2': (0x7c00, 0x03ff), 'BF16_V2': (0x7f80, 0x007f), 'E6M9_V2': (0x7e00, 0x01ff)}
LANES = list(LANE_FORMATS)   # a lane format's code: its index
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
def lane_parts(code, p, a, c, ar, cr, ftz):
    """(A NaN, C NaN, a, c, both zero, the zeros' pick, A taken) of one lane: a, c with modifiers
    (flushed under .FTZ), ar, cr as encoded. The format's NaN test and order are chosen by its code
    (a decode-time choice in SLEIGH), not a branch per format."""
    by_format = lambda f: chosen(code, lambda k: f(LANES[k]))
    nan_a, nan_c = by_format(lambda fmt: lane_nan(ar, fmt)), by_format(lambda fmt: lane_nan(cr, fmt))
    if ftz: a, c = flushed(a), flushed(c)
    zeros = ((a | c) & 0x7fff) == 0
    zero_pick = (a | c) if p else (a & c)
    take_a = by_format(lambda fmt: lane_less(a, c, fmt)) == p
    return nan_a, nan_c, a, c, zeros, zero_pick, take_a


def lane_name(kind=""):
    """A namer: min/max (a constant predicate) or hmnmx (of it), [.ftz][.NaN][.xorsign]<kind>, then
    the lane format when constant (.f16, .bf16, .e6m9), else of its code too."""
    return lambda a: lane_label(a, kind)


def lane_label(a, kind):
    code, p = a["code"], a["p"]
    mods = [".ftz"] * a["ftz"] + [".NaN"] * a.get("nan", False) + [".xorsign"] * a.get("xorsign", False) + [kind] * bool(kind)
    fmt = ["." + LANES[value(code)].split("_")[0].lower()] if is_const(code) else []
    head = ("min" if value(p) else "max") if is_const(p) else "hmnmx"
    return "".join([head, *mods, *fmt]), [*(["code"] if not fmt else []), *(["p"] if head == "hmnmx" else []),
                                          "a", "c", *(["ar", "cr"] if a["ar"] is not None else [])]


@definition(lane_name())
def hmnmx_lane(code, p, a, c, ar=None, cr=None, ftz=False, nan=False, xorsign=False):
    """PTX min (predicate) or max of two F16/BF16/E6M9 lanes with the GPU's NaN: a number wins over
    NaN, two NaNs give 0x7fff, -0 is below +0; .NaN: any NaN gives 0x7fff; .xorsign: the result
    takes the XOR of the encoded signs; .ftz flushes inputs and result.

    code: the lane format's index in LANES; ar, cr: the lanes as encoded, when not a, c."""
    ar, cr = (a, c) if ar is None else (ar, cr)
    nan_a, nan_c, a, c, zeros, zero_pick, take_a = lane_parts(code, p, a, c, ar, cr, ftz)
    ordered = (zero_pick if zeros else (a if take_a else c))
    both = nan_a & nan_c
    canonical = const(CANONICAL_HALF_NAN, 16)
    if nan: r = canonical if nan_a | nan_c else ordered
    else: r = canonical if both else (c if nan_a else (a if nan_c else ordered))
    if xorsign:
        keep = (both | (nan_a | nan_c)) if nan else both
        r = r if keep else (r & 0x7fff) | ((ar ^ cr) & 0x8000)
    if ftz: r = flushed(r)
    return r


@definition(lane_name(".is_a"))
def lane_chose_a(code, p, a, c, ar=None, cr=None, ftz=False):
    """HMNMX2.IS_A: whether a lane's min (predicate) or max is A: A wins two NaNs, the number wins one.

    ar, cr: the lanes as encoded, when not a, c."""
    ar, cr = (a, c) if ar is None else (ar, cr)
    nan_a, nan_c, a, c, zeros, zero_pick, take_a = lane_parts(code, p, a, c, ar, cr, ftz)
    chose_a = ((a == zero_pick) if zeros else take_a)
    return const(1, 1) if nan_a & nan_c else (const(0, 1) if nan_a else (const(1, 1) if nan_c else chose_a))


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
    code = i.select_lookup_const('ofmt', {f: k for k, f in enumerate(LANES) if f in i.labels('ofmt')}, 2)
    # No readable variant, as before: FTZ and the NaN rules apply in both lowerings.
    unmodified = all(lanes[n][k] is raws[n][k] for n in (0, 1) for k in (0, 1))   # then the label omits the raw lanes
    raw = lambda k: (None, None) if unmodified else (raws[0][k], raws[1][k])
    results = [hmnmx_lane(code, p, lanes[0][k], lanes[1][k], *raw(k), ftz, nan, xorsign) for k in (0, 1)]
    writes = dict(Rd=concat(results[1], results[0]))
    if predicates: writes.update({n: lane_chose_a(code, p, lanes[0][k], lanes[1][k], *raw(k), ftz) for k, n in enumerate(("Pu", "Pv"))})
    return outputs(**writes)


def raw_half_lanes(i, names, swizzle):
    """half_lanes without the |x| / -x modifiers (NaN tests and XORSIGN read the encoded signs)."""
    name = i.resolve(names)
    if i.has_operand(name + '1'): return [trunc(i.value(n, modifiers=False), 16) for n in (name + '1', name)]
    v = i.value(name, modifiers=False)
    if v.w == 16: return [v, v]
    return swizzled(i, swizzle, extract(v, 15, 0), extract(v, 31, 16))
