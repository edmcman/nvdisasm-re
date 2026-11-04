import pytest
from sass.emulate import kernel, reference
from tests.harness import asynchronous, gpu, hardware


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_async_groups(arch, order):
    for fixture in asynchronous.fixtures(arch):
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), (fixture.name, result)
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.gpu
def test_async_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(asynchronous.fixtures(arch)).values())
