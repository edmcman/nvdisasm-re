"""SM89/SM90 whole-kernel execution: compile CUDA fixtures, patch SASS words, run on the GPU through
the CUDA Driver API, and build the equivalent whole-kernel emulator request."""
import ctypes as C, hashlib, struct, subprocess
from dataclasses import dataclass, field
from typing import Callable
from sass import launch
from sass.decode import cubin, pydecode
from tests.harness.case import word
from sass.paths import CACHE, NVCC, ROOT

ENCODING = (1 << 105) - 1  # instruction bits below the scheduling controls
INPUT, OUTPUT = 0x100000000, 0x200000000


def compile(kernel, *flags, arch="SM89"):
    """Cached target cubin for tests/kernels/<kernel>.cu."""
    source = ROOT / "tests/kernels" / f"{kernel}.cu"
    argv = [NVCC, "--allow-unsupported-compiler", *flags, "-cubin", f"-arch=sm_{arch[2:]}"]
    key = hashlib.sha256(source.read_bytes() + " ".join(argv).encode()).hexdigest()[:16]
    out = CACHE / "kernels" / f"{kernel}-{key}.cubin"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*argv, str(source), "-o", str(out)], check=True)
    return out.read_bytes()


@dataclass
class Fixture:
    """One patched kernel launch with reference outputs `expected` (None only while drafting with
    tests.observe). GPU outputs are compared with it using `same(gpu, want)`, emulated outputs using
    `emulator_same(emulated, want)` (default `same`). The emulator may run a different
    `emulator_image` (default image); `excused(i, emulated, want)` admits known emulator defects;
    `visited` offsets and `targets` mnemonics must execute; `exit` requires termination through EXIT. `block` threads form a block;
    `cooperative` emulates each block as one (warps in lockstep, shared memory, BAR.SYNC, WARPSYNC) instead
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
    emulator_image: bytes | None = None
    excused: Callable = lambda i, emulated, want: False
    modes: tuple = ("exact",)  # fp lowerings to emulate
    vectors: list = field(default_factory=list, repr=False)
    block: int = 32
    cooperative: bool = False

    @property
    def threads(self): return len(self.data) // (3 * self.input_size)

    @property
    def fingerprint(self):
        """What a GPU run checks: the kernel (text, constants, parameters; not the other kernels sharing its cubin), launch, inputs and reference outputs."""
        launch = (cubin.kernel_image(self.image, self.kernel), self.kernel, self.input_size, self.output_size, self.arguments, self.block, self.expected)
        return hashlib.sha256(self.data + repr(launch).encode()).hexdigest()[:16]


def launch_parts(f):
    """Fixture f's kernel image and `launch.kernel_request`'s other arguments (launch dimensions, stack, parameters)."""
    image = cubin.kernel_image(f.emulator_image or f.image, f.kernel)
    if [p["size"] for p in image["parameters"]] != [8, 8] + [size for size, _ in f.arguments]:
        raise ValueError(f"{f.name}: unexpected kernel parameter layout")
    if not f.threads or len(f.data) % (3 * f.input_size) or f.threads % f.block or f.block % 32: raise ValueError("input must contain whole blocks of whole warps")
    memory = [dict(space="ram", address=INPUT, hex=f.data.hex()), dict(space="ram", address=OUTPUT, hex="cc" * f.threads * f.output_size)]
    return (image, (INPUT, OUTPUT, *(v for _, v in f.arguments)), memory, f.threads, f.block,
            dict(address=OUTPUT, size=f.output_size, stride=f.output_size), f.cooperative,
            dict(synchronization=dict(bssy=True, bsync=True, **({"bar": True, "warpsync": True} if f.cooperative else {}))))


def request(f):
    """The emulator's whole-kernel request for fixture f."""
    return launch.kernel_request(*launch_parts(f))


class Driver:
    """CUDA Driver API through ctypes; supports SM89 and SM90 devices."""

    def __init__(self, arch=None):
        self.lib = C.CDLL("libcuda.so.1"); self.ctx = C.c_void_p()
        self.call("cuInit", 0); device = C.c_int(); self.call("cuDeviceGet", C.byref(device), 0)
        major, minor = C.c_int(), C.c_int()
        self.call("cuDeviceGetAttribute", C.byref(major), 75, device); self.call("cuDeviceGetAttribute", C.byref(minor), 76, device)
        self.arch = f"SM{major.value}{minor.value}"
        if self.arch not in launch.GLOBAL_DESCRIPTOR: raise ValueError(f"unsupported GPU architecture {self.arch}; supported: SM89, SM90")
        self.require_arch(arch)
        self.call("cuCtxCreate_v2", C.byref(self.ctx), 0, device)
        name, version = C.create_string_buffer(256), C.c_int()
        self.call("cuDeviceGetName", name, 256, device); self.call("cuDriverGetVersion", C.byref(version))
        self.device = f"{name.value.decode()}, CUDA driver API {version.value // 1000}.{version.value % 1000 // 10}"

    def require_arch(self, arch):
        if arch is not None and arch != self.arch: raise ValueError(f"GPU target {arch} does not match device {self.arch}")

    def call(self, name, *args):
        if rc := getattr(self.lib, name)(*args): raise RuntimeError(f"{name}: CUDA status {rc}")

    def close(self):
        if self.ctx: self.call("cuCtxDestroy_v2", self.ctx); self.ctx = C.c_void_p()

    def run(self, f):
        self.require_arch(cubin.kernel_image(f.image, f.kernel)["arch"])
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
        meta = cubin.kernel_image(image, kernel)
        self.arch, self.code = meta["arch"], meta["code"]
        self.start = section_offset(image, ".text." + kernel)

    def decoded(self, offset): return pydecode.decode(self.arch, self.code[offset:offset + 16])

    def control(self, offset): return int.from_bytes(self.code[offset:offset + 16], "little") & ~ENCODING

    def patch(self, words, image=None):
        """words: {offset: (encoding int, control bits)} -> patched cubin bytes."""
        out = bytearray(image or self.image)
        for offset, (encoding, control) in words.items():
            out[self.start + offset:self.start + offset + 16] = ((encoding & ENCODING) | control).to_bytes(16, "little")
        return bytes(out)



def mnemonic(decoded):
    """The instruction's mnemonic with modifiers, without its guard predicate."""
    return decoded.text.split()[1 if decoded.text.startswith("@") else 0]


def hits(op, target):
    """Whether mnemonic `op` is fixture/probe target `target` (equal, or `target` plus modifiers)."""
    return op == target or op.startswith(target + ".")

def encode(cls, *, arch="SM89", **fields):
    # The md renamed this immediate operand form on Hopper.
    if arch == 'SM90': cls = {'p2r__RsIR_RIR': 'p2r__RuIR_RIR', 'bra_': 'bra__CONV_DIV'}.get(cls, cls)
    return word(arch, cls, **fields)


def sm89(cls, **fields):
    """Compatibility entry point for archived SM89 research probes."""
    return encode(cls, arch="SM89", **fields)


def target(config):
    """Resolve once before collection, without making CPU-only tests require CUDA."""
    if hasattr(config, '_gpu_target'): return config._gpu_target
    try:
        driver = Driver()
    except (OSError, RuntimeError) as error:
        if config.getoption('--require-gpu'): raise
        config._gpu_unavailable = str(error)
        config._gpu_target = None
        return None
    try:
        arch = driver.arch
        requested = config.getoption('--arch')
        if requested is not None and requested != 'all' and arch not in requested.split(','):
            raise ValueError(f"--arch {requested} does not include GPU device {arch}")
        config._gpu_target = arch
        return arch
    finally: driver.close()
