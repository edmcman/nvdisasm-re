"""Equivalence testing (sass.emulate.equivalence) on the native emulator: a fixture kernel agrees with
itself, and differs from itself with its instruction under test replaced by a NOP, on exactly the
threads whose outputs then change."""
import pytest
from sass.decode import pydecode
from sass.emulate import equivalence, kernel
from tests.harness import gpu, hardware
from tests.harness.select import arches

pytestmark = pytest.mark.native  # opt-in: -m native

ARCH = "SM89"
FIXTURES = {"iadd3": "int_alu", "shfl_idx": "threads"}   # a non-cooperative and a cooperative kernel, by family


def fixture(name): return next(f for _, f in hardware.fixtures((FIXTURES[name],), ARCH) if f.name == name)


def nopped(image, mnemonic):
    """The image with its first `mnemonic` instruction replaced by one of its NOPs."""
    words = [image["code"][i:i + 16] for i in range(0, len(image["code"]), 16)]
    names = [pydecode.decode(image["arch"], w).klass.mnemonic for w in words]
    nop = words[names.index("NOP")]
    words[names.index(mnemonic)] = nop
    return dict(image, code=b"".join(words))


@pytest.mark.parametrize("name", FIXTURES)
def test_equivalence(name, request):
    if ARCH not in arches(request.config): pytest.skip(f"an {ARCH} kernel")
    f = fixture(name)
    image, arguments, memory, threads, block, output, cooperative, context = gpu.launch_parts(f)
    runs = [equivalence.Launch(arguments, tuple(memory), output, threads, block, cooperative, context)]
    assert equivalence.equivalent(image, image, runs)
    mnemonic = next(m for m in (t.split(".")[0] for t in f.targets) if m != "NOP")
    other = nopped(image, mnemonic)
    found = list(equivalence.differences(image, other, runs))
    assert found, f"replacing {mnemonic} by NOP changed no output"
    mine, theirs = (equivalence.response(i, runs[0], "exact") for i in (image, other))
    assert {d.thread for d in found} == {t for t, (p, q) in enumerate(zip(mine["outputs"], theirs["outputs"])) if p != q}
