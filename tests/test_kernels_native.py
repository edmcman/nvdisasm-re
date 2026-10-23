"""Whole kernels on the native emulator (sass.emulate.kernel) compute the GPU fixtures' references,
like test_kernels_reference on the lifted semantics, and stop as the scheduler does on the c.kernel
cases (limits, PC outside the kernel, no EXIT): model 1 the non-cooperative fixtures, model 2 the
cooperative ones. One item per (architecture, family, fixture, fp lowering), plus one per kernel case."""
from sass.emulate import kernel
from tests.harness import lifted
from tests.harness.checks import problems
from tests.harness.select import arches, families, modes, selected, skipped

ARCHES = ("SM89", "SM90")


def native_fixtures(config):
    from tests.harness.hardware import fixtures
    return [(arch, family, f) for arch in arches(config) if arch in ARCHES for family, f in fixtures(tuple(families(config)), arch)]


def kernel_cases(config):
    return [(arch, fp, case) for arch in arches(config) for fp in modes(config)
            for _, case in lifted.judged((arch, fp, "control", 0, None)) if "kernel" in case.request]


def pytest_generate_tests(metafunc):
    if "native_fixture" in metafunc.fixturenames:
        if not selected(metafunc.config): return skipped(metafunc, "native_fixture", "unmarked", "unmarked tests not selected by -m")
        params = [(arch, family, f.name, mode) for arch, family, f in native_fixtures(metafunc.config) for mode in f.modes if mode in modes(metafunc.config)]
        metafunc.parametrize("native_fixture", params, ids=["-".join(p) for p in params])
    if "native_kernel_case" in metafunc.fixturenames:
        if not selected(metafunc.config): return skipped(metafunc, "native_kernel_case", "unmarked", "unmarked tests not selected by -m")
        params = kernel_cases(metafunc.config)
        metafunc.parametrize("native_kernel_case", params, ids=[f"{a}-{fp}-{c.cls}" for a, fp, c in params])


def results(config):
    if not hasattr(config, "_native_results"):
        from tests.harness import hardware
        config._native_results = hardware.emulate([f for _, _, f in native_fixtures(config)], modes(config), engine="native")
    return config._native_results


def test_kernels_native(native_fixture, request):
    arch, family, name, mode = native_fixture
    assert not (found := results(request.config)[arch, name, mode]), "\n".join(found)


def test_kernel_cases_native(native_kernel_case):
    arch, fp, case = native_kernel_case
    assert not (found := problems(case.expected, kernel.run_kernel(case.request, arch, fp))), found
