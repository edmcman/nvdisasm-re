import pytest
from functools import cache
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


SHAPES = (("F16", "16816", "F32"), ("F16", "16816", "F16"), ("BF16", "16816", "F32"),
          ("F16", "1688", "F32"), ("F16", "1688", "F16"), ("BF16", "1688", "F32"),
          ("TF32", "1684", "F32"), ("TF32", "1688", "F32"))


def test_half_accumulator_promotes_every_encoding():
    from sass.semantics import bitvec
    from sass.semantics.families import matrix
    from tests.refs import float
    expression = bitvec.lowered(matrix.half_accumulator(bitvec.param("x", 16)))
    initial = evaluate.State()
    for bits in range(1 << 16):
        assert evaluate.evaluate(expression, initial, {"x": bits}) == float.f2f(bits, 16, 32, "RN"), bits


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
def test_dense_tensor_plans_cover_each_control_combination(arch):
    actual = {(s["size"], s["srcfmt"], s["dstfmt"], s["Ra@negate"], s["Rb@negate"], s.get("UPp@not", "unset"))
              for entry in autocases.saved_plan(arch)["forms"] if entry["cls"] == "hmma_x8_"
              for s in (entry["settings"],)}
    inversions = ("unset", "set") if arch not in ("SM75", "SM80") else ("unset",)
    expected = {(size, source, destination, a, b, p) for source, size, destination in SHAPES
                for a in ("unset", "set") for b in ("unset", "set") for p in inversions} if arch != "SM75" else set()
    assert actual == expected


@cache
def arithmetic_cases(arch, source="F16", size="16816", controls=(0, 0, 0), destination="F32"):
    flags = {key: value for key, value in zip(("Ra@negate", "Rb@negate", "UPp@not"), controls)
             if key != "UPp@not" or "UPp" in encoding.klass(arch, "hmma_x8_").operand_types}
    word = bytes.fromhex(encoding.encode(arch, "hmma_x8_", size=size, srcfmt=source, dstfmt=destination,
                                        Ra=16, Rb=24, Rc=32, Rd=40, **flags))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    return autocases.generate(arch, "hmma_x8_", form, native=False, sample=100)


def assert_arithmetic(arch, source, size, controls=(0, 0, 0), destination="F32"):
    selected = arithmetic_cases(arch, source, size, controls, destination)
    semantics = {word: lifter.lift(arch, bytes.fromhex(word)) for word in {c.request["word"] for c in selected}}
    assert all(sem.status == "primitive" for sem in semantics.values())
    for case in selected:
        found = evaluate.execute(semantics[case.request["word"]], state(case),
                                 runtime=Runtime(int(arch[2:]), case.request["context"]))
        assert all(found.read("R", int(name[1:]), 1) == value for name, value in case.expected.items()), case.hardware


@pytest.mark.parametrize("arch", ["SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("source,size,destination", SHAPES)
def test_tensor_arithmetic(arch, source, size, destination):
    assert_arithmetic(arch, source, size, destination=destination)


@pytest.mark.parametrize("source,size,destination", SHAPES)
@pytest.mark.parametrize("controls", [(a, b, p) for a in (0, 1) for b in (0, 1) for p in (0, 1) if a or b or p])
def test_tensor_operand_controls(source, size, destination, controls):
    assert_arithmetic("SM89", source, size, controls, destination)


@pytest.mark.native
@pytest.mark.parametrize("source,size,destination", SHAPES)
@pytest.mark.parametrize("controls", [(a, b, p) for a in (0, 1) for b in (0, 1) for p in (0, 1)])
def test_native_tensor_arithmetic(source, size, destination, controls):
    selected = arithmetic_cases("SM89", source, size, controls, destination)
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
