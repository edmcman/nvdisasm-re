"""Integer families."""
from sass.semantics.bitvec import *
from sass.semantics.family import definition, family, helper, outputs, unmodelled


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


CW = {'W': 'wrap', 'C': 'clamp'}   # .W/.C: a count wrapped or clamped to the width


def cw(i):
    mode = i.select_fork('cw')
    if mode not in CW: unmodelled('cw selector')
    return CW[str(mode)]


@definition("bmsk.{mode}.b32")
def bit_mask(pos, width, mode):
    """PTX bmsk: `width` ones starting at bit `pos`; wrap takes both modulo 32, clamp clamps the width."""
    pos, width = (pos & 31, width & 31) if mode == 'wrap' else (pos, umin(width, 32))
    ones = const(0xffffffff, 32)
    return 0 if pos >= 32 else (ones << pos) & ~(ones << (pos + width))


@family('BMSK')
def bmsk(i):
    """A mask of `width` ones starting at bit `pos`; .W wraps both to 5 bits, .C clamps the width."""
    pos, width = i.value('Ra'), i.value('Rb|Sb')
    return outputs(Rd=bit_mask(pos, width, cw(i)))


@family('MOV', 'UMOV', 'MOV64IUR')
def mov(i):
    """Rd = the source (register, uniform register, immediate or constant), 32 or 64 bits. MOV.SPILL
    (SM100+) is taken as the same copy, as before the migration (no hardware evidence)."""
    if i.has_operand('indexURd|indexURb'): unmodelled('indexed register file')
    if i.has_operand('size') and i.select_fork('size') not in ('64', '_64'): unmodelled('size')
    if i.has_operand('nonconformity') and i.select_fork('nonconformity') != 'SPILL': unmodelled('nonconformity')
    return outputs(Rd=resize(i.value('Sb|Sa|Ra|Rb'), i.width('Rd')))


@family('R2UR')
def r2ur(i):
    """URd = Ra (the executing thread's value). Pu (whether Ra was warp-uniform), .OR and the
    SM100+ fill/broadcast forms are unmodelled."""
    if not i.is_true('Pu'): unmodelled('predicate output')
    return outputs(Rd=i.value('Ra'))


@family('SEL', 'USEL')
def sel(i):
    """Rd = Pp ? Ra : Rb."""
    if i.has_operand('size') and i.select_fork('size') not in ('64', '_64'): unmodelled('size')
    w = i.width('Rd')
    return outputs(Rd=resize(i.value('Ra'), w) if i.pred('Pp') else resize(i.value('Rb|Sb'), w))


@definition(lambda a: (f"abs.s{a['x'].w}", ["x"]))
def absolute(x):
    """PTX abs: the two's complement magnitude (|-2^31| = -2^31)."""
    return iabs(x)


@family('IABS')
def iabs_(i):
    """Rd = |source| (two's complement: |-2^31| = -2^31)."""
    if i.width('Rd') != 32: unmodelled('width')
    return outputs(Rd=absolute(i.value('Sb|Rb|Ra')))


@family('IADD', 'VIADD')
def iadd(i):
    """Rd = Ra + Rb, 32 bits, no carry out (Pu = PT). The SM100+ .X/.XOR/.64 classes are
    unmodelled (their class selectors have no default)."""
    if i.width('Rd') != 32: unmodelled('width')   # IADD.64 (SM120)
    if i.has_operand('fmt') and i.select_fork('fmt') not in ('32', '_32', 'U32', 'S32'): unmodelled(f"{i.select_fork('fmt')} format")
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('carry out')
    return outputs(Rd=i.value('Ra|Sa') + i.value('Rb|Sb'))


@family('LEA', 'ULEA')
def lea(i):
    """Rd = (Ra << scale) + Rb, carry Pu at 2^32; .HI shifts Rc:Ra (or sext(Ra), .SX32) and takes the
    high word. Ra's modifier applies to the shifted word; .X adds Pp and inverts instead of negating."""
    if i.has_operand('Pr'): unmodelled('Pr')
    x = i.has_operand('X')
    a, n = i.value('Ra', modifiers=False), zext(i.value('scaleU5'), 64)
    match i.select_fork('hilo'):
        case 'LO': word = trunc(zext(a, 64) << n, 32)
        case 'HI':
            if i.has_operand('sx32'):
                if i.select_fork('sx32') != 'SX32': unmodelled('HI without SX32 or a high word')
                wide = sext(a, 64)
            else: wide = concat(i.value('Rc|Sc'), a)
            word = trunc((wide << n) >> 32, 32)
    total = addend(i, 'Ra', word, x) + addend(i, 'Rb|Sb', i.value('Rb|Sb', modifiers=False), x)
    if x: total = total + zext(i.pred('Pp'), 64)
    return outputs(Rd=trunc(total, 32), Pu=total >= 1 << 32)


