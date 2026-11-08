"""Generated calls preserve instruction state, laziness and per-instruction failures."""
import pytest
from sass.assemble import assemble_instruction
from sass.emulate import codegen, cxx, definitions, kernel, machine
from sass.semantics import bitvec, evaluate, lifter, ordered
from sass.semantics.runtime import Runtime, UnsupportedSemantics
from tests.test_definition_functions import nested, overload, primitive, register, toowide, wide

pytestmark = pytest.mark.native


def semantics(writes, guard=None):
    outer = ordered.Builder()
    condition = outer.capture(guard) if guard is not None else None
    builder = ordered.Builder(outer.count)
    builder.assign_from_inputs((location.args, value) for location, value in writes)
    if condition is not None: outer.emit(ordered.If(condition, builder.block()))
    return lifter.Semantics("SM89", "definitions", "definitions", 0x1000, "native",
                            body=outer.block() if condition is not None else builder.block())


def same_state(got, want):
    assert got.regs == {key: value for key, value in want.regs.items() if value}
    assert got.pc == want.pc


def test_leading_zero_count_widths():
    widths = (1, 3, 8, 16, 32, 64, 65, 127, 128)
    source, output = bitvec.reg('R', 4, 4), bitvec.reg('R', 0, 1)
    expressions = [bitvec.zext(bitvec.extract(bitvec.op('lzcount', bitvec.extract(source, width - 1, 0)),
                                           min(width, 32) - 1, 0), 32)
                   for width in widths]
    library = machine.Fragments([semantics([(output, expression)]) for expression in expressions], semantics=True)
    for index, width in enumerate(widths):
        for value in (0, 1, 1 << (width - 1), bitvec.mask(width)):
            state = evaluate.State({('R', 4 + lane): value >> (32 * lane) & 0xffffffff for lane in range(4)})
            got = library.execute(index, state, {})
            assert got.regs.get(('R', 0), 0) == width - value.bit_length()


def test_ordered_mufu_captures_and_skips_unselected_failures():
    from sass.semantics import primitive
    builder = ordered.Builder()
    source = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(0, 32)))
    value = bitvec.ite(bitvec.reg("P", 0, 1, 1), primitive.mufu("EX2", source),
                      primitive.mufu("RCP", source))
    builder.assign_from_inputs([(("R", 1, 1), value)])
    sem = lifter.Semantics("SM89", "mufu", "mufu", 0x1000, "native", body=builder.block())
    library = machine.Fragments([sem], semantics=True)
    state = evaluate.State({("R", 0): 0x3f800000})
    found = library.execute(0, state, {})
    assert found.read("R", 0, 1) == 0 and found.read("R", 1, 1) == 0x3f800000
    state.regs["P", 0] = 1
    with pytest.raises(UnsupportedSemantics, match="MUFU approximation"):
        library.execute(0, state, {})


@pytest.mark.parametrize("arch", ["SM89", "SM100"])
@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("cls,fields,inputs", [
    ("bmov_clear__Rd", dict(Rd=8, cbu_state="B0", clear=0), [0, 0]),
    ("bmov_clear__Rd", dict(Rd=8, cbu_state="B0", clear=1), [0, 1]),
    ("bmov_clear_barrier_", dict(barReg="B2", Ba="B3"), [1, 2, 3]),
    ("bmov_clear_bd__Bd", dict(barReg="B2", cbu_state="MEXITED"), [1, 2, 24]),
    ("bmov_dst64__R", dict(Rb=4), [1, 0x12345678]),
    ("bmov_pquad__RRR", dict(pquad=1, cbu_state="MACTIVE", Rb=4), [1, 1, 26, 0x12345678]),
    ("bmov_pquad_bar__RBR", dict(pquad=0, cbu_state="B0", barReg="B3"), [1, 0, 0, 3]),
    ("break_inst_", dict(Pp=1, barReg="B2"), [1, 2]),
])
def test_convergence_environment_operations_preserve_inputs_and_predication(arch, fp, cls, fields, inputs):
    from sass.assemble import encoding
    code = bytes.fromhex(encoding.encode(arch, cls, **fields))
    semantics = lifter.lift(arch, code, 0x1000, fp)
    assert semantics.body is not None
    library = machine.Fragments([semantics], semantics=True)
    read = "Rd" in fields
    op = "break" if cls.startswith("break") else "bmov"
    initial = evaluate.State({("R", 4): 0x12345678, ("P", 1): 1})
    context = dict(synchronization={op: {"0": 37} if read else True})
    environment = Runtime(int(arch[2:]), context)
    expected = evaluate.execute(semantics, initial.copy(), runtime=environment)
    assert environment.events == [dict(op=op.upper(), inputs=inputs)]
    same_state(library.execute(0, initial, context), expected)
    if read: assert expected.regs["R", 8] == 37
    if op == "break":
        initial.regs["P", 1] = 0
        environment = Runtime(int(arch[2:]), {})
        expected = evaluate.execute(semantics, initial.copy(), runtime=environment)
        assert environment.events == []
        same_state(library.execute(0, initial, {}), expected)


