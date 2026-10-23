"""The engines answer every GPU fixture's whole-kernel request identically: outputs, steps, visited
offsets, exit, error and the traced writes of the first TRACED threads. The native emulator
(`sass.emulate.kernel`) and the lifted semantics (`sass.emulate.reference`) always (unmarked); the
Ghidra emulator and the lifted semantics with -m ghidra. All run `sass.emulate.schedule` or its C++
port, so a difference is in an instruction's semantics on one side. One item per (architecture,
family, fixture, fp lowering)."""
import re
import pytest
from sass import ghidra_bridge
from sass.emulate import kernel, reference
from tests.harness.select import arches, families, modes, require_languages, selected, skipped

ARCHES = ("SM89", "SM90")
TRACED = 64


def pytest_generate_tests(metafunc):
    for name, marker in (("parity_fixture", "ghidra"), ("native_parity_fixture", None)):
        if name not in metafunc.fixturenames: continue
        if not selected(metafunc.config, marker): return skipped(metafunc, name, marker or "unmarked", None if marker else "unmarked tests not selected by -m")
        from tests.harness.hardware import fixtures
        params = [(arch, family, f.name, mode) for arch in arches(metafunc.config) if arch in ARCHES
                  for family, f in fixtures(tuple(families(metafunc.config)), arch) for mode in f.modes if mode in modes(metafunc.config)]
        metafunc.parametrize(name, params, ids=["-".join(p) for p in params])


def traced(f):
    from tests.harness.gpu import request
    r = request(f)
    return dict(r, kernel=dict(r["kernel"], trace=True, trace_threads=TRACED))


def normalized(result):
    """An opaque instruction's error names its arguments on Ghidra, its lifter reason in Python: compare the pcodeop."""
    return dict(result, error=re.sub(r"(sass_opaque_\w+).*", r"\1", result["error"])) if "error" in result else result


def compared(config, other):
    """{(arch, fixture, mode): (fixture, other's response, Python's)} on traced requests."""
    from tests.harness import hardware
    from tests.harness.gpu import Template
    targets = [arch for arch in arches(config) if arch in ARCHES]
    batches = {(arch, mode): (arch, mode, [f for _, f in hardware.fixtures(tuple(families(config)), arch)
                                           if mode in f.modes and Template(f.image, f.kernel).arch == arch])
               for arch in targets for mode in modes(config)}
    requests = {key: (arch, mode, [traced(f) for f in fs]) for key, (arch, mode, fs) in batches.items()}
    theirs, python = other(requests), reference.emulate_sharded(requests)
    return {(arch, f.name, mode): (f, normalized(g), normalized(p)) for key, (arch, mode, fs) in batches.items()
            for f, g, p in zip(fs, theirs[key], python[key])}


@pytest.fixture(scope="session")
def responses(request):
    require_languages([(arch, mode) for arch in arches(request.config) if arch in ARCHES for mode in modes(request.config)])
    return compared(request.config, ghidra_bridge.emulate_sharded)


@pytest.fixture(scope="session")
def native_responses(request): return compared(request.config, kernel.emulate_sharded)


def ghidra_defects(f, ghidra, python):
    """Threads whose outputs differ only by a Ghidra emulator defect the fixture documents (Ghidra
    12.1.4's minimum-subnormal underflow, docs/semantics.md): Python's output is the hardware
    reference bit for bit, and Ghidra's is the fixture's emulator allowance for it."""
    if "outputs" not in ghidra or "outputs" not in python: return set()
    accepts = f.emulator_same or f.same
    return {i for i, (g, p, w) in enumerate(zip(ghidra_bridge.outputs(ghidra), ghidra_bridge.outputs(python), f.expected or []))
            if g != p and p == w and (accepts(g, w) or f.excused(i, g, w))}


def differences(f, ghidra, python, defects=True):
    excused = ghidra_defects(f, ghidra, python) if defects else set()
    out = [f"{k}: ghidra {ghidra.get(k)!r:.200} python {python.get(k)!r:.200}" for k in ("error", "steps", "visited", "exit") if ghidra.get(k) != python.get(k)]
    if bad := [i for i, (g, p) in enumerate(zip(ghidra.get("outputs", []), python.get("outputs", []))) if g != p and i not in excused]:
        out.append(f"outputs differ at {len(bad)} threads, first {bad[0]}: ghidra {ghidra['outputs'][bad[0]]} python {python['outputs'][bad[0]]}")
    if len(ghidra.get("outputs", [])) != len(python.get("outputs", [])): out.append("output counts differ")
    traces = [[s for s in r.get("trace", []) if s["thread"] not in excused] for r in (ghidra, python)]
    if step := next(((g, p) for g, p in zip(*traces) if g != p), None):
        out.append(f"first trace difference: ghidra {step[0]} python {step[1]}")
    elif len(traces[0]) != len(traces[1]): out.append("trace lengths differ")
    return out


@pytest.mark.ghidra
def test_kernel_parity(parity_fixture, responses):
    arch, family, name, mode = parity_fixture
    assert not (found := differences(*responses[arch, name, mode])), "\n".join(found)


def test_native_parity(native_parity_fixture, native_responses):
    arch, family, name, mode = native_parity_fixture
    f, native, python = native_responses[arch, name, mode]
    assert not (found := differences(f, native, python, defects=False)), "\n".join(found).replace("ghidra", "native")
