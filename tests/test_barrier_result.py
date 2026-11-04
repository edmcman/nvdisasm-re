import pytest
from sass.emulate import kernel, reference
from tests.harness import gpu, hardware, synchronization


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_barrier_results(arch, order):
    for fixture in synchronization.reductions(arch):
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), result
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.gpu
def test_barrier_results_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(synchronization.reductions(arch)).values())


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_indirect_branches(arch):
    for fixture in synchronization.indirect_branches(arch):
        request = gpu.request(fixture)
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), result
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.gpu
def test_indirect_branches_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(synchronization.indirect_branches(arch)).values())
