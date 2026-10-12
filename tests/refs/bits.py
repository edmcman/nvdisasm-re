"""Bit-pattern helpers: float views, sign extension and packed lanes."""
import math, struct

MASK32, MASK64 = (1 << 32) - 1, (1 << 64) - 1


def f32(bits): return struct.unpack("<f", (bits & MASK32).to_bytes(4, "little"))[0]
def f64(bits): return struct.unpack("<d", (bits & MASK64).to_bytes(8, "little"))[0]
def f16(bits): return struct.unpack("<e", (bits & 0xffff).to_bytes(2, "little"))[0]
def bf16(bits): return f32((bits & 0xffff) << 16)
def bits32(value): return struct.unpack("<I", struct.pack("<f", value))[0]
def bits64(value): return struct.unpack("<Q", struct.pack("<d", value))[0]


def f16_bits(value):
    try: return int.from_bytes(struct.pack("<e", value), "little")
    except OverflowError: return (0xfc00 if value < 0 else 0x7c00)


def nan32(bits): return bits & 0x7f800000 == 0x7f800000 and bits & 0x7fffff != 0
def nan16(bits): return bits & 0x7c00 == 0x7c00 and bits & 0x3ff != 0
def nan_bf16(bits): return bits & 0x7f80 == 0x7f80 and bits & 0x7f != 0
def is_nan(x): return math.isnan(x)


def sext(value, width):
    value &= (1 << width) - 1
    return value - (1 << width) if value >> (width - 1) else value


def lanes(value, width, signed=False, count=None):
    """Split `value` into `count` (default 32/width) lanes, low lane first."""
    out = [(value >> (width * i)) & ((1 << width) - 1) for i in range(count or 32 // width)]
    return [sext(x, width) for x in out] if signed else out


def pack(values, width):
    return sum((v & ((1 << width) - 1)) << (width * i) for i, v in enumerate(values))


def ftz32(bits):
    """Flush a subnormal fp32 bit pattern to signed zero."""
    return bits & 0x80000000 if bits & 0x7f800000 == 0 else bits


def ftz16(bits):
    return bits & 0x8000 if bits & 0x7c00 == 0 else bits
