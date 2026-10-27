"""Focused CUDA backend comparisons against Python softfloat; run outside the sandbox."""
import hashlib, random, struct, subprocess
from types import SimpleNamespace

import pytest
from sass.emulate import build, native
from sass.paths import CACHE, NVCC, ROOT
from sass.semantics import bitvec
from tests.harness import gpu
from tests.test_emulate_fp import binary, special, ternary

pytestmark = pytest.mark.gpu


def image(arch, force_soft):
    source = ROOT / "tests/kernels/fp_backends.cu"
    flags = ("--allow-unsupported-compiler", "-std=c++20", "-ftz=false", "--fmad=false",
             *(("-DSASS_FP_SOFT",) if force_soft else ()), "-I", str(build.INCLUDE),
             "-cubin", f"-arch=sm_{arch[2:]}")
    key = hashlib.sha256(source.read_bytes() + b"".join(p.read_bytes() for p in build.SOURCES) +
                         repr(flags).encode() + subprocess.check_output([NVCC, "--version"])).hexdigest()[:16]
    out = CACHE / "kernels" / f"fp-backends-{key}.cubin"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([NVCC, *flags, str(source), "-o", str(out)], check=True)
    return out.read_bytes()


def requests():
    rng = random.Random(89)
    for w in (16, 32, 64):
        pairs = binary(w, w, 30)
        triples = ternary(w, w, 30)
        for mode in native.ROUNDING:
            for op in ("add", "mul", "fma", "lt", "le", "eq", "nan"):
                inputs = triples if op == "fma" else [(a, b, 0) for a, b in pairs]
                for power in ((-513, -512, -2, 0, 3, 512, 513) if op == "mul" else (0,)):
                    for a, b, c in inputs:
                        if op in ("add", "mul", "fma"):
                            operands = [a, b] if op != "fma" else [a, b, c]
                            args = operands + [mode] + ([power] if op == "mul" else [])
                            widths = [w] * len(operands) + [None] * (len(args) - len(operands))
                            expected = bitvec.OPS[{"add": "fadd", "mul": "fmul", "fma": "ffma"}[op]](args, widths, w)
                        elif op == "nan":
                            expected = bitvec.OPS["fnan"]([a], [w], 1)
                        else:
                            expected = bitvec.OPS["f" + op]([a, b], [w, w], 1)
                        yield (native.FP_OPS.index(op), w, w, native.ROUNDING.index(mode),
                               power & ((1 << 64) - 1), a, b, c), expected
            for dst in (16, 32, 64):
                for a in [*special(w), *(rng.getrandbits(w) for _ in range(80))]:
                    expected = bitvec.OPS["fconv"]([a, mode], [w, None], dst)
                    yield (3, w, dst, native.ROUNDING.index(mode), 0, a, 0, 0), expected


@pytest.mark.parametrize("force_soft", (False, True), ids=("default", "soft-defined"))
def test_cuda_backend(pytestconfig, force_soft):
    arch = gpu.target(pytestconfig)
    if arch is None: pytest.skip(pytestconfig._gpu_unavailable)
    rows = list(requests())
    count = -(-len(rows) // 32) * 32
    rows.extend([rows[0]] * (count - len(rows)))
    data = b"".join(struct.pack("<8Q", *request) for request, _ in rows)
    # This backend probe has eight input columns rather than Fixture's three.
    fixture = SimpleNamespace(image=image(arch, force_soft), kernel="fp_backends", data=data,
                              threads=count, block=32, output_size=8, arguments=())
    driver = gpu.Driver(arch)
    try: got = driver.run(fixture)
    finally: driver.close()
    bad = [(request, value, expected) for (request, expected), value in zip(rows, got) if value != expected]
    assert not bad, f"{len(bad)}/{len(rows)} differ; first: {bad[:5]}"
