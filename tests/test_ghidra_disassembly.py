"""Generated SLEIGH disassembles like pydecode (SLEIGH display mode), ratcheted per md
class against baseline/ghidra_SMxx.json. Synthetic words (64 per class) by default; --full adds
the whole real corpus. Architectures run concurrently; display is identical for both lowerings."""
from collections import defaultdict
import pytest
from sass import synth
from sass.check.ghidra_disasm import compare
from sass.check.pydecode_disasm import sample
from tests.harness.select import arches, require_languages
from sass import ghidra_bridge
from sass.sleigh.build import ARCHES
from tests.harness.baseline import BASELINE, ratchet


def class_results(arch, full):
    words = list(synth.synthetic(arch, 64, 0)) + (sample(arch, "real", 0)[0] if full else [])
    status = defaultdict(lambda: True)
    for s, cls, _, _ in compare(arch, words).values(): status[cls] &= s in ("ok", "collision")  # collisions are Ghidra's cache, not the language
    return dict(sorted(status.items()))


@pytest.fixture(scope="session")
def disassembly(request):
    selected = arches(request.config)
    require_languages([(a, "exact") for a in selected])
    return ghidra_bridge.concurrently(class_results, {a: (a, request.config.getoption("--full")) for a in selected})


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ARCHES)
def test_ghidra_disassembly(arch, disassembly, request):
    if arch not in disassembly: pytest.skip(f"--arch does not select {arch}")
    ratchet(BASELINE / f"ghidra_{arch}.json", disassembly[arch], request.config.getoption("--update-baseline"))
