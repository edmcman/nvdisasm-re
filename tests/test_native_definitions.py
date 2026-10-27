"""Generated calls preserve instruction state, laziness and per-instruction failures."""
import pytest
from sass.assemble import assemble_instruction
from sass.emulate import codegen, cxx, definitions, kernel, machine
from sass.semantics import bitvec, evaluate, lifter
from sass.semantics.runtime import Runtime, UnsupportedSemantics
from tests.test_definition_functions import nested, overload, primitive, register, toowide, wide

pytestmark = pytest.mark.native


def semantics(writes, guard=None):
    return lifter.Semantics("SM89", "definitions", "definitions", 0x1000, "native",
                            guard=guard, writes=writes)


def same_state(got, want):
    assert got.regs == {key: value for key, value in want.regs.items() if value}
    assert got.pc == want.pc


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
    x, p = bitvec.reg("R", 0, 1), bitvec.reg("P", 0, 1, 1)
    # An invalid primitive must only run in the selected arm and after the guard.
    bad = bitvec.primitive("read_lane", 32, x, bitvec.const(32, 32))
    s = semantics([(x, bitvec.ite(p, nested(bad), nested(x)))], bitvec.reg("P", 1, 1, 1))
    library = machine.Fragments([s], semantics=True, definition_calls=calls)
    context = {"warp": {"lane": 0, "values": [0] * 32}}
    for guard, condition in ((0, 0), (0, 1), (1, 0)):
        state = evaluate.State({("R", 0): 7, ("P", 0): condition, ("P", 1): guard})
        want = evaluate.execute(s, state, runtime=Runtime(89, context))
        got = library.execute(0, state, context)
        same_state(got, want)
    with pytest.raises(UnsupportedSemantics, match="out of range"):
        library.execute(0, evaluate.State({("P", 0): 1, ("P", 1): 1}), context)


@pytest.mark.parametrize("fn,message", ((register, "reg"), (primitive, "primitive"), (toowide, "256-bit")))
def test_unsupported_function_fault_does_not_pollute_translation_unit(fn, message):
    x = bitvec.reg("R", 0, 1)
    bad = semantics([(x, nested(x)), (bitvec.reg("R", 8, 8), fn(bitvec.reg("R", 4, 4)))]) if fn == toowide else \
          semantics([(x, nested(x)), (bitvec.reg("R", 1, 1), fn(x))])
    good = semantics([(x, overload(x))])
    source = codegen.semantic_fragments([bad, good])
    assert " test_nested(" not in source  # even the valid earlier use rolls back
    assert " test_overload(" in source
    library = machine.Fragments([bad, good], semantics=True)
    with pytest.raises(UnsupportedSemantics, match=f"not compiled:.*{message}"):
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
