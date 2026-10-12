"""Layer 2: generated SLEIGH disassembles like pydecode (SLEIGH display mode), ratcheted per md
class against baseline/ghidra_SMxx.json. Synthetic words (64 per class) by default; --full adds
the whole real corpus. Architectures run concurrently; display is identical for both lowerings."""
from collections import defaultdict
import pytest
from tests import synth
from tests.compare_ghidra_disassembly import compare
from tests.compare_pydecode_disassembly import sample
from tests.conftest import arches, require_languages
from tests.harness import ghidra
from tests.paths import ARCHES
from tests.test_pydecode_disassembly import BASELINE, ratchet


def class_results(arch, full):
    words = list(synth.synthetic(arch, 64, 0)) + (sample(arch, "real", 0)[0] if full else [])
    status = defaultdict(lambda: True)
    for s, cls, _, _ in compare(arch, words).values(): status[cls] &= s in ("ok", "collision")  # collisions are Ghidra's cache, not the language
    return dict(sorted(status.items()))


@pytest.fixture(scope="session")
def disassembly(request):
    selected = arches(request.config)
    require_languages([(a, "exact") for a in selected])
    return ghidra.concurrently(class_results, {a: (a, request.config.getoption("--full")) for a in selected})


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ARCHES)
def test_ghidra_disassembly(arch, disassembly, request):
    if arch not in disassembly: pytest.skip(f"--arch does not select {arch}")
    ratchet(BASELINE / f"ghidra_{arch}.json", disassembly[arch], request.config.getoption("--update-baseline"))
