import pytest
from sass.emulate import kernel, machine, reference
from tests.harness import asynchronous, gpu, hardware

pytestmark = pytest.mark.usefixtures("selected_architecture")


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_async_groups(arch, order):
    for fixture in asynchronous.fixtures(arch):
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), (fixture.name, result)
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
@pytest.mark.parametrize("board", [0, 3])
def test_predicated_off_wait_completes_pending_copies_once(monkeypatch, arch, order, board):
    name = "async_predicated_off_wait" if board == 0 else "async_scoreboard3_predicated_off_wait"
    fixture = next(f for f in asynchronous.fixtures(arch) if f.name == name)
    request = gpu.request(fixture)
    request["kernel"]["schedule"] = order
    calls = []
    wait_slots = machine.Async.wait_slots

    def wait(pending, mask):
        lane = pending.lane
        if lane.kernel.lift(lane.pc()).text.startswith("@!PT NOP"):
            address = lane.read("R", 1) + 16
            assert mask == 1 << board
            assert lane.state.load("shared", address, 128) == 0xddeeff0099aabbcc5566778811223344
            calls.append(lane.index)
        wait_slots(pending, mask)

    monkeypatch.setattr(machine.Async, "wait_slots", wait)
    result = reference.run_kernel(request, arch)
    assert not hardware.emulated_problems(fixture, result), result
    assert sorted(calls) == list(range(len(fixture.expected)))


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_predicated_off_wait_ghidra(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    fixtures = [f for f in asynchronous.fixtures(arch) if "predicated_off_wait" in f.name]
    requests = [dict(r, kernel=dict(r["kernel"], schedule=order))
                for order in ("round_robin", "reverse", "seeded") for r in map(gpu.request, fixtures)]
    expected = [reference.run_kernel(r, arch) for r in requests]
    for fixture, result in zip(fixtures * 3, expected):
        assert not hardware.emulated_problems(fixture, result), result
    found = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    assert all(results == expected for results in found.values())


@pytest.mark.gpu
def test_async_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(asynchronous.fixtures(arch)).values())
