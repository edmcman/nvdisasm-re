import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.emulate import machine
from sass.semantics import evaluate, lifter
from sass.semantics.runtime import Runtime
from sass.sleigh import modifiers
from tests.harness import autocases


def cases(arch, mode, count, uniform=False):
    cls = ("ldsm__URsIR" if arch == "SM75" else "ldsm__UR_sI_R") if uniform else "ldsm__sImmOffset"
    word = bytes.fromhex(encoding.encode(arch, cls, Rd=8, Ra=4, sz="16", mode=mode, num=str(count)))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    return autocases.generate(arch, cls, form, native=False, sample=100)


def state(case):
    s = evaluate.State()
    for name, value in case.request["registers"].items():
        file = name.rstrip("0123456789")
        s.write(file, int(name[len(file):]), 1, value)
    for region in case.request.get("memory", []):
        data = bytes.fromhex(region["hex"])
        s.store(region["space"], region["address"], len(data) * 8, int.from_bytes(data, "little"))
    return s


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("mode,count,uniform", [("M88", 1, False), ("M88", 4, True), ("MT88", 2, False), ("MT88", 4, True)])
def test_matrix_lane_mapping(arch, mode, count, uniform):
    for case in cases(arch, mode, count, uniform):
        context = case.request["context"]
        sem = lifter.lift(arch, bytes.fromhex(case.request["word"]))
        assert sem.status == "primitive", sem.reason
        out = evaluate.execute(sem, state(case), runtime=Runtime(int(arch[2:]), context))
        assert all(out.read("R", int(name[1:]), 1) == value for name, value in case.expected.items())


@pytest.mark.native
@pytest.mark.parametrize("mode", ["M88", "MT88"])
def test_native_matrix_context(mode):
    selected = cases("SM89", mode, 4, True)
    fragment = machine.Fragments([("SM89", bytes.fromhex(case.request["word"]), 0) for case in selected])
    for j, case in enumerate(selected):
        found = fragment.execute(j, state(case), case.request["context"])
        assert all(found.read("R", int(name[1:]), 1) == value for name, value in case.expected.items())


def arithmetic_cases(arch):
    word = bytes.fromhex(encoding.encode(arch, "hmma_x8_", size="16816", srcfmt="F16", dstfmt="F32",
                                        Ra=16, Rb=24, Rc=32, Rd=40))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    return autocases.generate(arch, "hmma_x8_", form, native=False, sample=100)


@pytest.mark.parametrize("arch", ["SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
def test_tensor_arithmetic(arch):
    selected = arithmetic_cases(arch)
    semantics = {word: lifter.lift(arch, bytes.fromhex(word)) for word in {c.request["word"] for c in selected}}
    assert all(sem.status == "primitive" for sem in semantics.values())
    for case in selected:
        found = evaluate.execute(semantics[case.request["word"]], state(case),
                                 runtime=Runtime(int(arch[2:]), case.request["context"]))
        assert all(found.read("R", int(name[1:]), 1) == value for name, value in case.expected.items()), case.hardware


@pytest.mark.native
def test_native_tensor_arithmetic():
    selected = arithmetic_cases("SM89")
    indices = {word: j for j, word in enumerate(dict.fromkeys(c.request["word"] for c in selected))}
    fragment = machine.Fragments([("SM89", bytes.fromhex(word), 0) for word in indices])
    for case in selected:
        found = fragment.execute(indices[case.request["word"]], state(case), case.request["context"])
        assert all(found.read("R", int(name[1:]), 1) == value for name, value in case.expected.items()), case.hardware


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_tensor_warp_snapshot(arch, order):
    from sass.emulate import kernel, reference
    from tests.cases import matrix
    from tests.harness import gpu, hardware
    for fixture in matrix.gpu_fixtures(arch):
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        result = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, result), (fixture.name, result)
        assert kernel.run_kernel(request, arch) == result
