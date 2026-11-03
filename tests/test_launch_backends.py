import pytest
from sass import ghidra_bridge, launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import kernel, reference
from tests.harness import gpu, hardware, launch_geometry
from tests.harness.select import arches, modes, require_languages


@pytest.mark.parametrize("arch", ("SM89", "SM90"))
@pytest.mark.parametrize("engine", [
    pytest.param("native", marks=pytest.mark.native),
    pytest.param("ghidra", marks=pytest.mark.ghidra),
])
def test_geometry_backend(arch, engine, request):
    if arch not in arches(request.config) or "exact" not in modes(request.config):
        pytest.skip("architecture/lowering not selected")
    fixtures = launch_geometry.fixtures(arch)
    if engine == "ghidra": require_languages([(arch, "exact")])
    run = dict(native=kernel.emulate_sharded, ghidra=ghidra_bridge.emulate_sharded)[engine]
    results = run({arch: (arch, "exact", [gpu.request(f) for f in fixtures])})[arch]
    for f, result in zip(fixtures, results):
        assert not hardware.emulated_problems(f, result), (f.name, result)
    if engine == "ghidra":
        unsupported = gpu.request(fixtures[0])
        unsupported["kernel"]["scope"] = "launch"
        assert "does not support launch scope" in run({arch: (arch, "exact", [unsupported])})[arch][0]["error"]
    traced = gpu.request(fixtures[1])
    traced["kernel"].update(trace=True, trace_threads=fixtures[1].threads)
    result = run({arch: (arch, "exact", [traced])})[arch][0]
    assert [step["thread"] for step in result["trace"]] == sorted(step["thread"] for step in result["trace"])
    assert set(step["thread"] for step in result["trace"]) == set(range(fixtures[1].threads))
    fault = launch.kernel_request(cubin.kernel_image(assemble_cubin(f""".target {arch}
.kernel diagnostic
.registers 4, 0
S2R R0, SR_CTAID.X;
ISETP.NE.U32.AND P0, PT, R0, RZ, PT;
MOV R0, 1;
@P0 WARPSYNC {"0x1" if arch == "SM89" else "R0"};
EXIT;
.endkernel
"""), "diagnostic"), (), [], 34, 17, dict(address=0x200000000),
                                  dict(synchronization=dict(warpsync=True)), scope="block")
    expected = reference.run_kernel(fault, arch)
    assert "thread 18:" in expected["error"] and "omits the lane" in expected["error"]
    assert run({arch: (arch, "exact", [fault])})[arch][0] == expected


@pytest.mark.gpu
def test_geometry_gpu(request):
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(launch_geometry.fixtures(arch)).values())
