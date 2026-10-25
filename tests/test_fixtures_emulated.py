"""The GPU fixtures' whole kernels, run in the emulator, compute their references (no GPU needed;
test_refs_on_gpu checks the references against hardware). One item per (fixture, fp lowering)."""
import pytest
from tests.harness.select import arches, families, modes, require_languages, selected, skipped


def pytest_generate_tests(metafunc):
    if "emulated_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "emulated_fixture", "ghidra")
    from tests.harness.hardware import fixtures
    params = [(arch, family, f.name, mode) for arch in arches(metafunc.config) if arch in ("SM89", "SM90") for family, f in fixtures(tuple(families(metafunc.config)), arch) for mode in f.modes if mode in modes(metafunc.config)]
    metafunc.parametrize("emulated_fixture", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def emulated_results(request):
    from tests.harness import hardware
    selected_modes = modes(request.config)
    targets = [arch for arch in arches(request.config) if arch in ("SM89", "SM90")]
    require_languages([(arch, mode) for arch in targets for mode in selected_modes])
    return hardware.emulate([f for arch in targets for _, f in hardware.fixtures(tuple(families(request.config)), arch)], selected_modes)


@pytest.mark.ghidra
def test_fixtures_emulated(emulated_fixture, emulated_results):
    arch, family, name, mode = emulated_fixture
    assert not (found := emulated_results[arch, name, mode]), "\n".join(found)
