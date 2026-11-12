"""Generated SLEIGH disassembles like pydecode (SLEIGH display mode), ratcheted per md
class against baseline/ghidra_SMxx.json. Synthetic words (64 per class) by default; --full adds
the whole real corpus. Architectures run concurrently; display is identical for both lowerings."""
from collections import defaultdict
import pytest
from sass import assemble, synth
from sass.check.ghidra_disasm import compare
from sass.check.pydecode_disasm import sample
from sass.decode import pydecode
from tests.harness.select import arches, modes, require_languages
from sass import ghidra_bridge
from sass.sleigh.build import ARCHES
from tests.harness.baseline import BASELINE, ratchet


def class_results(arch, full):
    words = list(synth.synthetic(arch, 64, 0)) + (sample(arch, "real", 0)[0] if full else [])
    status = defaultdict(lambda: True)
    for s, cls, _, _ in compare(arch, words).values(): status[cls] &= s == "ok"
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


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_ghidra_unaligned_disassembly(arch, fp, request):
    """Raw code can start at any byte address, including relative branches."""
    if arch not in arches(request.config) or fp not in modes(request.config):
        pytest.skip("architecture/lowering not selected")
    require_languages([(arch, fp)])
    words = [assemble.assemble_instruction(arch, text, addr=0x100)
             for text in ("NOP", "BRA 0x130")]
    inputs = [(word, 0x100 + offset) for offset in range(16) for word in words]
    results = ghidra_bridge.disassemble(arch, fp, inputs, "--dataflow")
    for (word, address), result in zip(inputs, results):
        assert "error" not in result, f"{address:#x}: {result}"
        expected = pydecode.decode(arch, word, address)
        assert "".join(result["text"].split()) == "".join(expected.text.split())
        if expected.klass.mnemonic == "BRA":
            branches = [op for op in result["ops"] if op["op"] in ("BRANCH", "CBRANCH")]
            assert len(branches) == 1
            assert branches[0]["inputs"][0] == dict(space="ram", offset=address + 16 + 32, size=8)
