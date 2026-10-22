"""Repository paths and external tools."""
import os, sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out"
LANGUAGES = Path(os.environ.get("SASS_SLEIGH_OUT", ROOT / "processor/SASS/data/languages")).resolve()
LDEFS = LANGUAGES / "sass.ldefs"
GHIDRA_PY = os.environ.get("GHIDRA_PY", os.path.expanduser("~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv/bin/python3"))
CACHE = ROOT / ".cache"
WHEELS = Path(sysconfig.get_paths()["purelib"]) / "nvidia/cu13/bin"  # the dev group's CUDA wheels


def tool(name, *fallbacks):
    """$NAME, else the dev group's wheel, else the first existing fallback, else `name` on PATH."""
    return os.environ.get(name.upper()) or next((str(p) for p in [WHEELS / name, *fallbacks] if p.exists()), name)


# nvcc/ptxas: CUDA 13.0.88, which built the GPU fixtures and PTX probes (the wheels and /usr/local/cuda give identical cubins).
NVCC = tool("nvcc", Path("/usr/local/cuda/bin/nvcc"))
PTXAS = tool("ptxas", Path("/usr/local/cuda/bin/ptxas"))
CUOBJDUMP = tool("cuobjdump", Path("/usr/local/cuda/bin/cuobjdump"))
# The target nvdisasm (CUDA 13.4, V13.4.92); the oracle cache is keyed by its hash.
NVDISASM = tool("nvdisasm", Path("/tmp/.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm"),
                *Path.home().glob(".cache/uv/archive-v0/*/nvidia/cu13/bin/nvdisasm"))


def variant(arch, fp="exact"):
    """Language variant: exact float semantics keep the plain smXX name."""
    return arch.lower() + ("" if fp == "exact" else "_" + fp)


def language(arch, fp="exact"):
    return f"SASS:LE:64:{variant(arch, fp)}"
