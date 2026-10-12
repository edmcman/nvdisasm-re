"""Layer 1: pydecode disassembles like nvdisasm, ratcheted per md class against baseline/SMxx.json."""
import json
from collections import defaultdict
from pathlib import Path
import pytest
from tests import corpus, synth
from tests.compare_pydecode_disassembly import PASSING, compare, sample
from tests.paths import ARCHES

BASELINE = Path(__file__).parent / "baseline"
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


def ratchet(path, now, update):
    """Fail on classes that regressed against the baseline at `path`; report newly passing ones."""
    if update: path.parent.mkdir(exist_ok=True); path.write_text(json.dumps(now, indent=0) + "\n")
    if not path.exists(): pytest.fail(f"{path.name} missing: run with --update-baseline", pytrace=False)
    base = json.loads(path.read_text())
    regressed = sorted(c for c, ok in now.items() if base.get(c) and not ok)
    improved = sorted(c for c, ok in now.items() if ok and base.get(c) is False)
    if improved: print(f"{path.stem}: newly passing (run --update-baseline): {improved}")
    assert not regressed, f"{path.stem}: classes regressed: {regressed}"


@pytest.mark.parametrize("arch", ARCHES)
def test_pydecode_disassembly(arch, request):
    ratchet(BASELINE / f"{arch}.json", class_results(arch, request.config.getoption("--full")), request.config.getoption("--update-baseline"))
