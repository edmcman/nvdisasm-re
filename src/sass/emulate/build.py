"""C++ compiled on first use into the cache (`CACHE/emulate`): a shared library per source, keyed by
the source, the runtime headers, the compiler's identity and the flags, written by atomic rename so
concurrent workers share it (the pattern of `ghidra_decode.java_class`)."""
import ctypes, hashlib, os, platform, subprocess, tempfile
from functools import cache
from pathlib import Path
from sass.paths import CACHE, CXX

HERE = Path(__file__).parent
INCLUDE = HERE / "include"
SOURCES = sorted([*INCLUDE.rglob("*.hpp"), *HERE.glob("*.cpp")])   # what a compiled library may contain
X86 = platform.machine().lower() in ("x86_64", "amd64")
# No contraction or fast math: the arithmetic must round exactly as written (on x86 in SSE, not x87's
# extended precision).
FLAGS = ("-std=c++20", "-O2", "-fPIC", "-shared", "-ffp-contract=off", "-fno-fast-math",
         "-frounding-math", *(("-msse2", "-mfpmath=sse") if X86 else ()))
# Control-register bits that flush subnormals or replace NaN operands: x86 MXCSR FTZ/DAZ; ARM64 FPCR
# FZ, FZ16, DN and FEAT_AFP's AH/FIZ.
FP_CONTROL = ("MXCSR", 0x8040, "FTZ and DAZ") if X86 else ("FPCR", 0x3080003, "FZ, FZ16, DN, AH and FIZ")


@cache
def compiler():
    return subprocess.run([CXX, "--version"], capture_output=True, text=True, check=True).stdout


def flags(): return [*os.environ.get("SASS_CXXFLAGS", "").split(), *FLAGS]


def path(source, stem):
    key = hashlib.sha256(b"\0".join([source.encode(), *(p.read_bytes() for p in SOURCES), compiler().encode(), *map(str.encode, flags())]))
    return CACHE / "emulate" / f"{stem}-{key.hexdigest()[:16]}.so"


FP_CONTROL_SOURCE = """
#if defined(__x86_64__)
#include <xmmintrin.h>
extern "C" unsigned long long sass_runtime_fp_control() { return _mm_getcsr(); }
#elif defined(__aarch64__)
extern "C" unsigned long long sass_runtime_fp_control() {
  unsigned long long fpcr;
  __asm__ volatile("mrs %0, fpcr" : "=r"(fpcr));
  return fpcr;
}
#else
#error "the SASS runtime supports x86-64 and ARM64 hosts"
#endif
"""


def check_fp_control(lib):
    """Reject flushing or default-NaN arithmetic without modifying the caller's process settings."""
    lib.sass_runtime_fp_control.restype = ctypes.c_uint64
    lib.sass_runtime_fp_control.argtypes = []
    register, mask, names = FP_CONTROL
    if lib.sass_runtime_fp_control() & mask:
        raise RuntimeError(f"SASS floating point requires {register} {names} disabled (bits {mask:#x}); "
                           "the loading process must clear them before loading the runtime")


def library(source, stem):
    """The loaded shared library compiled from C++ `source` (which may include `sass/...` headers)."""
    source += FP_CONTROL_SOURCE
    out = path(source, stem)
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
            cpp, so = Path(tmp, f"{stem}.cpp"), Path(tmp, f"{stem}.so")
            cpp.write_text(source)
            subprocess.run([CXX, *flags(), "-I", str(INCLUDE), "-o", str(so), str(cpp)], check=True)
            os.replace(so, out)
    lib = ctypes.CDLL(str(out))
    check_fp_control(lib)
    return lib