@definition(lambda a: (f"mul.wide.{'s' if a['signed'] else 'u'}32", ["a", "b"]))
def wide_product(a, b, signed):
    """PTX mul.wide: the 64-bit product of two 32-bit values, signed or not."""
    ext = sext if signed else zext
    return ext(a, 64) * ext(b, 64)


@family('IMAD', 'UIMAD')
def imad(i):
    """Rd = Ra * Rb + Rc (.X: + Pp). .LO keeps the low word; .HI/.WIDE form the 64-bit product
    (signed for S32) plus Rc and keep its high word or all of it, Pu = unsigned overflow of that
    sum. A narrow (32-bit, non-pair) addend of .HI is added to the high word instead (SM89 GPU)."""
    if i.has_operand('Pq'): unmodelled('Pq')
    if i.has_operand('x') and i.select_fork('x') not in ('nox', 'NOX'): unmodelled('x selector')
    fmt = i.select_fork('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    x = i.has_operand('X')
    a, b, c = i.value('Ra|Sa'), i.value('Rb|Sb'), i.value('Rc|Sc')
    if max(a.w, b.w, c.w) > 64: unmodelled('wide source')
    cin = zext(i.pred('Pp'), 64) if x else const(0, 64)
    mode = i.select_fork('wide')
    if mode == 'LO': return outputs(Rd=a * b + resize(c, 32) + trunc(cin, 32))
    product = wide_product(a, b, fmt == 'S32')
    # A negated addend enters as ~c + 1, so -0 carries (SM89 GPU, like IADD3).
    negated = i.operand_negated('Rc|Sc')
    if negated: c, cin = i.value('Rc|Sc', modifiers=False), const(1, 64)
    addend = lambda w: ~resize(c, w) if negated else resize(c, w)
    pair = i.kind('Rc|Sc') in ('register', 'uniform') and c.w == 64
    if mode == 'HI' and not pair and c.w <= 32:
        full = zext(trunc(product >> 32, 32), 64) + zext(addend(32), 64) + cin
        return outputs(Rd=trunc(full, 32), Pu=full >= 1 << 32)
    summed = product + addend(64)
    full = summed + cin
    overflow = (summed < product) | (full < summed) if x or negated else summed < product
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
    if i.select_fork('lut') != 'LUT': unmodelled('lut')
    rd = truth_table(i.static('imm8'), [i.value('Ra|Sa'), i.value('Rb|Sb'), i.value('Rc|Sc')])
    pu = rd != 0
    if i.has_operand('Pp'):
        match i.select_fork('pop'):
            case 'POR': pu = pu | i.pred('Pp')
            case 'PAND': pu = pu & i.pred('Pp')
    return outputs(Rd=rd, Pu=pu)


@definition("lut3")
def lut3(table, p, q, r):
    """The one-bit entry of the 8-bit truth table at index 4p+2q+r, for one-bit predicates p, q, r."""
    index = (zext(p, 8) << 2) | (zext(q, 8) << 1) | zext(r, 8)
    return extract(table >> index, 0, 0)


@family('PLOP3', 'UPLOP3', classes=lambda k: 'reg__' not in k.name)
def plop3(i):
    """Pu, Pv = the functions of Pp, Pq, Pr with truth tables uimm8, vimm8 (bit 4p+2q+r, as LOP3).
    Both look their bit up: enumerating Pu's 256 tables into boolean expressions cost ~30 KB of
    SLEIGH per class (SM90 was near the .sla cap), and both jointly 65536 rows. The register-source
    classes (a register's sign as an input) are unmodelled."""
    if i.select_fork('lut') != 'LUT': unmodelled('lut')
    xs = [i.pred('Pp'), i.pred('Pq'), i.pred('Pr')]
    bit = lambda table: lut3(trunc(i.imm(table), 8), *xs)
    return outputs(Pu=bit('uimm8'), Pv=bit('vimm8'), order=('Pv', 'Pu'))   # aliased: Pu wins (SM89)


PRMT_MODES = ('IDX', 'F4E', 'B4E', 'RC8', 'RC16', 'ECL', 'ECR')


def prmt_bits(mode): return 4 if mode == 'IDX' else 1 if mode == 'RC16' else 2


def picked_byte(a, b, mode, j, v):
    """Byte j of a permute by the selector value v: which of b:a's bytes (and IDX's sign replication)."""
    match mode:
        case 'IDX': p = v & 7
        case 'F4E': p = v + j
        case 'B4E': p = (v - j) & 7
        case 'RC8': p = v
        case 'RC16': p = v * 2 + (j & 1)
        case 'ECL': p = max(v, j)
        case 'ECR': p = min(v, j)
    byte = extract(a if p < 4 else b, (p & 3) * 8 + 7, (p & 3) * 8)
    return sext(extract(byte, 7, 7), 8) if mode == 'IDX' and v & 8 else byte


@definition(lambda a: (f"prmt.b32{'' if a['mode'] == 'IDX' else '.' + a['mode'].lower()}", ["a", "s", "b"]))
def permute(a, s, b, mode):
    """PTX prmt.b32: four bytes picked from b:a (a is bytes 0-3) by the selector s, per mode: the
    default (IDX) takes nibble j for byte j, its bit 3 replicating the byte's sign; f4e/b4e forward
    or backward from s & 3; rc8/rc16 replicate a byte or half; ecl/ecr clamp at the edges.

    s: a 32-bit selector. Constants and zero-extended decode-time choices select bytes statically;
    other expressions, including parameters, use the run-time expansion."""
    # Extraction unwraps the immediate's zero extension and, for IDX, the concatenation of its
    # four independent nibble choices. Width alone cannot identify a decode-time choice.
    codes = [extract(s, 4 * j + 3, 4 * j) if mode == 'IDX' else extract(s, prmt_bits(mode) - 1, 0)
             for j in range(4)]
    if all(is_const(code) or (code.op == 'sel' and all(is_const(t) for _, t in code.args[1])) for code in codes):
        return concat(*reversed([chosen(code, lambda v: picked_byte(a, b, mode, j, v)) for j, code in enumerate(codes)]))
    data = concat(b, a)
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
        byte = trunc(data >> (zext(p & 7, 64) << 3), 8)
        if mode == 'IDX': byte = sext(extract(byte, 7, 7), 8) if (p & 8) != 0 else byte   # replicate the sign bit
        picked.append(byte)
    return concat(*reversed(picked))


@family('PRMT', 'UPRMT')
def prmt(i):
    """Rd = four bytes picked from Rc:Ra (Ra is bytes 0-3) by the selector Rb, per mode. IDX: nibble
    j of Rb picks byte j, bit 3 replicating the picked byte's sign; F4E/B4E: forward/backward
    from Rb & 3; RC8/RC16: replicate a byte/half; ECL/ECR: edge clamp left/right (SM89 GPU). UPRMT
    is IDX only."""
    if any(i.width(n) != 32 for n in ('Rd', 'Ra|Sa', 'Rb|Sb', 'Rc|Sc')): unmodelled('width')
    # Make encoded zero registers visible before building expressions, so their arithmetic folds.
    for source in ('Ra|Sa', 'Rc|Sc'):
        if i.kind(source) in ('register', 'uniform'): i.is_zero(source)
    a, b = i.value('Ra|Sa'), i.value('Rc|Sc')
    mode = str(i.select_fork('pmode|idx'))   # UPRMT spells its only mode /IDXOnly("IDX"):idx
    if mode not in PRMT_MODES: unmodelled(f'{mode} mode')
    if i.kind('Rb|Sb') == 'immediate':
        # Select bytes at decode time. Keeping an immediate as a run-time shift amount leaves
        # shifts by zero and concatenation with RZ in raw p-code; independent small tables avoid
        # a 65,536-row table over the whole selector. The permute reads them as selector values.
        bits = prmt_bits(mode)
        code = lambda lo: i.select_bits('Rb|Sb', lo, bits, {v: const(v, bits) for v in range(1 << bits)})
        s = concat(*(code(4 * j) for j in reversed(range(4)))) if mode == 'IDX' else code(0)
        return outputs(Rd=permute(a, zext(s, 32), b, mode))
    return outputs(Rd=permute(a, i.value('Rb|Sb'), b, mode))


@definition(lambda a: (f"shf.{a['dir'].lower()}.{a['mode']}{'.hi' if a['hi'] else ''}.{a['fmt'].lower()}", ["lo", "hi_word", "n"]))
def funnel(lo, hi_word, n, fmt, mode, dir, hi):
    """PTX shf: a word of hi_word:lo shifted left or right (arithmetic for signed formats) by n,
    wrapped or clamped to the format width; .hi takes the high word."""
    limit = 32 if fmt.endswith('32') else 64
    count = n & (limit - 1) if mode == 'wrap' else umin(n, limit)
    base, count = concat(hi_word, lo), zext(count, 64)
    r = base << count if dir == 'L' else (sar(base, count) if fmt.startswith('S') else base >> count)
    return trunc(r >> 32 if hi else r, 32)


@family('SHF', 'USHF')
def shf(i):
    """Rd = a word of Rc:Ra shifted left or right (arithmetic for signed formats) by Rb, wrapped
    (.W) or clamped (.C) to the format width; .HI takes the high word."""
    fmt = str(i.select_fork('fmt'))
    if fmt not in ('U32', 'S32', 'U64', 'S64'): unmodelled(f'{fmt} format')
    n = i.value('Rb|Sb')
    mode = cw(i)
    lo, hi = i.value('Ra|Sa'), i.value('Rc|Sc')
    direction, half = str(i.select_fork('dir')), str(i.select_fork('hilo'))
    if direction not in ('L', 'R'): unmodelled(f'{direction} direction')
    if half not in ('LO', 'HI'): unmodelled(f'{half} half')
    return outputs(Rd=funnel(lo, hi, n, fmt, mode, direction, half == 'HI'))


def word_source(i):
    """The single 32-bit source of BREV/POPC/FLO (register, uniform, immediate, constant or CX)."""
    if i.width('Rd') != 32 or i.width('Rb|Sb') != 32: unmodelled('width')
    return i.value('Rb|Sb')


@definition("brev.b32")
def reversed_bits(x):
    """PTX brev.b32: the bits in reverse order."""
    for shift, m in ((1, 0x55555555), (2, 0x33333333), (4, 0x0f0f0f0f), (8, 0x00ff00ff)):
        x = ((x >> shift) & m) | ((x & m) << shift)
    return (x >> 16) | (x << 16)


@family('BREV', 'UBREV')
def brev(i):
    """Rd = the source's bits in reverse order."""
    return outputs(Rd=reversed_bits(word_source(i)))


@definition("popc.b32")
def population(x):
    """PTX popc.b32: the number of set bits."""
    x = x - ((x >> 1) & 0x55555555)
    x = (x & 0x33333333) + ((x >> 2) & 0x33333333)
    x = (x + (x >> 4)) & 0x0f0f0f0f
    x = x + (x >> 8)
    x = x + (x >> 16)
    return x & 0x3f


@family('POPC', 'UPOPC')
def popc(i):
    """Rd = the number of set bits of the source."""
    return outputs(Rd=population(word_source(i)))


def leading_zeros(x):
    """The leading zero bits of a nonzero 32-bit x (31 for 0). A binary search, branch-free: each
    step shifts by its width when the top part is clear (a 1-bit test scaled to the shift, not a
    choice between two values)."""
    n = const(0, 32)
    for k, m in ((4, 0xffff0000), (3, 0xff000000), (2, 0xf0000000), (1, 0xc0000000), (0, 0x80000000)):
        s = zext((x & m) == 0, 32) << k
        n, x = n | s, x << s
    return n


@definition(lambda a: (f"bfind{'.shiftamt' if a['shift'] else ''}.{'s' if a['signed'] else 'u'}32", ["x"]))
def find_leading(x, signed, shift):
    """PTX bfind: the index of the most significant set bit (.shiftamt: its distance from bit 31),
    or -1 for 0; signed searches the complement of a negative value (the highest bit differing from
    the sign)."""
    x0 = (~x if slt(x, 0) else x) if signed else x
    n = leading_zeros(x0)
    return const(-1, 32) if x0 == 0 else (n if shift else 31 - n)


@family('FLO', 'UFLO')
def flo(i):
    """Rd = the index of the most significant set bit (.SH: its distance from bit 31), or -1 for 0.
    .S32 searches the complement of a negative value: the highest bit differing from the sign
    (SM89 GPU, bfind.s32)."""
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('Pu')
    fmt = i.select_fork('fmt')
    if fmt not in ('S32', 'U32', '32'): unmodelled(f'{fmt} format')
    sh = str(i.select_fork('sh'))
    if sh not in ('nosh', 'SH'): unmodelled(f'{sh}')
    return outputs(Rd=find_leading(word_source(i), fmt == 'S32', sh == 'SH'))


@definition(lambda a: (f"szext.{a['mode']}.{'s' if a['signed'] else 'u'}32", ["a", "width"]))
def extended(a, width, mode, signed):
    """PTX szext: the low n bits of a extended from bit n-1 (sign or zero), n the width wrapped
    (modulo 32) or clamped (to 32); n = 0 gives 0 (SM89 GPU)."""
    n = width & 31 if mode == 'wrap' else umin(width, 32)
    shift = (const(32, 32) - n) & 31
    extended = sar(a << shift, shift) if signed else (a << shift) >> shift
    return ite(n == 0, const(0, 32), extended)


@family('SGXT', 'USGXT')
def sgxt(i):
    """Rd = the low n bits of Ra extended from bit n-1 (sign for S32, zero for U32); n is Rb & 31
    for .W, min(Rb, 32) for .C, and n = 0 gives 0 (SM89 GPU)."""
    if i.width('Rd') != 32: unmodelled('width')
    a, width = i.value('Ra'), i.value('Rb|Sb')
    mode = cw(i)
    fmt = str(i.select_fork('fmt'))
    if fmt not in ('S32', 'U32', '32'): unmodelled('fmt selector')
    return outputs(Rd=extended(a, width, mode, fmt == 'S32'))


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
    fmt = i.select_fork('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    a, b = i.value('Ra|Sa'), i.value('Rb|Sb')
    if a.w != 32 or b.w != 32: unmodelled('width')
    comparison = i.select_fork('icmp')
    cond = compare(comparison, a, b, fmt == 'S32')
    if i.has_operand('ex') and i.select_fork('ex') == 'EX':
        if not i.has_operand('Pr'): unmodelled('EX without Pr')
        low = i.pred('Pr')
        if comparison not in ('F', 'T'): cond = cond if a != b else low
    elif i.has_operand('Pr'): i.ignore('Pr')
    p, bop = i.pred('Pp'), i.select_fork('bop')
    return outputs(Pu=boolean(bop, cond, p), Pv=boolean(bop, lnot(cond), p),
                   order=('Pv', 'Pu'))  # equal destinations: Pu wins (SM89)


@definition(lambda a: (f"imnmx.{'s' if a['signed'] else 'u'}32", ["p", "a", "b"]))
def select_minmax(p, a, b, signed):
    """IMNMX with a register predicate: min.s32/u32 when it holds, else max."""
    less = slt(a, b) if signed else a < b
    return a if less == p else b


@family('IMNMX', 'UIMNMX')
def imnmx(i):
    """Rd = Pp ? min(Ra, Rb) : max(Ra, Rb), signed for S32. SM107/SM120's forms with predicate
    outputs Pu/Pv and a second input Pq are unmodelled (Pq's meaning is unknown, even as PT)."""
    if any(i.has_operand(p) for p in ('Pu', 'Pv', 'Pq')): unmodelled('Pu/Pv/Pq predicates')
    fmt = i.select_fork('fmt')
    if fmt not in ('U32', 'S32'): unmodelled(f'{fmt} format')
    a, b = i.value('Ra|Sa'), i.value('Rb|Sb')
    if a.w != 32 or b.w != 32 or i.width('Rd') != 32: unmodelled('width')
    p, signed = i.pred('Pp'), fmt == 'S32'
    return outputs(Rd=pick(p, a, b, signed) if is_const(p) else select_minmax(p, a, b, signed))


def lanes(x, width):
    """x's lanes of `width` bits, lowest first."""
    return [extract(x, lo + width - 1, lo) for lo in range(0, x.w, width)]


def widen(x, signed): return sext(x, 32) if signed else zext(x, 32)


def packed(fmt):
    """(lane width, signed) of a packed-integer format label: S32, U16x2, S8x4, ..."""
    width = int(fmt[1:].split('x')[0])
    return width, fmt[0] == 'S'


@definition(lambda a: ((f"{'min' if value(a['p']) else 'max'}.{'s' if a['signed'] else 'u'}{a['x'].w}", ["x", "y"]) if is_const(a["p"])
                       else (f"vimnmx.{'s' if a['signed'] else 'u'}{a['x'].w}", ["p", "x", "y"])))
def pick(p, x, y, signed):
    """Integer min (predicate) or max, signed or not (VIMNMX's lanes, extended to 32 bits)."""
    less = slt(x, y) if signed else x < y
    more = slt(y, x) if signed else y < x
    return x if (p & less) | (lnot(p) & more) else y


@definition(lambda a: (f"relu.s{a['v'].w}", ["v"]))
def rectified(v):
    """ReLU (PTX .relu): a negative value becomes 0."""
    return const(0, 32) if slt(v, 0) else v


@helper
def vector_result(i, fmt, relu, values):
    """The 32-bit packed result of per-lane values (32-bit, extended), RELU clamping negatives."""
    width, signed = packed(fmt)
    if relu and not signed: unmodelled('RELU of an unsigned format')
    if relu: values = [rectified(v) for v in values]
    return concat(*reversed([trunc(v, width) for v in values]))


def vector_formats(i, allowed):
    fmt = i.select_fork('fmt')
    if fmt not in allowed: unmodelled(f'{fmt} format')
    relu = i.select_fork('relu') == 'RELU' if i.has_operand('relu') else False
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


@family('VISET')
def viset(i):
    """Per lane (S/U 32, 16x2, 8x4): all ones where Ra icmp Rb holds, else zero. .BM (the md's only
    mode, read as a bit mask) is inferred: no compiler emits VISET (SM107; ptxas 13.4 has no such
    opcode)."""
    fmt, _ = vector_formats(i, ('S32', 'U32', 'S16x2', 'U16x2', 'S8x4', 'U8x4'))
    if i.select_fork('bm') != 'BM': unmodelled('bm')
    width, signed = packed(fmt)
    icmp = i.select_fork('icmp')
    xs, ys = lanes(i.value('Ra'), width), lanes(i.value('Rb|Sb'), width)
    return outputs(Rd=concat(*reversed([-zext(compare(icmp, x, y, signed), width) for x, y in zip(xs, ys)])))


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
    """Per lane: Pp ? min(Ra + Rb, Rc) : max(Ra + Rb, Rc), the sum and B negation modular in each lane, optionally RELU."""
    fmt, relu = vector_formats(i, ('S32', 'U32', 'S16x2', 'U16x2'))
    width, signed = packed(fmt)
    p = i.pred('Pp')
    xs, zs = (lanes(i.value(n), width) for n in ('Ra', 'Rc|Sc'))
    ys = lanes(i.value('Rb|Sb', modifiers=False), width)
    if i.operand_negated('Rb|Sb'): ys = [-y for y in ys]
    values = [pick(p, widen(x + y, signed), widen(z, signed), signed) for x, y, z in zip(xs, ys, zs)]
    return outputs(Rd=vector_result(i, fmt, relu, values))


@definition(lambda a: (f"vabsdiff4.{'s' if a['signed'] else 'u'}8.{'add' if a['c'] is not None else 'merge'}", ["a", "b", *(["c"] if a["c"] is not None else [])]))
def byte_differences(a, b, signed, c=None):
    """PTX vabsdiff4: per byte |a - b|, packed (merge), or summed with c (add)."""
    diffs = []
    for x, y in zip(lanes(a, 8), lanes(b, 8)):
        u, v = widen(x, signed), widen(y, signed)
        diffs.append(u - v if (sle(v, u) if signed else v <= u) else v - u)
    if c is None: return concat(*reversed([trunc(d, 8) for d in diffs]))
    return diffs[0] + diffs[1] + diffs[2] + diffs[3] + c


@family('VABSDIFF4', 'UVABSDIFF4')
def vabsdiff4(i):
    """Per byte |Ra - Rb|: packed (.SIMD_MRG) or summed with Rc (.ACC)."""
    fmt = i.select_fork('fmt')
    if fmt not in ('S8', 'U8'): unmodelled(f'{fmt} format')
    if i.has_operand('Pu') and not i.is_true('Pu'): unmodelled('Pu output')
    signed = fmt == 'S8'
    a, b = i.value('Ra'), i.value('Rb|Sb')
    match i.select_fork('red'):
        case 'SIMD_MRG':
            if i.has_operand('Rc|Sc'): i.ignore('Rc|Sc')   # not read by the merge
            return outputs(Rd=byte_differences(a, b, signed))
        case 'ACC': return outputs(Rd=byte_differences(a, b, signed, i.value('Rc|Sc')))
        case other: unmodelled(f'{other} reduction')


def dp_name(a):
    types = f"{'s' if a['asigned'] else 'u'}32.{'s' if a['bsigned'] else 'u'}32"
    return (f"dp4a.{types}" if a["mode"] == "4A" else f"dp2a.{a['mode'][-2:].lower()}.{types}"), ["a", "b", "c"]


@definition(dp_name)
def dot_product(a, b, c, mode, asigned, bsigned):
    """PTX dp4a/dp2a: c plus the dot product of a's lanes (four bytes, or two halves) with b's bytes
    (all four, or bytes 0-1 for .lo and 2-3 for .hi)."""
    aw, count, first = {'4A': (8, 4, 0), '2A.LO': (16, 2, 0), '2A.HI': (16, 2, 2)}[mode]
    xs, ys = lanes(a, aw), lanes(b, 8)
    acc = c
    for j in range(count):
        acc = acc + widen(xs[j], asigned) * widen(ys[first + j], bsigned)
    return acc


@family('IDP', 'UIDP')
def idp(i):
    """Rd = Rc + the dot product of Ra's lanes with Rb's bytes: .4A four bytes each, .2A two halves
    of Ra with bytes 0-1 (.LO) or 2-3 (.HI) of Rb."""
    mode = i.select_fork('mode')
    match mode:
        case '4A': aw, count, first = 8, 4, 0
        case '2A.LO': aw, count, first = 16, 2, 0
        case '2A.HI': aw, count, first = 16, 2, 2
        case other: unmodelled(f'{other} mode')
    afmt, bfmt = i.select_fork('SrcAFmt'), i.select_fork('SrcBFmt')
    if afmt not in ((('S16', 'U16') if aw == 16 else ('S8', 'U8'))) or bfmt not in ('S8', 'U8'): unmodelled('formats')
    if i.width('Rd') != 32: unmodelled('width')
    return outputs(Rd=dot_product(i.value('Ra'), i.value('Rb|Sb'), i.value('Rc'), str(mode), afmt[0] == 'S', bfmt[0] == 'S'))


def clamped(x, width, signed):
    """The 32-bit x clamped to the range of a width-bit integer."""
    lo, hi = (-(1 << width - 1), (1 << width - 1) - 1) if signed else (0, (1 << width) - 1)
    return smax(smin(x, const(hi, 32)), const(lo, 32))


@definition(lambda a: (f"cvt.sat.{'s' if a['signed'] else 'u'}{a['width']}.s32", ["b"]))
def saturated_narrow(b, width, signed):
    """PTX cvt.sat from S32 to a narrow integer: b clamped to its range, held as a 32-bit value
    (sign-extended for S8/S16, SM89 GPU)."""
    return clamped(b, width, signed)


@family('I2I')
def i2i(i):
    """Rd = Rb converted from S32 to U8/S8/U16/S16 with saturation, as a 32-bit value (SM89 GPU)."""
    if i.select_fork('srcfmt') != 'S32' or i.select_fork('SAT') != 'SAT': unmodelled('format')
    fmt = i.select_fork('dstfmt')
    return outputs(Rd=saturated_narrow(i.value('Rb|Sb|URb'), int(fmt[1:]), fmt[0] == 'S'))


def pack_name(a):
    mods = ".sat" * a["sat"] + ".relu" * a["relu"] + ".h1" * a["high"]
    return f"cvt.pack{mods}.{'s' if a['signed'] else 'u'}{a['width']}.s32", ["a", "b", *(["c"] if a["c"] is not None else [])]


@definition(pack_name)
def packed_pair(a, b, c, width, signed, sat, relu, high):
    """PTX cvt.pack: two S32 values converted to a narrow integer (saturating with .sat, negative to
    0 with .relu) and packed, b in the low lane and a above it, the rest of the word from c: its
    high half with .h1, its low bits otherwise (sub-byte forms take c unshifted, SM89 GPU)."""
    def convert(x):
        if sat: x = clamped(x, width, signed)
        if relu: x = smax(x, const(0, 32))
        return trunc(x, width)

    packed = concat(convert(a), convert(b))
    if c is None: return packed
    return concat(extract(c, 31, 16) if high else trunc(c, 32 - 2 * width), packed)   # .24/.28: c unshifted


@family('I2IP', 'UI2IP')
def i2ip(i):
    """Rd = Ra and Rb converted from S32 to a narrow integer (saturating with .SAT, negative to 0
    with .RELU) and packed: Rb in the low lane, Ra above it, and the rest of the word from C: its
    high half with .H1, its low bits otherwise (sub-byte .24/.28 forms take C unshifted, SM89 GPU)."""
    if i.select_fork('srcfmt') != 'S32': unmodelled('srcfmt')
    fmt = i.select_fork('dstfmt')
    width, signed = int(fmt[1:]), fmt[0] == 'S'
    half = i.select_fork('extract_dist_limited|extract_limited|extract_24|extract_28')
    if width <= 4 and half not in (str(32 - 2 * width), f'_{32 - 2 * width}'): unmodelled(f'{half} extract')
    if width >= 8 and half not in ('H0', 'H1'): unmodelled(f'{half} extract')
    if i.has_operand('satrelu'):
        mode = i.select_fork('satrelu')
        if mode not in ('SAT', 'SATRELU', 'RELU') and not mode.startswith('nosatrelu'): unmodelled(f'{mode} mode')
        sat, relu = mode in ('SAT', 'SATRELU'), mode in ('RELU', 'SATRELU')
    else: sat, relu = i.select_fork('SAT') == 'SAT', False   # SM75/SM80: /SATONLY
    if relu and not signed: unmodelled('RELU of an unsigned format')
    a, b = i.value('Ra'), i.value('Rb|Sb')
    return outputs(Rd=packed_pair(a, b, None if width == 16 else i.value('Rc'), width, signed, sat, relu, width == 8 and half == 'H1'))


def byte_offset(label):
    if label not in ('B0', 'B1', 'B2', 'B3'): unmodelled(f'{label} byte')
    return 8 * int(label[1])


@family('P2R')
def p2r(i):
    """Rd = Ra with the bits of byte `insert` selected by the mask's low byte replaced by P0-P6 (PR);
    there is no P7, so mask bit 7 clears bit 7 (SM89 GPU)."""
    at = byte_offset(i.select_fork('insert'))
    mask = zext(trunc(i.value('Rb|Sb'), 8), 32) << at
    return outputs(Rd=(i.value('Ra') & ~mask) | ((zext(i.value('Pr'), 32) << at) & mask))


@family('R2P')
def r2p(i):
    """P0-P6 (PR) = bits of byte `a_bsel` of Ra where the mask Rb is set; others keep their value."""
    at = byte_offset(i.select_fork('a_bsel'))
    mask, old = trunc(i.value('Rb|Sb'), 7), i.value('PR')
    return outputs(PR=(extract(i.value('Ra'), at + 6, at) & mask) | (old & ~mask))


@family("UP2UR")
def up2ur(i):
    """URd = URa with bit k replaced by UPk for each bit k of the immediate mask's low byte; there is
    no UP7, so mask bit 7 clears bit 7 (as P2R, SM89 GPU)."""
    if i.cls != "up2ur__Imm": unmodelled("register mask")
    if i.select_fork("insert") != "B0": unmodelled("insert byte")
    i.select_fork("UPR")   # the one-label source operand naming the uniform predicate file
    mask = i.static_low("Sb", 7)
    v = i.value("URa", modifiers=False) & ~zext(trunc(i.value("Sb"), 8), 32)
    for k in range(7):
        if mask >> k & 1: v = v | (zext(reg("UP", k, 1, element=1), 32) << k)
    return outputs(URd=v)
