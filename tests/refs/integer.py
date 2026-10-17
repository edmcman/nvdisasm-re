"""Integer, SIMD-lane and predicate references, written from bit/lane definitions."""
from tests.refs.bits import MASK32, MASK64, lanes, pack, sext

COMPARISONS = ("F", "LT", "EQ", "LE", "GT", "NE", "GE", "T")


def compare(a, b, op):
    return {"F": False, "LT": a < b, "EQ": a == b, "LE": a <= b, "GT": a > b, "NE": a != b, "GE": a >= b, "T": True}[op]


def combine(value, predicate, bop):
    """ISETP/FSETP/DSETP predicate combiner."""
    value, predicate = bool(value), bool(predicate)
    return int({"AND": value and predicate, "OR": value or predicate, "XOR": value ^ predicate}[bop])


def isetp(a, b, comparison, bop, predicate, signed=False, low=None):
    """Integer compare and complementary predicate results. In .EX, equal high words
    inherit the low-word comparison (except the constant F/T comparisons)."""
    x, y = (sext(a, 32), sext(b, 32)) if signed else (a, b)
    condition = bool(low) if low is not None and a == b and comparison not in ('F', 'T') else compare(x, y, comparison)
    return combine(condition, predicate, bop), combine(not condition, predicate, bop)


def shf(a, count, c, fmt, direction, mode, half):
    """Select a word from a shifted {c,a} bit string (the existing SHF case definition).
    Enumerate source bit positions; a signed right shift extends bit 63."""
    width = {'U32':32, 'S32':32, 'U64':64, 'S64':64}[fmt]
    if direction not in ('L', 'R') or mode not in ('C', 'W') or half not in ('LO', 'HI'):
        raise ValueError('unknown SHF mode')
    count = min(count, width) if mode == 'C' else count % width
    data, base = (c << 32) | a, 32 if half == 'HI' else 0
    result = 0
    for bit in range(32):
        index = base + bit + (count if direction == 'R' else -count)
        value = (data >> index) & 1 if 0 <= index < 64 else int(index >= 64 and direction == 'R' and fmt.startswith('S') and bool(c >> 31))
        result |= value << bit
    return result


def iabs(value):
    """32-bit two's-complement absolute value; INT_MIN wraps to its original bits."""
    return abs(sext(value, 32)) & MASK32


def mov(value, width=32):
    """Copy a word or register pair without changing its bits."""
    if width not in (32, 64): raise ValueError('unsupported move width')
    return value & ((1 << width) - 1)


def brev(value): return sum(((value >> bit) & 1) << (31 - bit) for bit in range(32))


def flo(value, shift_amount, signed=False):
    """Highest set bit index (or the normalization shift with .SH); -1 for zero. Signed (.S32): of
    the complement for negative values, so the highest bit differing from the sign (SM89 GPU)."""
    if signed and value >> 31: value = ~value & MASK32
    if not value: return MASK32
    return (32 - value.bit_length()) if shift_amount else value.bit_length() - 1


def sgxt(value, width, wrap, signed):
    """SGXT: keep the low n bits of value and extend them from bit n-1 (sign or zero), by
    enumeration; n is width & 31 when wrapping, min(width, 32) when clamping (SM89 GPU)."""
    n = width & 31 if wrap else min(width, 32)
    fill = (value >> (n - 1)) & 1 if signed and n else 0
    return sum(((value >> i & 1) if i < n else fill) << i for i in range(32))


def bmsk(position, width, mode):
    """Mask of bits [position, position+width) by enumeration; W wraps both operands to 5 bits."""
    position &= MASK32; width &= MASK32
    if mode == "W": position &= 31; width &= 31
    elif mode != "C": raise ValueError(mode)
    return sum(1 << bit for bit in range(32) if position <= bit < position + width)


def prmt(a, sel, c, mode):
    """PRMT: pick bytes of {c, a} by the selector register `sel` (IDX: a nibble per result byte)."""
    data, out = a | (c << 32), 0
    for i in range(4):
        pick = ((sel >> (4 * i)) & 15) if mode == "IDX" else dict(
            F4E=(sel & 3) + i, B4E=((sel & 3) - i) & 7, RC8=sel & 3, RC16=((sel & 1) * 2) + (i & 1),
            ECL=max(i, sel & 3), ECR=min(i, sel & 3))[mode]
        byte = data >> (8 * (pick & 7)) & 255
        if mode == "IDX" and pick & 8: byte = 255 if byte & 128 else 0
        out |= byte << (8 * i)
    return out


def dot4(a, b, c, a_signed, b_signed):
    """IDP.4A: four byte products accumulated into c."""
    return (c + sum(x * y for x, y in zip(lanes(a, 8, a_signed), lanes(b, 8, b_signed)))) & MASK32


def dot2(a, b, c, a_signed, b_signed, high):
    """IDP.2A: two 16-bit A lanes times the low or high byte pair of B."""
    bs = lanes(b, 8, b_signed)[2:] if high else lanes(b, 8, b_signed)[:2]
    return (c + sum(x * y for x, y in zip(lanes(a, 16, a_signed), bs))) & MASK32


def vabsdiff4(a, b, c, signed, reduction):
    """Byte-lane |a-b|: packed (SIMD_MRG) or summed into c (ACC)."""
    diffs = [abs(x - y) for x, y in zip(lanes(a, 8, signed), lanes(b, 8, signed))]
    return pack(diffs, 8) if reduction == "SIMD_MRG" else (sum(diffs) + c) & MASK32


