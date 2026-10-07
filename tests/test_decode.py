"""Differential decode tests against nvdisasm, ratcheted per md class."""
import json
from collections import defaultdict
from pathlib import Path
import pytest
import compare, corpus, synth

ARCHS = ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM120"]
BASELINE = Path(__file__).parent / "baseline"
REAL_SAMPLE, MUTATED = 200_000, 50_000

def corpora(arch, full):
    real = corpus.real(arch)
    yield "real", compare.sample(arch, "real", 0 if full else REAL_SAMPLE), None
    labels = synth.synthetic(arch, 64, 0)
    yield "synthetic", list(labels), labels
    yield "mutated", list(corpus.mutated(real, MUTATED, 0)), None

def class_results(arch, full):
    status = defaultdict(lambda: True)
    for _, words, labels in corpora(arch, full):
        for w, (s, cls, exp, _) in compare.compare(arch, words).items():
            if s == "blank": continue
            key = labels[w].removeprefix("synth:") if labels else cls or f"<{(exp or '?').split()[0]}>"
            status[key] &= s in ("ok", "both_err")
    return dict(sorted(status.items()))

@pytest.mark.parametrize("arch", ARCHS)
def test_pydecode(arch, request):
    now = class_results(arch, request.config.getoption("--full"))
    path = BASELINE / f"{arch}.json"
    if request.config.getoption("--update-baseline") or not path.exists():
        BASELINE.mkdir(exist_ok=True)
        path.write_text(json.dumps(now, indent=0) + "\n")
    base = json.loads(path.read_text())
    regressed = sorted(c for c, ok in now.items() if base.get(c) and not ok)
    improved = sorted(c for c, ok in now.items() if ok and base.get(c) is False)
    if improved: print(f"{arch}: newly passing (run --update-baseline): {improved}")
    assert not regressed, f"{arch}: classes regressed: {regressed}"
