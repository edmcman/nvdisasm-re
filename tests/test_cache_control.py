import pytest

from sass.decode import ir
from sass.emulate import kernel, machine, reference
from sass.sleigh.build import ARCHES
from tests.harness import autocases, cache_controls, checks, device_cases, device_evidence, gpu, hardware, lifted
from tests.refs import cache


SCHEDULES = [("thread", "round_robin"), ("block", "round_robin"),
             ("launch", "round_robin"), ("launch", "reverse"), ("launch", "seeded")]


def cases(arch):
    for k in ir.load(arch).classes:
        if k.alternate or k.name not in ("cctl__sImmOffset", "cctl__sImmOffset_pf2"): continue
        for operation in ir.load(arch).enums[k.operand_types["cop"].type]:
            if operation not in cache.OPERATIONS or arch == "SM75" and operation in ("PML2", "DML2"): continue
            for e in ("E", "noe"):
                yield from autocases.generate(arch, k.name, dict(e=e, cache="D", cop=operation),
                                               profiles=["effect_forms"], native=False)


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_cache_profiles_and_native_requests(arch, fp):
    selected = list(cases(arch))
    assert selected
    library = machine.Fragments([(arch, bytes.fromhex(c.request["word"]), 0x1000) for c in selected], fp=fp)
    records = machine.Cache(library.lib, library.lib.sass_cache_new())
    expected = []
    try:
        for index, case in enumerate(selected):
            assert not lifted.run(arch, fp, case)[1], case.id
            regions = [(m["space"], m["address"], m["size"]) for m in case.request["observe_memory"]]
            result = library.execute(index, lifted.state(arch, case.request), dict(cache=records), observe=regions)
            for region in case.request["memory"]:
                found = bytes(result.memory["ram"][region["address"] + k] for k in range(128))
                assert found.hex() == region["hex"]
            expected.extend(case.expected["__event_values"])
        assert records.requests() == expected
    finally: library.lib.sass_cache_free(records.state)


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("scope,order", SCHEDULES)
def test_cache_kernel_schedules(arch, scope, order):
    for fixture in cache_controls.fixtures(arch):
        request = gpu.request(fixture)
        request["kernel"].update(scope=scope, schedule=order)
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), result
        assert kernel.run_kernel(request, arch) == result


@pytest.mark.gpu
def test_cache_profiles_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    scenarios = [device_cases.Scenario("control", c, *device_cases.classify(c, arch=arch), arch=arch)
                 for c in cases(arch)]
    found = device_cases.isolated(device_cases.kernels(scenarios))
    device_evidence.record(scenarios, found["results"], found["device"], arch=arch)
    for scenario in scenarios:
        if scenario.kind == "emulator": continue
        result = found["results"][scenario.id]
        assert isinstance(result, dict), result
        assert not device_cases.compare(scenario.case, result["names"], result["values"], arch)


@pytest.mark.gpu
def test_cache_kernel_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    results = hardware.run(cache_controls.fixtures(arch))
    assert not (errors := {name: failures for name, failures in results.items() if failures}), errors


@pytest.mark.ghidra
def test_cache_profiles_ghidra(pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    variants = [(arch, fp) for arch in select.arches(pytestconfig) for fp in select.modes(pytestconfig)]
    select.require_languages(variants)
    selected = {variant: list(cases(variant[0])) for variant in variants}
    results = ghidra_bridge.emulate_sharded({variant: (*variant, [c.request for c in cs])
                                            for variant, cs in selected.items()})
    for variant, found in results.items():
        for case, result in zip(selected[variant], found):
            assert not checks.problems(case.expected, result), (variant, case.id, result)


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_cache_kernel_ghidra(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    requests = [dict(r, kernel=dict(r["kernel"], scope=scope, schedule=order))
                for scope, order in SCHEDULES
                for r in map(gpu.request, cache_controls.fixtures(arch))]
    expected = [reference.run_kernel(r, arch) for r in requests]
    results = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    assert all(found == expected for found in results.values())