@pytest.mark.parametrize("spec", [*ordered.BARRIER_ACKS.values(), *ordered.WARP_ACKS.values()],
                         ids=lambda spec: spec.name)
def test_synchronization_acknowledgements_preserve_typed_inputs(spec):
    arguments = tuple(bitvec.const((1 << width) - 1 - index if width > 1 else 0, width)
                      for index, width in enumerate(spec.arguments))
    builder = ordered.Builder()
    builder.special(spec, *arguments)
    sem = lifter.Semantics("SM90", spec.name, spec.name, 0x1000, "primitive", body=builder.block())
    op = "bar" if spec in ordered.BARRIER_ACKS.values() else "warpsync"
    context = dict(synchronization={op: True})
    environment = Runtime(90, context)
    initial = evaluate.State()
    expected = evaluate.execute(sem, initial, runtime=environment)
    assert environment.events == [dict(op=op.upper(), inputs=[1, *map(bitvec.value, arguments)])]
    library = machine.Fragments([sem], semantics=True)
    same_state(library.execute(0, initial, context), expected)
    with pytest.raises(UnsupportedSemantics, match="synchronization context required"):
        library.execute(0, initial, {})


@pytest.mark.parametrize("enabled", [0, 1])
def test_constant_bank_warpsync_reads_only_its_selected_arm(enabled):
    from sass.assemble import encoding
    code = bytes.fromhex(encoding.encode("SM89", "warpsync__RCR", Pp=1, Sb_bank=0, Sb_addr=0x40))
    sem = lifter.lift("SM89", code, 0x1000)
    class SelectedLoads(evaluate.State):
        def load(self, *args):
            assert enabled, "disabled WARPSYNC read its mask"
            return super().load(*args)
    initial = SelectedLoads({("P", 1): enabled})
    if enabled: initial.store("cbank", 0x40, 32, 0x12345678)
    context = dict(synchronization={"warpsync": True}) if enabled else {}
    environment = Runtime(89, context)
    evaluate.apply(sem, initial, runtime=environment)
    assert environment.events == ([dict(op="WARPSYNC", inputs=[1, 0x12345678])] if enabled else [])
    state = evaluate.State({("P", 1): enabled}, memory=initial.memory.copy())
    same_state(machine.Fragments([sem], semantics=True).execute(0, state, context), initial)


def test_collector_is_optional_and_functions_precede_bodies():
    x = bitvec.reg("R", 0, 1)
    s = semantics([(x, nested(overload(x)))])
    uses = set()
    assert "sass::def::test_nested" in cxx.instruction(s, "f", None, uses)
    assert uses == {("test.nested", (32,)), ("test.overload", (32,))}
    assert "sass::def::" not in cxx.instruction(s, "f", None)
    source = codegen.semantic_fragments([s, s])
    assert source.count(" test_nested(") == source.count(" test_overload(") == 1
    assert source.index(" test_nested(") < source.index("static void f0")
    assert "test_overload" not in source[source.index(" test_nested("):source.index("namespace sass::def {", source.index(" test_nested("))]
    assert "namespace sass::def" not in codegen.semantic_fragments([s], False)


def test_complete_domain_runner_binds_both_backends():
    from tests.harness import native_full
    # Compile both explicit namespaces with the host default and exercise boundary chunks.
    specs = [native_full.floating("add", "RN"), native_full.floating("convert", "RM", 32, 16)]
    fn = native_full.compiled(specs)
    for i, spec in enumerate(specs):
        for start in (0, spec.total - 64):
            assert native_full.chunk(fn, i, spec, start, 64) == 64


def test_signatures_are_validated_before_merging(monkeypatch):
    x = bitvec.reg("R", 0, 1)
    nested(x); overload(x)
    functions = codegen.Definitions()
    monkeypatch.setattr(definitions, "definition_name", lambda label, widths: "collision")
    functions.add({("test.nested", (32,))})
    before = functions.source()
    with pytest.raises(NotImplementedError, match="ambiguous"):
        functions.add({("test.overload", (32,)), ("test.overload", (32, 32))})
    assert functions.source() == before


@pytest.mark.parametrize("calls", (False, True))
def test_nested_overloads_wide_and_aliasing(calls):
    x, y, z = bitvec.reg("R", 0, 1), bitvec.reg("R", 1, 1), bitvec.reg("R", 4, 4)
    s = semantics([(x, nested(overload(x, (y,)))), (y, overload(x)), (z, wide(z))])
    library = machine.Fragments([s, s], semantics=True, definition_calls=calls)
    for value in (0, 1, 0xffffffff, 0x7fc00001):
        state = evaluate.State({("R", 0): value, ("R", 1): 0x12345678,
                                **{("R", 4 + i): 0x10203040 * (i + 1) for i in range(4)}})
        want = evaluate.execute(s, state)
        for i in (0, 1):
            got = library.execute(i, state, {})
            same_state(got, want)


