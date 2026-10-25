"""Mixed F16/BF16 and binary32-pair references, shared with the retained dense corpus."""
from sass.semantics.fp import softfloat
from tests.refs.bits import nan32
from tests.refs.float import underflow_band, widen_half


def widen(bits, fmt):
    return (bits << 16) if fmt == "BF16" else widen_half(bits)


def ref_lanes(value, swizzle, absolute, negate, np_, half=None):
    """Lane 0 and 1 of a source as binary32 bits (None: a NaN half, whose payload isn't compared)."""
    if half:
        lo, hi = value & 0xffff, value >> 16
        lanes = [widen({"H1_H0": lo, "H0_H0": lo, "H1_H1": hi}[swizzle], half), widen({"H1_H0": hi, "H0_H0": lo, "H1_H1": hi}[swizzle], half)]
    elif swizzle == "F32": lanes = [value, value]
    else:
        lanes = [value & 0xffffffff, value >> 32]
        if swizzle == "F32x2.LO_HI": lanes.reverse()
    if None in lanes: return None
    lanes = [x & 0x7fffffff if absolute else x for x in lanes]
    return [lanes[0] ^ (negate << 31), lanes[1] ^ ((negate ^ np_) << 31)]


def lane_result(op, inputs, rnd, ftz, dst):
    """One lane: the binary32 result, or the exact value rounded RZ straight to a half format (FTZ:
    binary32 inputs and a F16 subnormal result flushed).
    None when a NaN or Ghidra's RN underflow band makes the comparison meaningless."""
    if any(nan32(x) for x in inputs) or rnd == "RN" and underflow_band(op, inputs, 32): return None
    if dst is None:
        r = softfloat.arithmetic(op, inputs, 32, rnd, ftz)
        return None if nan32(r) else r
    parts = [softfloat.unpack(x, 32, ftz) for x in inputs]
    if any(k != "finite" and k != "zero" for k, _, _ in parts): return None
    v = parts[0][2] + parts[1][2] if op == "add" else parts[0][2] * parts[1][2]
    if not v: return None   # the zero's sign: not this suite's question
    sign = int(v < 0)
    # F16 straight from the exact value; BF16 truncates binary32's RZ (BF16 is binary32's top half).
    return softfloat.pack(v, 32, "RZ", sign) >> 16 if dst == "BF16" else softfloat.pack(v, 16, "RZ", sign, ftz)


