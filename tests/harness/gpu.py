"""SM89 whole-kernel execution: compile CUDA fixtures, patch SASS words, run on the GPU through
the CUDA Driver API, and build the equivalent whole-kernel emulator request."""
import ctypes as C, hashlib, struct, subprocess
from dataclasses import dataclass, field
from typing import Callable
from sass.decode import cubin as elf, pydecode
from sass.decode.pyencode import word
from sass.paths import CACHE, NVCC, ROOT

ENCODING = (1 << 105) - 1  # instruction bits below the scheduling controls
INPUT, OUTPUT = 0x100000000, 0x200000000


def compile(kernel, *flags):
    """Cached SM89 cubin for tests/kernels/<kernel>.cu."""
    source = ROOT / "tests/kernels" / f"{kernel}.cu"
    argv = [NVCC, "--allow-unsupported-compiler", *flags, "-cubin", "-arch=sm_89"]
    key = hashlib.sha256(source.read_bytes() + " ".join(argv).encode()).hexdigest()[:16]
    out = CACHE / "kernels" / f"{kernel}-{key}.cubin"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*argv, str(source), "-o", str(out)], check=True)
    return out.read_bytes()


@dataclass
class Fixture:
    """One patched kernel launch: GPU outputs are compared with `expected` (the reference) using
    `same(gpu, want)` and with the emulator using `emulator_same(emulated, other)` (default `same`),
    where other is the GPU output or, with emulator_vs="reference", the expected value. The emulator
    may run a different `emulator_image` (default image);
    `excused(i, emulated, gpu)` admits known emulator defects; `visited` offsets and `targets`
    mnemonics must execute; `exit` requires termination through EXIT. `block` threads form a block;
    `cooperative` emulates each block as one (warps in lockstep, shared memory, BAR.SYNC) instead
    of every thread alone."""
    name: str
    image: bytes
    kernel: str
    data: bytes
    expected: list | None
    input_size: int = 4
    output_size: int = 4
    arguments: tuple = ()  # extra (size, value) kernel parameters after the input/output pointers
    visited: tuple = ()
    exit: bool = False
    targets: tuple = ()
    same: Callable = lambda got, want: got == want
    emulator_same: Callable | None = None
    emulator_vs: str = "gpu"
    emulator_image: bytes | None = None
    excused: Callable = lambda i, emulated, gpu: False
    modes: tuple = ("exact",)  # fp lowerings to emulate
    vectors: list = field(default_factory=list, repr=False)
    block: int = 32
    cooperative: bool = False

    @property
    def threads(self): return len(self.data) // (3 * self.input_size)


def request(f):
    """The emulator's whole-kernel request for fixture f (launch dimensions, stack, parameters)."""
    image = elf.kernel_image(f.emulator_image or f.image, f.kernel)
    if image["arch"] != "SM89": raise ValueError("SM89 cubin required")
    if [p["size"] for p in image["parameters"]] != [8, 8] + [size for size, _ in f.arguments]:
        raise ValueError(f"{f.name}: unexpected kernel parameter layout")
    if not f.threads or len(f.data) % (3 * f.input_size) or f.threads % f.block or f.block % 32: raise ValueError("input must contain whole blocks of whole warps")
    constants = dict(image["constants"]); bank = bytearray(constants[0])
    # CUDA launch dimensions and local stack base precede the parameter region (SM89 compiler output).
    struct.pack_into("<III", bank, 0, f.block, 1, 1)
    struct.pack_into("<I", bank, 0x28, 0x1000)
    for parameter, value in zip(image["parameters"], (INPUT, OUTPUT, *(v for _, v in f.arguments))):
        struct.pack_into("<Q" if parameter["size"] == 8 else "<I", bank, image["parameter_base"] + parameter["offset"], value)
    constants[0] = bytes(bank)
    memory = [dict(space="cbank", address=index << 32, hex=value.hex()) for index, value in constants.items()]
    memory += [dict(space="ram", address=INPUT, hex=f.data.hex()), dict(space="ram", address=OUTPUT, hex="cc" * f.threads * f.output_size)]
    return dict(kernel=dict(name=image["name"], code=image["code"].hex(), threads=f.threads, block_size=f.block, cooperative=f.cooperative), memory=memory,
                output=dict(address=OUTPUT, size=f.output_size, stride=f.output_size), context=dict(synchronization=dict(bssy=True, bsync=True, **({"bar": True} if f.cooperative else {}))))


