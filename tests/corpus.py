"""Instruction-word corpora: real (from cubins), synthetic (per class), mutated (bit flips)."""
import random, struct, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "cubins"
CUOBJDUMP = "/usr/local/cuda/bin/cuobjdump"
LIBS = [Path("/usr/local/cuda/lib64") / n for n in
        ("libcublasLt.so", "libcusparse.so", "libcusolver.so", "libcurand.so", "libcufft.so")]
LOOSE = [*Path.home().glob("Downloads/*.cubin"), *Path.home().glob(".triton/cache/*/*.cubin"),
         *(ROOT / "tests" / "kernels" / "build").glob("*.cubin")]

def sections(data):
    shoff, = struct.unpack_from("<Q", data, 0x28)
    entsize, num, strndx = struct.unpack_from("<HHH", data, 0x3a)
    hdrs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entsize) for i in range(num)]
    strtab = hdrs[strndx][4]
    name = lambda o: data[strtab + o:data.index(b"\0", strtab + o)].decode()
    return {name(h[0]): data[h[4]:h[4] + h[5]] for h in hdrs if h[1] != 8}  # skip NOBITS

def elf_arch(data):
    flags, = struct.unpack_from("<I", data, 0x30)
    sm = (flags >> 8 if data[8] >= 8 else flags) & 0xff
    return f"SM{sm}"

def text_words(data):
    for name, body in sections(data).items():
        if name.startswith(".text."):
            yield from (body[i:i + 16] for i in range(0, len(body) - 15, 16))

def extract(lib):
    out = CACHE / lib.name
    if not out.exists():
        tmp = out.with_suffix(".tmp"); tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run([CUOBJDUMP, "-xelf", "all", str(lib.resolve())], cwd=tmp, check=True, capture_output=True)
        tmp.rename(out)
    return sorted(out.glob("*.cubin"))

def cubins():
    yield from (p for lib in LIBS if lib.exists() for p in extract(lib))
    yield from LOOSE

def real(arch):
    """Unique words of `arch` -> provenance (first cubin and section offset seen)."""
    words = {}
    for p in cubins():
        data = p.read_bytes()
        if elf_arch(data) == arch:
            for i, w in enumerate(text_words(data)):
                words.setdefault(w, f"{p.name}+{16 * i:#x}")
    return words

def mutated(words, n, seed=0, flips=(1, 2)):
    rng, pool = random.Random(seed), sorted(words)
    if not pool: return {}
    def flip(w):
        v = int.from_bytes(w, "little")
        for b in rng.sample(range(128), rng.choice(flips)): v ^= 1 << b
        return v.to_bytes(16, "little")
    return {flip(rng.choice(pool)): "mutated" for _ in range(n)}
