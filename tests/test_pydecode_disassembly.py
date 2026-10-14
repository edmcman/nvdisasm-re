"""Layer 1: pydecode disassembles like nvdisasm, ratcheted per md class against baseline/SMxx.json.
Every architecture, decoded in parallel processes."""
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import pytest
from sass import corpus, synth
from sass.check.pydecode_disasm import PASSING, compare, sample
from sass.sleigh.build import ARCHES
from tests.harness.baseline import BASELINE, ratchet

REAL_SAMPLE, MUTATED = 200_000, 50_000


def corpora(arch, full):
    real = corpus.real(arch)
    yield sample(arch, "real", 0 if full else REAL_SAMPLE, real=real)[0], None
    labels = synth.synthetic(arch, 64, 0)
    yield list(labels), labels
    yield list(corpus.mutated(real, MUTATED, 0)), None


def class_results(arch, full):
    status = defaultdict(lambda: True)
    for words, labels in corpora(arch, full):
        for w, (s, cls, exp, _) in compare(arch, words).items():
            if s == "blank": continue
            key = labels[w].removeprefix("synth:") if labels else cls or f"<{(exp or '?').split()[0]}>"
            status[key] &= s in PASSING
    return dict(sorted(status.items()))


@pytest.fixture(scope="session")
def decoded(request):
    full = request.config.getoption("--full")
    with ProcessPoolExecutor(len(ARCHES)) as pool:
        return dict(zip(ARCHES, pool.map(class_results, ARCHES, [full] * len(ARCHES))))


@pytest.mark.parametrize("arch", ARCHES)
def test_pydecode_disassembly(arch, decoded, request):
    ratchet(BASELINE / f"{arch}.json", decoded[arch], request.config.getoption("--update-baseline"))
