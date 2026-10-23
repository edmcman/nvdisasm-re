"""The C++ runtime from Python through ctypes: primitive calls (`sass.semantics.runtime`) and the
floating-point and MUFU test hooks of `primitives.cpp`."""
import ctypes
from functools import cache
from sass.emulate import build

LANES = 32
MESSAGE = 512
ROUNDING = ("RN", "RZ", "RP", "RM")
FP_OPS = ("add", "mul", "fma", "convert", "lt", "le", "eq", "nan")
SPACES = {"global": 1, "shared": 2, "local": 4, "shared_cluster": 8}


class Range(ctypes.Structure):
    _fields_ = [("start", ctypes.c_uint64), ("last", ctypes.c_uint64), ("space", ctypes.c_uint32)]


class Warp(ctypes.Structure):
    _fields_ = [("active", ctypes.c_uint32), ("waiting", ctypes.c_uint32), ("ranges", ctypes.POINTER(Range)), ("nranges", ctypes.c_uint32)]


class Failure(Exception):
    """A primitive failed with the runtime's message."""


@cache
def library():
    lib = build.library((build.HERE / "primitives.cpp").read_text(), "primitives")
    lib.sass_primitive.argtypes = [ctypes.c_char_p, ctypes.POINTER(Warp), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64),
                                   ctypes.c_int, ctypes.POINTER(ctypes.c_uint64), ctypes.c_char_p, ctypes.c_size_t]
    lib.sass_memory_descriptor.argtypes = [ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
    lib.sass_fp.argtypes = [ctypes.c_int] * 5 + [ctypes.c_uint64] * 3
    lib.sass_fp.restype = ctypes.c_uint64
    lib.sass_reduced_sine.argtypes, lib.sass_reduced_sine.restype = [ctypes.c_uint32], ctypes.c_uint32
    lib.sass_mufu_coefficient.argtypes, lib.sass_mufu_coefficient.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int64
    return lib


def checked(fn, *args):
    message = ctypes.create_string_buffer(MESSAGE)
    if fn(*args, message, MESSAGE): raise Failure(message.value.decode())


def primitive(name, args, lane=0, active=None, waiting=0, ranges=()):
    """Primitive `name` for `lane`: `args` are its arguments, each 32 lanes' values (or one value,
    this lane's); `ranges` are (space, start, end) generic address ranges. Returns this lane's result."""
    columns = [a if isinstance(a, (list, tuple)) else [a if l == lane else 0 for l in range(LANES)] for a in args]
    data = (ctypes.c_uint64 * (LANES * max(len(columns), 1)))(*(v & (1 << 64) - 1 for column in columns for v in column))
    spans = [Range(start, end - 1, SPACES[space]) for space, start, end in ranges if start < end]
    warp = Warp(1 << lane if active is None else active, waiting, (Range * len(spans))(*spans), len(spans))
    out = (ctypes.c_uint64 * LANES)()
    checked(library().sass_primitive, name.encode(), ctypes.byref(warp), 1 << lane, data, len(columns), out)
    return out[lane]


def memory_descriptor(descriptor): checked(library().sass_memory_descriptor, descriptor)


def fp(op, width, a, b=0, c=0, mode="RN", scale=0, dst=None):
    """softfloat's op (FP_OPS) in C++ on encodings of `width` bits; convert produces `dst` bits."""
    return library().sass_fp(FP_OPS.index(op), width, dst or width, ROUNDING.index(mode), scale, a, b, c)


def reduced_sine(offset): return library().sass_reduced_sine(offset)


def sincos(bits, op): return primitive(f"mufu_{op.lower()}", [bits])


def mufu_coefficients(): return [[library().sass_mufu_coefficient(s, k) for k in range(3)] for s in range(64)]
