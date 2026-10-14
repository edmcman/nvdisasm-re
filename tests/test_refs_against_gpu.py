"""Layer 5: the references and the emulated p-code agree with an SM89 GPU on whole patched kernels.
One item per (fixture, fp lowering); GPU kernels run one at a time, emulation per lowering in parallel."""
from functools import cache
import pytest
from tests.harness.select import families, require_languages, selected, skipped


@cache
def fixtures(fams):
    from tests.cases import gpu_fixtures
    return gpu_fixtures(fams)


def pytest_generate_tests(metafunc):
    if "gpu_fixture" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "gpu"): return skipped(metafunc, "gpu_fixture", "gpu")
    params = [(family, f.name, mode) for family, f in fixtures(tuple(families(metafunc.config))) for mode in f.modes]
    metafunc.parametrize("gpu_fixture", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def hardware_results(request):
    from tests.harness import gpu, hardware
    try: gpu.Driver().close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption("--require-gpu"): raise
        pytest.skip(f"no SM89 GPU: {error}")
    require_languages([("SM89", "exact"), ("SM89", "readable")])
    return hardware.run([f for _, f in fixtures(tuple(families(request.config)))])


@pytest.mark.gpu
def test_refs_against_gpu(gpu_fixture, hardware_results):
    family, name, mode = gpu_fixture
    assert not (found := hardware_results[name, mode]), "\n".join(found)
