"""The references agree with an SM89 GPU on whole patched kernels (the GPU fixtures). Needed only
when fixtures or references change; test_fixtures_emulated checks the emulator against the same
references without a GPU."""
import pytest
from tests.harness.select import families, selected, skipped


def pytest_generate_tests(metafunc):
    if "gpu_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "gpu"): return skipped(metafunc, "gpu_fixture", "gpu")
    from tests.harness.hardware import fixtures
    params = [(family, f.name) for family, f in fixtures(tuple(families(metafunc.config)))]
    metafunc.parametrize("gpu_fixture", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def gpu_results(request):
    from tests.harness import gpu, hardware
    try: gpu.Driver().close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption("--require-gpu"): raise
        pytest.skip(f"no SM89 GPU: {error}")
    return hardware.run([f for _, f in hardware.fixtures(tuple(families(request.config)))])


@pytest.mark.gpu
def test_refs_on_gpu(gpu_fixture, gpu_results):
    family, name = gpu_fixture
    assert not (found := gpu_results[name]), "\n".join(found)
