"""Integer families."""
from sass.sem import *
from sass.sem_family import family, helper, outputs, unmodelled


def addend(i, names, v, x):
    """A 32-bit term of a carry-producing sum, zero-extended to 64 bits: negated (plain form) or
    inverted (.X form) when its operand is; a negated 0 still carries 2^32."""
    w = zext(v, 64)
    if not (i.operand_inverted if x else i.operand_negated)(names): return w
    return w ^ 0xffffffff if x else (w ^ 0xffffffff) + 1


@family('IADD3', 'UIADD3')
def iadd3(i):
    """Rd = Ra + Rb + Rc, with carries at 2^32 (Pu) and 2^33 (Pv); .X adds carry-ins Pp and Pq.
    Plain IADD3 can negate a source, IADD3.X invert one (the md forbids modifying both Ra and Rb)."""
    x = i.has_operand('X')
    term = lambda names: addend(i, names, i.value(names, modifiers=False), x)
    total = term('Ra') + term('Rb|Sb') + term('Rc|Sc')
    if x: total = total + zext(i.pred('Pp'), 64) + zext(i.pred('Pq'), 64)
    return outputs(Rd=trunc(total, 32), Pu=total >= 1 << 32, Pv=total >= 1 << 33,
                   order=('Pv', 'Pu'))  # aliased Pu/Pv: Pu wins (SM89)


@family('BMSK')
def bmsk(i):
    """A mask of `width` ones starting at bit `pos`; .W wraps both to 5 bits, .C clamps the width."""
    pos, width = i.value('Ra'), i.value('Rb|Sb')
    match i.sel('cw'):
        case 'W': pos, width = pos & 31, width & 31
        case 'C': width = umin(width, 32)
        case _: unmodelled('cw selector')
    ones = const(0xffffffff, 32)
    return outputs(Rd=0 if pos >= 32 else (ones << pos) & ~(ones << (pos + width)))


@family('MOV', 'UMOV', 'MOV64IUR')
def mov(i):
    """Rd = the source (register, uniform register, immediate or constant), 32 or 64 bits. MOV.SPILL
    (SM100+) is taken as the same copy, as before the migration (no hardware evidence)."""
    if i.has_operand('indexURd|indexURb'): unmodelled('indexed register file')
    if i.has_operand('size') and i.sel('size') not in ('64', '_64'): unmodelled('size')
    if i.has_operand('nonconformity') and i.sel('nonconformity') != 'SPILL': unmodelled('nonconformity')
    return outputs(Rd=resize(i.value('Sb|Sa|Ra|Rb'), i.width('Rd')))


@family('SEL', 'USEL')
def sel(i):
    """Rd = Pp ? Ra : Rb."""
    if i.has_operand('size') and i.sel('size') not in ('64', '_64'): unmodelled('size')
    w = i.width('Rd')
    return outputs(Rd=resize(i.value('Ra'), w) if i.pred('Pp') else resize(i.value('Rb|Sb'), w))


@family('IABS')
def iabs_(i):
    """Rd = |source| (two's complement: |-2^31| = -2^31)."""
    if i.width('Rd') != 32: unmodelled('width')
    return outputs(Rd=iabs(i.value('Sb|Rb|Ra')))


@family('IADD', 'VIADD')
def iadd(i):
    """Rd = Ra + Rb, 32 bits, no carry out (Pu = PT). The SM100+ .X/.XOR/.64 classes are
    unmodelled (their class selectors have no default)."""
    if i.width('Rd') != 32: unmodelled('width')   # IADD.64 (SM120)
    if i.has_operand('fmt') and i.sel('fmt') not in ('32', '_32', 'U32', 'S32'): unmodelled(f"{i.sel('fmt')} format")
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('carry out')
    return outputs(Rd=i.value('Ra|Sa') + i.value('Rb|Sb'))