class Driver:
    """CUDA Driver API through ctypes; requires an SM89 device."""

    def __init__(self):
        self.lib = C.CDLL("libcuda.so.1"); self.ctx = C.c_void_p()
        self.call("cuInit", 0); device = C.c_int(); self.call("cuDeviceGet", C.byref(device), 0)
        major, minor = C.c_int(), C.c_int()
        self.call("cuDeviceGetAttribute", C.byref(major), 75, device); self.call("cuDeviceGetAttribute", C.byref(minor), 76, device)
        if (major.value, minor.value) != (8, 9): raise RuntimeError(f"SM89 GPU required, found {(major.value, minor.value)}")
        self.call("cuCtxCreate_v2", C.byref(self.ctx), 0, device)

    def call(self, name, *args):
        if rc := getattr(self.lib, name)(*args): raise RuntimeError(f"{name}: CUDA status {rc}")

    def close(self):
        if self.ctx: self.call("cuCtxDestroy_v2", self.ctx); self.ctx = C.c_void_p()

    def run(self, f):
        module, fn = C.c_void_p(), C.c_void_p()
        self.call("cuModuleLoadData", C.byref(module), C.c_char_p(f.image))
        try:
            self.call("cuModuleGetFunction", C.byref(fn), module, f.kernel.encode())
            a, b = C.c_uint64(), C.c_uint64()
            self.call("cuMemAlloc_v2", C.byref(a), C.c_size_t(len(f.data)))
            self.call("cuMemAlloc_v2", C.byref(b), C.c_size_t(f.threads * f.output_size))
            try:
                self.call("cuMemcpyHtoD_v2", a, C.create_string_buffer(f.data), C.c_size_t(len(f.data)))
                values = [a, b] + [(C.c_uint64 if size == 8 else C.c_uint32)(v) for size, v in f.arguments]
                params = (C.c_void_p * len(values))(*(C.cast(C.byref(v), C.c_void_p) for v in values))
                self.call("cuLaunchKernel", fn, C.c_uint(f.threads // f.block), C.c_uint(1), C.c_uint(1), C.c_uint(f.block), C.c_uint(1), C.c_uint(1),
                          C.c_uint(0), C.c_void_p(), params, C.c_void_p())
                result = C.create_string_buffer(f.threads * f.output_size)
                self.call("cuCtxSynchronize"); self.call("cuMemcpyDtoH_v2", result, b, C.c_size_t(f.threads * f.output_size))
            finally:
                self.call("cuMemFree_v2", a); self.call("cuMemFree_v2", b)
        finally:
            self.call("cuModuleUnload", module)
        return list(struct.unpack("<" + ("Q" if f.output_size == 8 else "I") * f.threads, result.raw))


def section_offset(image, name):
    """File offset of ELF section `name`."""
    shoff, = struct.unpack_from("<Q", image, 0x28)
    entsize, count, strndx = struct.unpack_from("<HHH", image, 0x3a)
    headers = [struct.unpack_from("<IIQQQQIIQQ", image, shoff + i * entsize) for i in range(count)]
    strings = headers[strndx][4]
    return next(h[4] for h in headers if image[strings + h[0]:image.index(0, strings + h[0])] == name.encode())


class Template:
    """A compiled kernel whose instruction words can be replaced, keeping scheduling controls."""

    def __init__(self, image, kernel):
        self.image, self.kernel = image, kernel
        self.code = elf.kernel_image(image, kernel)["code"]
        self.start = section_offset(image, ".text." + kernel)

    def decoded(self, offset): return pydecode.decode("SM89", self.code[offset:offset + 16])

    def control(self, offset): return int.from_bytes(self.code[offset:offset + 16], "little") & ~ENCODING

    def patch(self, words, image=None):
        """words: {offset: (encoding int, control bits)} -> patched cubin bytes."""
        out = bytearray(image or self.image)
        for offset, (encoding, control) in words.items():
            out[self.start + offset:self.start + offset + 16] = ((encoding & ENCODING) | control).to_bytes(16, "little")
        return bytes(out)


def sm89(cls, **fields): return word("SM89", cls, **fields)
