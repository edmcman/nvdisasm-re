"""The GPU fixtures' whole kernels on the lifted semantics (`sass.emulate.reference`, no Ghidra) compute
their references, like test_fixtures_emulated on Ghidra. One item per (architecture, family, fixture, fp lowering)."""
from tests.harness.select import arches, families, modes, selected, skipped

ARCHES = ("SM89", "SM90")


def pytest_generate_tests(metafunc):
    if "reference_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config): return skipped(metafunc, "reference_fixture", "unmarked", "unmarked tests not selected by -m")
    from tests.harness.hardware import fixtures
    params = [(arch, family, f.name, mode) for arch in arches(metafunc.config) if arch in ARCHES
              for family, f in fixtures(tuple(families(metafunc.config)), arch) for mode in f.modes if mode in modes(metafunc.config)]
    metafunc.parametrize("reference_fixture", params, ids=["-".join(p) for p in params])


def results(config):
    if not hasattr(config, "_reference_results"):
        from tests.harness import hardware
        targets = [arch for arch in arches(config) if arch in ARCHES]
        config._reference_results = hardware.emulate([f for arch in targets for _, f in hardware.fixtures(tuple(families(config)), arch)],
                                                     modes(config), engine="reference")
    return config._reference_results


def test_kernels_reference(reference_fixture, request):
    arch, family, name, mode = reference_fixture
    assert not (found := results(request.config)[arch, name, mode]), "\n".join(found)
