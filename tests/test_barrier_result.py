import pytest
from sass.emulate import kernel, reference
from sass.sleigh.build import ARCHES
from tests.harness import gpu, hardware, synchronization


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("mode", ["BAR", "WARP", "RESULT"])
def test_barrier_read_profiles(arch, mode):
    from sass.emulate import machine
    from tests.harness import autocases, lifted
    cases = autocases.generate(arch, f"b2r__{mode}", {"mode": mode}, profiles=["effect_forms"], native=False)
    library = machine.Fragments([(arch, bytes.fromhex(case.request["word"]), 0x1000) for case in cases])
    for index, case in enumerate(cases):
        assert not lifted.run(arch, "exact", case)[1]
        found = library.execute(index, lifted.state(arch, case.request), case.request["context"])
        assert lifted.observe(found, case.expected) == case.expected


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_barrier_results(arch, order):
    for fixture in [*synchronization.reductions(arch), *synchronization.barrier_states(arch)]:
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), result
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.gpu
def test_barrier_results_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    results = hardware.run([*synchronization.reductions(arch), *synchronization.barrier_states(arch)])
    assert not (failures := {name: errors for name, errors in results.items() if errors}), failures


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_barrier_states_ghidra(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    fixtures = [*synchronization.reductions(arch), *synchronization.barrier_states(arch)]
    requests = [dict(r, kernel=dict(r["kernel"], schedule=order))
                for order in ("round_robin", "reverse", "seeded") for r in map(gpu.request, fixtures)]
    expected = [reference.run_kernel(r, arch) for r in requests]
    for fixture, result in zip(fixtures * 3, expected):
        assert not hardware.emulated_problems(fixture, result), result
    results = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    assert all(found == expected for found in results.values())


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