@pytest.mark.parametrize("calls", (False, True))
def test_guard_and_lazy_primitive_arguments(calls):
    outer = ordered.Builder()
    guard = outer.read_register("P", 1)
    builder = ordered.Builder(outer.count)
    condition = builder.read_register("P", 0)
    def invalid(arm):
        source = arm.read_register("R", 0)
        snapshot, = arm.special(ordered.CAPTURE, bitvec.const(0, 32), bitvec.const(0xffffffff, 32), source)
        value, = arm.special(ordered.READ_LANE, snapshot, bitvec.const(32, 32))
        arm.special(ordered.RELEASE_SNAPSHOT, snapshot)
        return nested(value),
    value, = builder.if_else(condition, invalid, lambda arm: (nested(arm.read_register("R", 0)),))
    builder.emit(ordered.WriteRegister(("R", 0, 1), value))
    outer.emit(ordered.If(guard, builder.block()))
    s = lifter.Semantics("SM89", "definitions", "definitions", 0x1000, "primitive", body=outer.block())
    library = machine.Fragments([s], semantics=True, definition_calls=calls)
    context = {"warp": {"lane": 0, "values": [0] * 32}}
    for guard, condition in ((0, 0), (0, 1), (1, 0)):
        state = evaluate.State({("R", 0): 7, ("P", 0): condition, ("P", 1): guard})
        want = evaluate.execute(s, state, runtime=Runtime(89, context))
        got = library.execute(0, state, context)
        same_state(got, want)
    with pytest.raises(UnsupportedSemantics, match="snapshot source is inactive"):
        library.execute(0, evaluate.State({("P", 0): 1, ("P", 1): 1}), context)


@pytest.mark.parametrize("fn", (register, primitive))
def test_stateful_numerical_definitions_are_rejected(fn):
    builder = ordered.Builder()
    source = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 1, 1), fn(source)))
    body = builder.block()
    with pytest.raises(TypeError, match="state access inside a numerical definition"):
        ordered.validate(body)
    sem = lifter.Semantics("SM89", "definitions", "definitions", 0x1000, "native", body=body)
    with pytest.raises(TypeError, match="state access inside a numerical definition"):
        codegen.semantic_fragments([sem])


def test_unsupported_function_fault_does_not_pollute_translation_unit():
    x = bitvec.reg("R", 0, 1)
    bad = semantics([(x, nested(x)), (bitvec.reg("R", 8, 8), toowide(bitvec.reg("R", 4, 4)))])
    good = semantics([(x, overload(x))])
    source = codegen.semantic_fragments([bad, good])
    assert " test_nested(" not in source  # even the valid earlier use rolls back
    assert " test_overload(" in source
    library = machine.Fragments([bad, good], semantics=True)
    with pytest.raises(UnsupportedSemantics, match="not compiled:.*256-bit"):
        library.execute(0, evaluate.State(), {})
    assert library.execute(1, evaluate.State({("R", 0): 5}), {}).read("R", 0, 1) == 5


@pytest.mark.parametrize("arch", ("SM89", "SM90"))
@pytest.mark.parametrize("fp", ("exact", "readable"))
def test_fragments_and_kernel_match_inline(arch, fp):
    texts = ["FMNMX.FTZ R0, R0, R1, PT", "FMNMX.FTZ R0, R0, R1, PT",
             "PRMT R0, R0, 0x3210, R1"]
    words = [assemble_instruction(arch, text) for text in texts]
    instructions = [(arch, word, 0x1000 + 16 * i) for i, word in enumerate(words)]
    sources = [codegen.fragments(instructions, fp, calls) for calls in (False, True)]
    assert "sass::def::" not in sources[0]
    name = "min_f32" if fp == "exact" else "min_readable_f32"
    assert sources[1].count(f" {name}(") == 1
    state = evaluate.State({("R", 0): 0x80000001, ("R", 1): 0x3f800000})
    for calls in (False, True):
        library = machine.Fragments(instructions, fp, definition_calls=calls)
        for i, (_, word, address) in enumerate(instructions):
            want = evaluate.execute(lifter.lift(arch, word, address, fp), state)
            got = library.execute(i, state, {})
            same_state(got, want)
    code = b"".join([*words, assemble_instruction(arch, "STG [R10], R0"),
                     assemble_instruction(arch, "EXIT")])
    request = {"kernel": {"code": code.hex(), "address": 0x1000, "threads": 32, "block_size": 32,
                          "registers": {"R0": 0x80000001, "R1": 0x3f800000, "R10": 0x2000}},
               "output": {"address": 0x2000, "size": 4, "stride": 0}}
    # Both functions run under the real scheduler, including instruction pre-state.
    results = [kernel.Kernel(arch, code, 0x1000, fp, calls).run(request, workers=1)
               for calls in (False, True)]
    assert results[0] == results[1]
    assert len(results[0]["outputs"]) == 32
