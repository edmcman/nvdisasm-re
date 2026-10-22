"""The references agree with the selected GPU on whole patched kernels (the GPU fixtures). Needed only
when fixtures or references change; test_fixtures_emulated checks the emulator against the same
references without a GPU."""
import pytest
from tests.harness import gpu
from tests.harness.select import families, selected, skipped


def pytest_generate_tests(metafunc):
    if "gpu_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "gpu"): return skipped(metafunc, "gpu_fixture", "gpu")
    arch = gpu.target(metafunc.config)
    if arch is None:
        return metafunc.parametrize('gpu_fixture', [pytest.param(None, marks=pytest.mark.skip(reason=metafunc.config._gpu_unavailable))])
    from tests.harness.hardware import fixtures
    params = [(family, f.name) for family, f in fixtures(tuple(families(metafunc.config)), arch)]
    ids = ['-'.join(p) for p in params]
    metafunc.parametrize("gpu_fixture", params, ids=ids)


@pytest.fixture(scope="session")
def gpu_results(request):
    from tests.harness import gpu, hardware
    if gpu.target(request.config) is None: pytest.skip(request.config._gpu_unavailable)
    try: gpu.Driver(gpu.target(request.config)).close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption("--require-gpu"): raise
        pytest.skip(f"no supported GPU: {error}")
    return hardware.run([f for _, f in hardware.fixtures(tuple(families(request.config)), gpu.target(request.config))])


@pytest.mark.gpu
def test_refs_on_gpu(gpu_fixture, gpu_results):
    family, name = gpu_fixture
    assert not (found := gpu_results[name]), "\n".join(found)
