import pytest
from sass import ghidra_bridge
from sass.emulate import kernel, reference
from tests.harness import synchronization, warp_sync
from tests.harness.select import arches, modes, require_languages, selected, skipped


@pytest.mark.parametrize("arch,source", [
    ("SM89", "immediate"), ("SM89", "register"), ("SM89", "uniform"),
    ("SM90", "immediate"), ("SM90", "register"),
])
@pytest.mark.parametrize("threads", [7, 32, 37])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_ordered_warp_synchronization(arch, source, threads, schedule):
    request = synchronization.request(warp_sync.body(arch, source, threads), threads, 64, schedule, arch)
    expected = [value.to_bytes(4, "little").hex() for value in warp_sync.expected(threads)]
    python = reference.run_kernel(request, arch)
    assert python.get("outputs") == expected, python
    assert kernel.run_kernel(request, arch) == python


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("predicate", ["@!PT ", ""])
def test_false_predicates_do_not_enter_exclusive_synchronization(arch, predicate):
    request = synchronization.request(f"MOV R14, 0;\n{predicate}WARPSYNC.EXCLUSIVE !PT, R14;\nMOV R8, 7;", arch=arch)
    python = reference.run_kernel(request, arch)
    assert python.get("outputs") == ["07000000"] * 32, python
    assert kernel.run_kernel(request, arch) == python


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_deferred_barrier_completes_before_warp_arrival(arch, schedule):
    request = synchronization.request("BAR.SYNC.DEFER_BLOCKING 0;\n" + warp_sync.body(arch, "immediate", 37),
                                      37, 64, schedule, arch)
    python = reference.run_kernel(request, arch)
    assert python.get("outputs") == [value.to_bytes(4, "little").hex() for value in warp_sync.expected(37)], python
    assert kernel.run_kernel(request, arch) == python


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("instruction,error", [
    ("WARPSYNC R14;", "omits the lane"),
    ("WARPSYNC.EXCLUSIVE PT, R14;", "unsupported exclusive warp synchronization"),
])
def test_invalid_warp_synchronization(arch, instruction, error):
    request = synchronization.request(f"MOV R14, 0;\n{instruction}", arch=arch)
    assert error in reference.run_kernel(request, arch).get("error", "")
    assert error in kernel.run_kernel(request, arch).get("error", "")


def pytest_generate_tests(metafunc):
    if "language" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "language", "ghidra")
    metafunc.parametrize("language", [(arch, mode) for arch in arches(metafunc.config)
                                      if arch in ("SM89", "SM90") for mode in modes(metafunc.config)])


@pytest.mark.ghidra
def test_production_warp_synchronization_ghidra(language):
    arch, mode = language
    require_languages([language])
    requests = [(synchronization.request(warp_sync.body(arch, source, threads), threads, 64, schedule, arch),
                 [value.to_bytes(4, "little").hex() for value in warp_sync.expected(threads)])
                for source in (("immediate", "register", "uniform") if arch == "SM89" else ("immediate", "register"))
                for threads in (7, 32, 37) for schedule in ("round_robin", "reverse", "seeded")]
    for result, (_, expected) in zip(ghidra_bridge.emulate(arch, mode, [request for request, _ in requests]), requests):
        assert result.get("outputs") == expected, result
