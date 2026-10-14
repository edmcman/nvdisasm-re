"""Instruction-word corpora: real (from cubins), synthetic (per class), mutated (bit flips)."""
import random, shutil, subprocess, tempfile
from pathlib import Path
from sass.decode.cubin import elf_arch, text_words
from sass.paths import CACHE as CACHE_ROOT, CUOBJDUMP, ROOT

CACHE = CACHE_ROOT / "cubins"
LIBS = [Path("/usr/local/cuda/lib64") / n for n in
        ("libcublasLt.so", "libcusparse.so", "libcusolver.so", "libcurand.so", "libcufft.so")]
LOOSE = [*Path.home().glob("Downloads/*.cubin"), *Path.home().glob(".triton/cache/*/*.cubin"),
         *(ROOT / "tests" / "kernels" / "build").glob("*.cubin")]

def extract(lib):
    out = CACHE / lib.name
    if not out.exists():
        # A private directory per process: parallel workers (one per architecture) extract at once.
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=lib.name, dir=CACHE))
        subprocess.run([CUOBJDUMP, "-xelf", "all", str(lib.resolve())], cwd=tmp, check=True, capture_output=True)
        try: tmp.rename(out)
        except OSError: shutil.rmtree(tmp)   # another worker finished first
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
