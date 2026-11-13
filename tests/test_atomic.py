import pytest

from sass.assemble import encoding
from sass.decode import pydecode
from sass.emulate import machine
from sass.semantics import evaluate, lifter, ordered
from sass.semantics.runtime import Runtime
from sass.sleigh import modifiers
from tests.harness import autocases, autocase_profiles, lifted
from tests.refs import atomic


@pytest.mark.parametrize("arch", ["SM80", "SM89"])
def test_red_descriptor_add(arch):
    word = bytes.fromhex("8e7900040700000084e1100c00e20f00")
    decoded = pydecode.decode(arch, word)
    assert decoded.klass.name == "red_uniform__memdesc"
    cases = autocases.generate(arch, decoded.klass.name, modifiers.settings(arch, decoded))
    assert cases
    for case in cases:
        status, problems = lifted.run(arch, "exact", case)
        assert status == "native" and not problems, (case.id, problems)


@pytest.mark.parametrize("size", ["F32.FTZ.RN", "F64.RN", "F16x2.RN"])
def test_float_atomic_reference(size):
    from sass.semantics.bitvec import const
    import sass.semantics.families.atomic
    width = 64 if size == "F64.RN" else 32
    for old, value, _ in autocase_profiles.PROFILES["atomic_float_forms"][size]:
        expression = sass.semantics.families.atomic.float_add(size, const(old, width), const(value, width))
        assert evaluate.evaluate(expression, evaluate.State()) == atomic.float_add(size, old, value)


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("size", ["F32.FTZ.RN", "F64.RN", "F16x2.RN"])
@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_float_atomic_forms(arch, size, fp):
    cls = "atomg__RaNonRZ" if int(arch[2:]) < 90 else "atomg_fp__RaNonRZ"
    word = bytes.fromhex(encoding.encode(arch, cls, e="E", sz=size, op="ADD", Ra=4, Rb=12, Rd=8, Pu=1))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    for case in autocases.generate(arch, cls, form, fp, sample=100):
        status, problems = lifted.run(arch, fp, case)
        assert status == "native" and not problems, (case.id, problems)


@pytest.mark.parametrize("width", [32, 64, 128])
@pytest.mark.parametrize("op", ["ADD", "MIN", "MAX", "INC", "DEC", "AND", "OR", "XOR", "EXCH", "CAS"])
def test_atomic_reference_boundaries(width, op):
    from sass.semantics.bitvec import const
    import sass.semantics.families.atomic
    mask = (1 << width) - 1
    for old, value in ((0, 0), (0, 3), (3, 2), (2, 2), (mask, 1), (1 << (width - 1), mask >> 1)):
        for signed in (False, True):
            replacement = const(7, width) if op == "CAS" else None
            result = sass.semantics.families.atomic.update(op, const(old, width), const(value, width), replacement, signed)
            assert evaluate.evaluate(result, evaluate.State()) == atomic.update(op, old, value, width, signed, 7)


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("size", ["32", "64"])
@pytest.mark.parametrize("cas", [False, True])
def test_atomic_snapshots(arch, size, cas):
    cls = "atomg_cas__RaNonRZ" if cas else "atomg__RaNonRZ" if int(arch[2:]) < 90 else "atomg_int__RaNonRZ"
    fields = dict(e="E", sz=size, Ra=4, Rb=12, Rd=8, Pu=1) | (dict(Rc=16) if cas else dict(op="ADD"))
    word = bytes.fromhex(encoding.encode(arch, cls, **fields))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    cases = autocases.generate(arch, cls, form, sample=100)
    for case in cases:
        state = evaluate.State()
        for name, value in case.request["registers"].items():
            state.write(name.rstrip("0123456789"), int(name.lstrip("RUP")), 1, value)
        for region in case.request["memory"]:
            data = bytes.fromhex(region["hex"])
            state.store(region["space"], region["address"], len(data) * 8, int.from_bytes(data, "little"))
        sem = lifter.lift(arch, bytes.fromhex(case.request["word"]))
        assert sem.status == "native", sem.reason
        assert any(isinstance(statement, ordered.Boundary) and statement.enter for statement in ordered.linearize(sem.body))
        out = evaluate.execute(sem, state, runtime=Runtime(int(arch[2:])))
        for name, value in case.expected.items():
            if name.startswith("__"): continue
            assert out.read(name.rstrip("0123456789"), int(name.lstrip("RUP")), 1) == value
        assert out.load("ram", 0x100, int(size)) == int.from_bytes(bytes.fromhex(case.expected["__memory"]), "little")


@pytest.mark.native
def test_native_atomic_alias_transaction():
    word = bytes.fromhex(encoding.encode("SM89", "atomg_cas__RaNonRZ",
        Ra=4, Rb=12, Rc=16, Rd=4, Pu=1, e="E", sz="64"))
    state = evaluate.State()
    state.write("R", 4, 2, 0x100)
    state.write("R", 12, 2, 0x123456789abcdef0)
    state.write("R", 16, 2, 0xfedcba9876543210)
    state.write("P", 1, 1, 1)
    state.store("ram", 0x100, 64, 0x123456789abcdef0)
    sem = lifter.lift("SM89", word)
    expected = evaluate.execute(sem, state, runtime=Runtime(89))
    found = machine.Fragments([("SM89", word, 0)]).execute(0, state, {}, [("ram", 0x100, 8)])
    assert found.regs == {k: v for k, v in expected.regs.items() if v}
    assert found.memory["ram"] == expected.memory["ram"]


@pytest.mark.parametrize("scope", ["CTA", "SM", "GPU", "SYS"])
@pytest.mark.parametrize("semantics", ["SC", "ALL", "nomembar_sem"])
def test_fence_controls(scope, semantics):
    word = bytes.fromhex(encoding.encode("SM89", "membar_", sem=semantics, sco=scope))
    sem = lifter.lift("SM89", word)
    assert sem.status == "native", sem.reason
    runtime = Runtime(89)
    evaluate.execute(sem, evaluate.State(), runtime=runtime)
    assert runtime.events == [dict(op="MEMBAR", semantics="DEFAULT" if semantics == "nomembar_sem" else semantics, scope=scope)]


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("order", ["round_robin", "reverse", "seeded"])
def test_atomic_contention(arch, order):
    from sass.emulate import kernel, reference
    from tests.cases import atomic
    from tests.harness import gpu, hardware
    for fixture in atomic.gpu_fixtures(arch):
        request = gpu.request(fixture)
        request["kernel"]["schedule"] = order
        expected = reference.run_kernel(request, arch)
        assert not hardware.emulated_problems(fixture, expected), expected
        assert kernel.run_kernel(request, arch) == expected


@pytest.mark.gpu
def test_atomic_contention_gpu(request):
    from tests.cases import atomic
    from tests.harness import gpu, hardware
    arch = gpu.target(request.config)
    if arch is None: pytest.skip(request.config._gpu_unavailable)
    assert not any(hardware.run(atomic.gpu_fixtures(arch)).values())
