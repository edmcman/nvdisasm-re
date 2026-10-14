"""The GPU fixtures' whole kernels, run in the emulator, compute their references (no GPU needed;
test_refs_on_gpu checks the references against hardware). One item per (fixture, fp lowering)."""
import pytest
from tests.harness.select import arches, families, modes, require_languages, selected, skipped


def pytest_generate_tests(metafunc):
    if "emulated_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "emulated_fixture", "ghidra")
    if "SM89" not in arches(metafunc.config): return skipped(metafunc, "emulated_fixture", "ghidra", "GPU fixtures are SM89: --arch does not select it")
    from tests.harness.hardware import fixtures
    params = [(family, f.name, mode) for family, f in fixtures(tuple(families(metafunc.config))) for mode in f.modes if mode in modes(metafunc.config)]
    metafunc.parametrize("emulated_fixture", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def emulated_results(request):
    from tests.harness import hardware
    selected_modes = modes(request.config)
    require_languages([("SM89", mode) for mode in selected_modes])
    return hardware.emulate([f for _, f in hardware.fixtures(tuple(families(request.config)))], selected_modes)


@pytest.mark.ghidra
def test_fixtures_emulated(emulated_fixture, emulated_results):
    family, name, mode = emulated_fixture
    assert not (found := emulated_results[name, mode]), "\n".join(found)