def vminmax(values, width, signed, is_min, relu=False):
    """Lane-wise min/max over packed words, with signed RELU."""
    def lane(xs):
        v = (min if is_min else max)(xs)
        return max(v, 0) if relu and signed else v
    return pack([lane(xs) for xs in zip(*(lanes(v, width, signed) for v in values))], width)


def vset_mask(a, b, width, signed, op):
    """VISET.BM: per lane all ones where `a op b` holds (op: an ICmpAll label), else zero."""
    return pack([-int(compare(x, y, op)) for x, y in zip(lanes(a, width, signed), lanes(b, width, signed))], width)


def viaddmnmx(a, b, c, width, signed, is_min, relu=False):
    """VIADDMNMX: lane sums modulo the lane width, then min/max with c."""
    sums = pack([x + y for x, y in zip(lanes(a, width), lanes(b, width))], width)
    return vminmax((sums, c), width, signed, is_min, relu)


def saturate(value, width, signed, sat, relu):
    lo, hi = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if signed else (0, (1 << width) - 1)
    if sat: value = max(lo, min(hi, value))
    if relu: value = max(0, value)
    return value & ((1 << width) - 1)


def i2ip(a, b, c, width, signed, sat, relu, extract):
    """I2IP: pack converted a (high) and b (low); sub-16-bit forms fill from c's selected half."""
    packed = (saturate(a, width, signed, sat, relu) << width) | saturate(b, width, signed, sat, relu)
    if width < 16: packed |= (((c >> 16) if extract == "H1" else c) & ((1 << (32 - 2 * width)) - 1)) << (2 * width)
    return packed & MASK32


def imad_wide(a, b, c, signed, negate=0, invert=0, carry_in=0):
    """IMAD.HI/.WIDE's unbounded sum of the 64-bit product and the 64-bit addend c, whose carry-out is
    bit 64: a negated c enters as ~c + 1, so -0 carries (SM89 GPU, like IADD3); .X's inverted c as ~c."""
    x, y = (sext(a, 32), sext(b, 32)) if signed else (a, b)
    if negate or invert: c = (c ^ MASK64) + negate
    return ((x * y) & MASK64) + c + carry_in



def imad(a, b, c, mode, signed, negate=0, invert=0, carry_in=0):
    """(Rd, Pu) of IMAD/UIMAD: LO keeps the low word of a*b + c (32-bit c, negated or .X-inverted, no
    carry-out); HI/WIDE keep the high word or all 64 bits of `imad_wide`, Pu its carry-out."""
    if mode == "LO": return (a * b + ((c ^ MASK32) + negate if negate or invert else c) + carry_in) & MASK32, None
    total = imad_wide(a, b, c, signed, negate, invert, carry_in)
    return (total >> 32 & MASK32 if mode == "HI" else total & MASK64), int(total > MASK64)

def carry(cls, fields, observe, vector):
    """IADD3/LEA/IMAD (.X) results and carry predicates for the patched carry kernel.
    observe: result | carry (Pu + 2*Pv) | pu | low | high."""
    a, b, c = vector; extended = "_x_" in cls
    p = (c & 1) ^ fields.get("Pp@not", 0) if extended else 0
    q = ((c >> 1) & 1) ^ fields.get("Pq@not", 0) if extended else 0
    flip = lambda x, name, width=MASK32: (width - x + (0 if extended else 1)) if fields.get(name) else x
    if cls.startswith("iadd3"):
        total = sum(flip(x, n + ("@invert" if extended else "@negate")) for x, n in zip((a, b, c), ("Ra", "Rb", "Rc"))) + p + q
        return {"pu": int(total > MASK32), "result": total & MASK32}.get(observe, int(total > MASK32) + 2 * int(total > 0x1ffffffff))
    if cls.startswith("lea"):
        high = "_hi_" in cls
        src = (sext(a, 32) if "_sx32" in cls else (c << 32) | a) if high else a
        shifted = ((src << fields["scaleU5"]) >> (32 if high else 0)) & MASK32
        suffix = "@invert" if extended else "@negate"
        total = flip(shifted, "Ra" + suffix) + flip(b, "Rb" + suffix) + p
        return total & MASK32 if observe == "result" else int(total > MASK32)
    signed = fields["fmt"] == "S32"
    if "_hi_" in cls or "_wide_" in cls:
        total = imad_wide(a, b, (b << 32) | c, signed, fields.get("Rc@negate", 0), fields.get("Rc@invert", 0), p)
    else:
        x, y = (sext(a, 32), sext(b, 32)) if signed else (a, b)
        total = ((x * y) & MASK64) + (c ^ MASK32 if fields.get("Rc@invert") else c) + p
    if observe == "pu": return int(total > MASK64)
    return (total >> 32) & MASK32 if observe == "high" or "_hi_" in cls else total & MASK32


def p2r(predicates, base, mask, byte):
    """P2R: bit i of the selected byte of base becomes P_i where mask bit i (0-7) is set; there is
    no P7, so mask bit 7 clears bit 7 (SM89 GPU). Higher mask bits are ignored."""
    for i in range(8):
        bit = 8 * byte + i
        if mask >> i & 1: base = base & ~(1 << bit) | (predicates >> i & 1 if i < 7 else 0) << bit
    return base & MASK32


def r2p(predicates, source, mask, byte):
    """R2P: P_i (0-6, packed) becomes bit i of the selected byte of source where mask bit i is set."""
    return sum((source >> 8 * byte + i & 1 if mask >> i & 1 else predicates >> i & 1) << i for i in range(7))
