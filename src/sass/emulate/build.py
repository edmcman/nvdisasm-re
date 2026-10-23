"""C++ compiled on first use into the cache (`CACHE/emulate`): a shared library per source, keyed by
the source, the runtime headers, the compiler's identity and the flags, written by atomic rename so
concurrent workers share it (the pattern of `ghidra_decode.java_class`)."""
import ctypes, hashlib, os, subprocess, tempfile
from functools import cache
from pathlib import Path
from sass.paths import CACHE, CXX

HERE = Path(__file__).parent
INCLUDE = HERE / "include"
SOURCES = sorted([*INCLUDE.rglob("*.hpp"), *HERE.glob("*.cpp")])   # what a compiled library may contain
# No contraction or fast math: the arithmetic must round exactly as written.
FLAGS = ("-std=c++20", "-O2", "-fPIC", "-shared", "-ffp-contract=off", "-fno-fast-math")


@cache
def compiler():
    return subprocess.run([CXX, "--version"], capture_output=True, text=True, check=True).stdout


def flags(): return [*FLAGS, *os.environ.get("SASS_CXXFLAGS", "").split()]


def path(source, stem):
    key = hashlib.sha256(b"\0".join([source.encode(), *(p.read_bytes() for p in SOURCES), compiler().encode(), *map(str.encode, flags())]))
    return CACHE / "emulate" / f"{stem}-{key.hexdigest()[:16]}.so"


def library(source, stem):
    """The loaded shared library compiled from C++ `source` (which may include `sass/...` headers)."""
    out = path(source, stem)
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
            cpp, so = Path(tmp, f"{stem}.cpp"), Path(tmp, f"{stem}.so")
            cpp.write_text(source)
            subprocess.run([CXX, *flags(), "-I", str(INCLUDE), "-o", str(so), str(cpp)], check=True)
            os.replace(so, out)
    return ctypes.CDLL(str(out))
