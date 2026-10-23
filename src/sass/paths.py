"""Package data, cache, repository paths and external tools."""
import os, sysconfig
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[1]  # the repository, in a checkout
CHECKOUT = PACKAGE == ROOT / "src/sass"
MD = PACKAGE / "md/data"  # the machine descriptions as shipped, laid out per architecture
LANGUAGES = Path(os.environ.get("SASS_SLEIGH_OUT", ROOT / "processor/SASS/data/languages")).resolve()
LDEFS = LANGUAGES / "sass.ldefs"
GHIDRA_PY = os.environ.get("GHIDRA_PY", os.path.expanduser("~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv/bin/python3"))
CACHE = Path(os.environ.get("SASS_CACHE") or (ROOT / ".cache" if CHECKOUT else
             Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sass"))
RAW = CACHE / "md-raw"  # sass-extract-md's output, which sass-layout-md lays out into MD
WHEELS = Path(sysconfig.get_paths()["purelib"]) / "nvidia/cu13/bin"  # the dev group's CUDA wheels


def tool(name, *fallbacks):
    """$NAME, else the dev group's wheel, else the first existing fallback, else `name` on PATH."""
    return os.environ.get(name.upper()) or next((str(p) for p in [WHEELS / name, *fallbacks] if p.exists()), name)


# nvcc/ptxas: CUDA 13.0.88, which built the GPU fixtures and PTX probes (the wheels and /usr/local/cuda give identical cubins).
NVCC = tool("nvcc", Path("/usr/local/cuda/bin/nvcc"))
PTXAS = tool("ptxas", Path("/usr/local/cuda/bin/ptxas"))
CUOBJDUMP = tool("cuobjdump", Path("/usr/local/cuda/bin/cuobjdump"))
# The target nvdisasm (CUDA 13.4, V13.4.92); the oracle cache is keyed by its hash.
NVDISASM = tool("nvdisasm", *Path.home().glob(".cache/uv/archive-v0/*/nvidia/cu13/bin/nvdisasm"))


def variant(arch, fp="exact"):
    """Language variant: exact float semantics keep the plain smXX name."""
    return arch.lower() + ("" if fp == "exact" else "_" + fp)


def language(arch, fp="exact"):
    return f"SASS:LE:64:{variant(arch, fp)}"
