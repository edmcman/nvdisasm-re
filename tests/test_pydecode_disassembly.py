"""pydecode disassembles like nvdisasm, ratcheted per md class against baseline/SMxx.json. The
selected architectures' words are compared in chunks spread over every CPU."""
from concurrent.futures import ProcessPoolExecutor
import pytest
from sass import corpus, synth
from sass.check.pydecode_disasm import PASSING, compare, sample
from tests.harness.baseline import BASELINE, ratchet
from tests.harness.select import arches

REAL_SAMPLE, MUTATED, CHUNK = 200_000, 50_000, 4096


def corpora(job):
    """[(words, labels)] for one architecture: sampled real, synthetic (labelled by class), mutated."""
    arch, full = job
    real = corpus.real(arch)
    labels = synth.synthetic(arch, 64, 0)
    return arch, [(sample(arch, "real", 0 if full else REAL_SAMPLE, real=real)[0], None), (list(labels), labels),
                  (list(corpus.mutated(real, MUTATED, 0)), None)]


def chunk_status(job):
    """{class: passed} over one chunk of words."""
    arch, words, labels = job
    status = {}
    for w, (s, cls, exp, _) in compare(arch, words).items():
        if s == "blank": continue
        key = labels[w].removeprefix("synth:") if labels else cls or f"<{(exp or '?').split()[0]}>"
        status[key] = status.get(key, True) and s in PASSING
    return arch, status


@pytest.fixture(scope="session")
def decoded(request):
    selected, full = arches(request.config), request.config.getoption("--full")
    with ProcessPoolExecutor() as pool:
        jobs = [(arch, words[i:i + CHUNK], labels and {w: labels[w] for w in words[i:i + CHUNK]})
                for arch, parts in pool.map(corpora, [(a, full) for a in selected]) for words, labels in parts for i in range(0, len(words), CHUNK)]
        out = {arch: {} for arch in selected}
        for arch, status in pool.map(chunk_status, jobs):
            for key, ok in status.items(): out[arch][key] = out[arch].get(key, True) and ok
    return {arch: dict(sorted(status.items())) for arch, status in out.items()}


def pytest_generate_tests(metafunc):
    if "pydecode_arch" in metafunc.fixturenames: metafunc.parametrize("pydecode_arch", arches(metafunc.config))


def test_pydecode_disassembly(pydecode_arch, decoded, request):
    ratchet(BASELINE / f"{pydecode_arch}.json", decoded[pydecode_arch], request.config.getoption("--update-baseline"))