@family('LEA', 'ULEA')
def lea(i):
    """Rd = (Ra << scale) + Rb, carry Pu at 2^32; .HI shifts Rc:Ra (or sext(Ra), .SX32) and takes the
    high word. Ra's modifier applies to the shifted word; .X adds Pp and inverts instead of negating."""
    if i.has_operand('Pr'): unmodelled('Pr')
    x = i.has_operand('X')
    a, n = i.value('Ra', modifiers=False), zext(i.value('scaleU5'), 64)
    match i.sel('hilo'):
        case 'LO': word = trunc(zext(a, 64) << n, 32)
        case 'HI':
            if i.has_operand('sx32'):
                if i.sel('sx32') != 'SX32': unmodelled('HI without SX32 or a high word')
                wide = sext(a, 64)
            else: wide = concat(i.value('Rc|Sc'), a)
            word = trunc((wide << n) >> 32, 32)
    total = addend(i, 'Ra', word, x) + addend(i, 'Rb|Sb', i.value('Rb|Sb', modifiers=False), x)
    if x: total = total + zext(i.pred('Pp'), 64)
    return outputs(Rd=trunc(total, 32), Pu=total >= 1 << 32)


@family('IMAD', 'UIMAD')
def imad(i):
    """Rd = Ra * Rb + Rc (.X: + Pp). .LO keeps the low word; .HI/.WIDE form the 64-bit product
    (signed for S32) plus Rc and keep its high word or all of it, Pu = unsigned overflow of that
    sum. A narrow (32-bit, non-pair) addend of .HI is added to the high word instead (SM89 GPU)."""
    if i.has_operand('Pq'): unmodelled('Pq')
    if i.has_operand('x') and i.sel('x') not in ('nox', 'NOX'): unmodelled('x selector')
    fmt = i.sel('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    x = i.has_operand('X')
    a, b, c = i.value('Ra|Sa'), i.value('Rb|Sb'), i.value('Rc|Sc')
    if max(a.w, b.w, c.w) > 64: unmodelled('wide source')
    cin = zext(i.pred('Pp'), 64) if x else const(0, 64)
    mode = i.sel('wide')
    if mode == 'LO': return outputs(Rd=a * b + resize(c, 32) + trunc(cin, 32))
    ext = sext if fmt == 'S32' else zext
    product = ext(a, 64) * ext(b, 64)
    pair = i.kind('Rc|Sc') in ('register', 'uniform') and c.w == 64
    if mode == 'HI' and not pair and c.w <= 32:
        full = zext(trunc(product >> 32, 32), 64) + zext(c, 64) + cin
        return outputs(Rd=trunc(full, 32), Pu=full >= 1 << 32)
    summed = product + resize(c, 64)
    full = summed + cin
    overflow = (summed < product) | (full < summed) if x else summed < product
    return outputs(Rd=resize(full >> 32 if mode == 'HI' else full, i.width('Rd')), Pu=overflow)


def truth_table(bits, inputs):
    """The function with truth table `bits` (input 0 is the most significant index bit) as a small
    expression: Shannon expansion on each input, dropping inputs it doesn't depend on."""
    full = (1 << (1 << len(inputs))) - 1
    if bits == 0: return const(0, inputs[0].w)
    if bits == full: return const(-1, inputs[0].w)
    v, *rest = inputs
    half = 1 << len(rest)
    low, high, ones = bits & ((1 << half) - 1), bits >> half, (1 << half) - 1
    if low == high: return truth_table(low, rest)
    if (low, high) == (0, ones): return v
    if (low, high) == (ones, 0): return ~v
    lo, hi = truth_table(low, rest), truth_table(high, rest)
    if high == low ^ ones: return v ^ lo
    if low == 0: return v & hi
    if high == 0: return ~v & lo
    if low == ones: return ~v | hi
    if high == ones: return v | lo
    return (v & hi) | (~v & lo)


@family('LOP3', 'ULOP3')
def lop3(i):
    """Rd = the bitwise function of Ra, Rb, Rc with truth table imm8 (bit 4a+2b+c is f(a, b, c)).
    Pu = Rd != 0, combined with Pp by .POR/.PAND."""
    if i.sel('lut') != 'LUT': unmodelled('lut')
    rd = truth_table(i.static('imm8'), [i.value('Ra|Sa'), i.value('Rb|Sb'), i.value('Rc|Sc')])
    pu = rd != 0
    if i.has_operand('Pp'):
        match i.sel('pop'):
            case 'POR': pu = pu | i.pred('Pp')
            case 'PAND': pu = pu & i.pred('Pp')
    return outputs(Rd=rd, Pu=pu)


@family('PRMT', 'UPRMT')
def prmt(i):
    """Rd = four bytes picked from Rc:Ra (Ra is bytes 0-3) by the selector Rb, per mode. IDX: nibble
    j of Rb picks byte j, bit 3 replicating the picked byte's sign; F4E/B4E: forward/backward
    from Rb & 3; RC8/RC16: replicate a byte/half; ECL/ECR: edge clamp left/right (SM89 GPU). UPRMT
    is IDX only."""
    if any(i.width(n) != 32 for n in ('Rd', 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')): unmodelled('width')
    s, data = i.value('Rb|Sb'), concat(i.value('Rc|Sc'), i.value('Ra|Sa'))
    mode = i.sel('pmode|idx')   # UPRMT spells its only mode /IDXOnly("IDX"):idx
    picked = []
    for j in range(4):
        match mode:
            case 'IDX': p = (s >> 4 * j) & 15
            case 'F4E': p = (s & 3) + j
            case 'B4E': p = ((s & 3) - j) & 7
            case 'RC8': p = s & 3
            case 'RC16': p = (s & 1) * 2 + (j & 1)
            case 'ECL': p = umax(s & 3, j)
            case 'ECR': p = umin(s & 3, j)
            case other: unmodelled(f'{other} mode')
        byte = trunc(data >> (zext(p & 7, 64) << 3), 8)
        if mode == 'IDX': byte = sext(extract(byte, 7, 7), 8) if (p & 8) != 0 else byte   # replicate the sign bit
        picked.append(byte)
    return outputs(Rd=concat(*reversed(picked)))


@family('SHF', 'USHF')
def shf(i):
    """Rd = a word of Rc:Ra shifted left or right (arithmetic for signed formats) by Rb, wrapped
    (.W) or clamped (.C) to the format width; .HI takes the high word."""
    fmt = i.sel('fmt')
    if fmt not in ('U32', 'S32', 'U64', 'S64'): unmodelled(f'{fmt} format')
    limit = 32 if fmt.endswith('32') else 64
    n = i.value('Rb|Sb')
    match i.sel('cw'):
        case 'W': count = n & (limit - 1)
        case 'C': count = umin(n, limit)
        case other: unmodelled(f'{other} cw')
    base, count = concat(i.value('Rc|Sc'), i.value('Ra|Sa')), zext(count, 64)
    match i.sel('dir'):
        case 'L': r = base << count
        case 'R': r = sar(base, count) if fmt.startswith('S') else base >> count
        case other: unmodelled(f'{other} direction')
    match i.sel('hilo'):
        case 'LO': pass
        case 'HI': r = r >> 32
        case other: unmodelled(f'{other} half')
    return outputs(Rd=trunc(r, 32))


def word_source(i):
    """The single 32-bit source of BREV/POPC/FLO (register, uniform, immediate, constant or CX)."""
    if i.width('Rd') != 32 or i.width('Rb|Sb') != 32: unmodelled('width')
    return i.value('Rb|Sb')


@family('BREV', 'UBREV')
def brev(i):
    """Rd = the source's bits in reverse order."""
    x = word_source(i)
    for shift, m in ((1, 0x55555555), (2, 0x33333333), (4, 0x0f0f0f0f), (8, 0x00ff00ff)):
        x = ((x >> shift) & m) | ((x & m) << shift)
    return outputs(Rd=(x >> 16) | (x << 16))


@family('POPC', 'UPOPC')
def popc(i):
    """Rd = the number of set bits of the source."""
    x = word_source(i)
    x = x - ((x >> 1) & 0x55555555)
    x = (x & 0x33333333) + ((x >> 2) & 0x33333333)
    x = (x + (x >> 4)) & 0x0f0f0f0f
    x = x + (x >> 8)
    x = x + (x >> 16)
    return outputs(Rd=x & 0x3f)


@family('FLO', 'UFLO')
def flo(i):
    """Rd = the index of the most significant set bit (.SH: its distance from bit 31), or -1 for 0.
    .S32 searches the complement of a negative value: the highest bit differing from the sign
    (SM89 GPU, bfind.s32)."""
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('Pu')
    fmt = i.sel('fmt')
    if fmt not in ('S32', 'U32', '32'): unmodelled(f'{fmt} format')
    x0 = word_source(i)
    if fmt == 'S32': x0 = ~x0 if slt(x0, 0) else x0
    x, n = x0, const(0, 32)
    for shift, m in ((16, 0xffff0000), (8, 0xff000000), (4, 0xf0000000), (2, 0xc0000000), (1, 0x80000000)):
        low = (x & m) == 0
        n, x = (n | shift if low else n), (x << shift if low else x)
    match i.sel('sh'):
        case 'nosh': r = 31 - n
        case 'SH': r = n
    return outputs(Rd=const(-1, 32) if x0 == 0 else r)


@family('SGXT', 'USGXT')
def sgxt(i):
    """Rd = sext(byte Ra & 3 of Rb)."""
    if i.sel('cw') != 'C' or i.sel('fmt') not in ('S32', '32'): unmodelled('cw/fmt')
    if i.width('Rd') != 32: unmodelled('width')
    return outputs(Rd=sext(trunc(i.value('Rb|Sb') >> ((i.value('Ra') & 3) << 3), 8), 32))


def compare(label, a, b, signed):
    """The md comparison `label` (F LT EQ LE GT NE GE T) of a and b; EQ/NE ignore signedness."""
    lt, le = (slt, sle) if signed else ((lambda x, y: x < y), (lambda x, y: x <= y))
    match label:
        case 'F': return const(0, 1)
        case 'T': return const(1, 1)
        case 'LT': return lt(a, b)
        case 'LE': return le(a, b)
        case 'GT': return lt(b, a)
        case 'GE': return le(b, a)
        case 'EQ': return a == b
        case 'NE': return a != b
    unmodelled(f'{label} comparison')


def boolean(label, x, y):
    match label:
        case 'AND': return x & y
        case 'OR': return x | y
        case 'XOR': return x ^ y
    unmodelled(f'{label} operation')


@family('ISETP', 'UISETP')
def isetp(i):
    """Pu = (Ra icmp Rb) bop Pp, Pv = !(Ra icmp Rb) bop Pp. .EX compares the high words of a wider
    comparison: if they are equal, Pr (the low words' result) decides."""
    fmt = i.sel('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    a, b = i.value('Ra|Sa'), i.value('Rb|Sb')
    if a.w != 32 or b.w != 32: unmodelled('width')
    cond = compare(i.sel('icmp'), a, b, fmt == 'S32')
    if i.has_operand('ex') and i.sel('ex') == 'EX':
        if not i.has_operand('Pr'): unmodelled('EX without Pr')
        cond = cond if a != b else i.pred('Pr')
    elif i.has_operand('Pr'): i.ignore('Pr')
    p, bop = i.pred('Pp'), i.sel('bop')
    return outputs(Pu=boolean(bop, cond, p), Pv=boolean(bop, lnot(cond), p))


@family('IMNMX', 'UIMNMX')
def imnmx(i):
    """Rd = Pp ? min(Ra, Rb) : max(Ra, Rb), signed for S32. SM120's forms with predicate outputs
    Pu/Pv and a second input Pq are unmodelled (Pq's meaning is unknown, even as PT)."""
    if any(i.has_operand(p) for p in ('Pu', 'Pv', 'Pq')): unmodelled('SM120 predicates')
    fmt = i.sel('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    a, b = i.value('Ra|Sa'), i.value('Rb|Sb')
    if a.w != 32 or b.w != 32 or i.width('Rd') != 32: unmodelled('width')
    less = slt(a, b) if fmt == 'S32' else a < b
    return outputs(Rd=a if less == i.pred('Pp') else b)


def lanes(x, width):
    """x's lanes of `width` bits, lowest first."""
    return [extract(x, lo + width - 1, lo) for lo in range(0, x.w, width)]


def widen(x, signed): return sext(x, 32) if signed else zext(x, 32)


def packed(fmt):
    """(lane width, signed) of a packed-integer format label: S32, U16x2, S8x4, ..."""
    width = int(fmt[1:].split('x')[0])
    return width, fmt[0] == 'S'


@helper
def pick(p, x, y, signed):
    """min(x, y) if p else max(x, y) (VIMNMX's PT/!PT), as 32-bit lane values."""
    less = slt(x, y) if signed else x < y
    more = slt(y, x) if signed else y < x
    return x if (p & less) | (lnot(p) & more) else y


@helper
def vector_result(i, fmt, relu, values):
    """The 32-bit packed result of per-lane values (32-bit, extended), RELU clamping negatives."""
    width, signed = packed(fmt)
    if relu and not signed: unmodelled('RELU of an unsigned format')
    if relu: values = [const(0, 32) if slt(v, 0) else v for v in values]
    return concat(*reversed([trunc(v, width) for v in values]))


def vector_formats(i, allowed):
    fmt = i.sel('fmt')
    if fmt not in allowed: unmodelled(f'{fmt} format')
    relu = i.sel('relu') == 'RELU' if i.has_operand('relu') else False
    for p in ('Pu', 'Pv'):
        if i.has_operand(p) and (i.kind(p) != 'predicate' or not i.is_true(p)): unmodelled(f'{p} output')   # vimnmx_pred: P0-P6, no PT
    if i.width('Rd') != 32: unmodelled('width')
    return fmt, relu


@family('VIMNMX', 'UVIMNMX')
def vimnmx(i):
    """Per lane: Pp ? min(Ra, Rb) : max(Ra, Rb), optionally RELU."""
    fmt, relu = vector_formats(i, ('S32', 'U32', 'S16x2', 'U16x2', 'S8x4', 'U8x4'))
    width, signed = packed(fmt)
    p = i.pred('Pp')
    xs, ys = lanes(i.value('Ra'), width), lanes(i.value('Rb|Sb'), width)
    return outputs(Rd=vector_result(i, fmt, relu, [pick(p, widen(x, signed), widen(y, signed), signed) for x, y in zip(xs, ys)]))


@family('VIMNMX3', 'UVIMNMX3')
def vimnmx3(i):
    """Per lane: the min (Pp) or max (!Pp) of Ra, Rb, Rc, optionally RELU."""
    fmt, relu = vector_formats(i, ('S32', 'U32', 'S16x2', 'U16x2'))
    width, signed = packed(fmt)
    p = i.pred('Pp')
    xs, ys, zs = (lanes(i.value(n), width) for n in ('Ra', 'Rb|Sb', 'Rc|Sc'))
    values = [pick(p, pick(p, widen(x, signed), widen(y, signed), signed), widen(z, signed), signed)
              for x, y, z in zip(xs, ys, zs)]
    return outputs(Rd=vector_result(i, fmt, relu, values))


@family('VIADDMNMX', 'UVIADDMNMX')
def viaddmnmx(i):
    """Per lane: Pp ? min(Ra + Rb, Rc) : max(Ra + Rb, Rc), the sum modular in the lane, optionally RELU."""
    fmt, relu = vector_formats(i, ('S32', 'U32', 'S16x2', 'U16x2'))
    width, signed = packed(fmt)
    p = i.pred('Pp')
    xs, ys, zs = (lanes(i.value(n), width) for n in ('Ra', 'Rb|Sb', 'Rc|Sc'))
    values = [pick(p, widen(x + y, signed), widen(z, signed), signed) for x, y, z in zip(xs, ys, zs)]
    return outputs(Rd=vector_result(i, fmt, relu, values))


@family('VABSDIFF4', 'UVABSDIFF4')
def vabsdiff4(i):
    """Per byte |Ra - Rb|: packed (.SIMD_MRG) or summed with Rc (.ACC)."""
    fmt = i.sel('fmt')
    if fmt not in ('S8', 'U8'): unmodelled(f'{fmt} format')
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('Pu output')
    signed = fmt == 'S8'
    xs, ys = (lanes(i.value(n), 8) for n in ('Ra', 'Rb|Sb'))
    diffs = []
    for x, y in zip(xs, ys):
        a, b = widen(x, signed), widen(y, signed)
        diffs.append(a - b if (sle(b, a) if signed else b <= a) else b - a)
    match i.sel('red'):
        case 'SIMD_MRG':
            if i.has_operand('Rc|Sc'): i.ignore('Rc|Sc')   # not read by the merge
            return outputs(Rd=concat(*reversed([trunc(d, 8) for d in diffs])))
        case 'ACC': return outputs(Rd=diffs[0] + diffs[1] + diffs[2] + diffs[3] + i.value('Rc|Sc'))
        case other: unmodelled(f'{other} reduction')


@family('IDP', 'UIDP')
def idp(i):
    """Rd = Rc + the dot product of Ra's lanes with Rb's bytes: .4A four bytes each, .2A two halves
    of Ra with bytes 0-1 (.LO) or 2-3 (.HI) of Rb."""
    mode = i.sel('mode')
    match mode:
        case '4A': aw, count, first = 8, 4, 0
        case '2A.LO': aw, count, first = 16, 2, 0
        case '2A.HI': aw, count, first = 16, 2, 2
        case other: unmodelled(f'{other} mode')
    afmt, bfmt = i.sel('SrcAFmt'), i.sel('SrcBFmt')
    if afmt not in ((('S16', 'U16') if aw == 16 else ('S8', 'U8'))) or bfmt not in ('S8', 'U8'): unmodelled('formats')
    if i.width('Rd') != 32: unmodelled('width')
    a, b = lanes(i.value('Ra'), aw), lanes(i.value('Rb|Sb'), 8)
    acc = i.value('Rc')
    for j in range(count):
        acc = acc + widen(a[j], afmt[0] == 'S') * widen(b[first + j], bfmt[0] == 'S')
    return outputs(Rd=acc)


@family('I2IP', 'UI2IP')
def i2ip(i):
    """Rd = Ra and Rb converted from S32 to a narrow integer (saturating with .SAT, negative to 0
    with .RELU) and packed: Rb in the low lane, Ra above it, and the rest of the word from C: its
    high half with .H1, its low bits otherwise (sub-byte .24/.28 forms take C unshifted, SM89 GPU)."""
    if i.sel('srcfmt') != 'S32': unmodelled('srcfmt')
    fmt = i.sel('dstfmt')
    width, signed = int(fmt[1:]), fmt[0] == 'S'
    half = i.sel('extract_dist_limited|extract_limited|extract_24|extract_28')
    if width <= 4 and half not in (str(32 - 2 * width), f'_{32 - 2 * width}'): unmodelled(f'{half} extract')
    if width >= 8 and half not in ('H0', 'H1'): unmodelled(f'{half} extract')
    if i.has_operand('satrelu'):
        mode = i.sel('satrelu')
        if mode not in ('SAT', 'SATRELU', 'RELU') and not mode.startswith('nosatrelu'): unmodelled(f'{mode} mode')
        sat, relu = mode in ('SAT', 'SATRELU'), mode in ('RELU', 'SATRELU')
    else: sat, relu = i.sel('SAT') == 'SAT', False   # SM75/SM80: /SATONLY
    if relu and not signed: unmodelled('RELU of an unsigned format')
    lo, hi = (-(1 << width - 1), (1 << width - 1) - 1) if signed else (0, (1 << width) - 1)

    def convert(x):
        if sat: x = smax(smin(x, const(hi, 32)), const(lo, 32))
        if relu: x = smax(x, const(0, 32))
        return trunc(x, width)

    packed = concat(convert(i.value('Ra')), convert(i.value('Rb|Sb')))
    if width == 16: return outputs(Rd=packed)
    c = i.value('Rc')
    rest = extract(c, 31, 16) if width == 8 and half == 'H1' else trunc(c, 32 - 2 * width)   # .24/.28: C unshifted
    return outputs(Rd=concat(rest, packed))


def byte_offset(label):
    if label not in ('B0', 'B1', 'B2', 'B3'): unmodelled(f'{label} byte')
    return 8 * int(label[1])


@family('P2R')
def p2r(i):
    """Rd = Ra with byte `insert` replaced by the predicates P0-P6 (PR) selected by the mask Rb."""
    at = byte_offset(i.sel('insert'))
    picked = zext(i.value('Pr') & trunc(i.value('Rb|Sb'), 7), 32)
    return outputs(Rd=(i.value('Ra') & ~const(0xff << at, 32)) | (picked << at))


@family('R2P')
def r2p(i):
    """P0-P6 (PR) = bits of byte `a_bsel` of Ra where the mask Rb is set; others keep their value."""
    at = byte_offset(i.sel('a_bsel'))
    mask, old = trunc(i.value('Rb|Sb'), 7), i.value('PR')
    return outputs(PR=(extract(i.value('Ra'), at + 6, at) & mask) | (old & ~mask))
