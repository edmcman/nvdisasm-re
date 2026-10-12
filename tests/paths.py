"""Repository paths, external tools and the architecture list shared by every test layer."""
import os
from pathlib import Path
from sass.build_languages import ARCHES
from sass.fpops import MODES as FP_MODES

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = Path(os.environ.get("SASS_SLEIGH_OUT", ROOT / "processor/SASS/data/languages")).resolve()
LDEFS = LANGUAGES / "sass.ldefs"
GHIDRA_PY = os.environ.get("GHIDRA_PY", os.path.expanduser("~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv/bin/python3"))
NVCC = os.environ.get("NVCC", "/usr/local/cuda/bin/nvcc")
CACHE = ROOT / ".cache"
CUOBJDUMP = os.environ.get("CUOBJDUMP", "/usr/local/cuda/bin/cuobjdump")
# The target nvdisasm (CUDA 13.4, V13.4.92); the oracle cache is keyed by its hash.
NVDISASM = os.environ.get("NVDISASM") or next(
    (str(p) for p in [Path("/tmp/.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm"),
                      *Path.home().glob(".cache/uv/archive-v0/*/nvidia/cu13/bin/nvdisasm")] if p.exists()), "nvdisasm")


def language(arch, fp="exact"):
    from sass.gen_sleigh import variant
    return f"SASS:LE:64:{variant(arch, fp)}"
