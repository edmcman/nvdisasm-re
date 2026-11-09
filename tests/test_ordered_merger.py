from sass.semantics import bitvec, ordered
import sass.sleigh.ordered
import pytest


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90",
                                  "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("protocol", ["warp", "convergence", "environment", "acknowledgement"])
def test_synchronization_classes_have_typed_ordered_bodies(arch, fp, protocol):
    from sass.semantics import family, operands
    from sass.semantics.families import runtime
    from sass.sleigh import gen, lower
    generator = gen.Gen(arch, fp)
    def selected(klass):
        match protocol:
            case "warp": return runtime.supported_warp_sync(klass)
            case "convergence": return klass.mnemonic in ("BSSY", "BSYNC")
            case "environment": return klass.mnemonic in ("BREAK", "BMOV")
            case _: return klass.mnemonic == "BAR" and not runtime.supported_barrier(klass) or \
                           klass.mnemonic == "WARPSYNC" and not runtime.supported_warp_sync(klass)
    classes = [klass for klass in generator.arch.classes if selected(klass)]
    assert classes
    for klass in classes:
        builder = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
        rows = lower.enumerate_rows(builder, family.family_for(klass))
        assert rows
        for assignment, body in rows:
            assert isinstance(body, ordered.Block), (klass.name, assignment, body)
            ordered.validate(body, ordered.parameter_types(body))
            operations = [statement.operation for statement in ordered.linearize(body)
                          if isinstance(statement, ordered.Special)]
            match protocol:
                case "warp":
                    assert len(operations) == 3
                    assert operations[0] in (ordered.WARP_ARRIVE, ordered.WARP_ARRIVE_EXCLUSIVE)
                    assert operations[1:] == [ordered.WARP_WAIT, ordered.WARP_RELEASE]
                case "convergence":
                    assert operations[0] == ordered.COORDINATED
                    if klass.mnemonic == "BSSY":
                        assert ordered.CAPTURE_PREDICATE in operations
                        assert ordered.CONVERGENCE_ACK_ENTER in operations or ordered.CONVERGENCE_ACK_ENTER_MODE in operations
                    else:
                        assert ordered.CONVERGENCE_WAIT in operations and ordered.CONVERGENCE_RELEASE in operations
                        assert ordered.CONVERGENCE_ACK_WAIT in operations or ordered.CONVERGENCE_ACK_WAIT_MODE in operations
                case "environment":
                    assert len(operations) == 1
                    assert not operations[0].suspends
                    assert operations[0].name.startswith("convergence_")
                case "acknowledgement":
                    assert len(operations) == 1
                    assert operations[0] in (*ordered.BARRIER_ACKS.values(), *ordered.WARP_ACKS.values())
                    assert not operations[0].suspends and operations[0].writes == ("events",)


def test_temporary_reference_does_not_count_as_an_operation():
    from sass.sleigh import lower
    value = ordered.temp(0, 32)
    assert lower.operations(value) == 0
    assert lower.operations(value + 1) == 1


@pytest.mark.parametrize("initial", [0, 0xffffffff])
def test_normalization_keeps_a_reused_calculation_before_later_state_changes(initial):
    from sass.semantics import evaluate
    builder = ordered.Builder()
    before = builder.read_register("R", 0)
    computed = builder.move(before + 1)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(0, 32)))
    after = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 1, 1), computed))
    builder.emit(ordered.WriteRegister(("R", 2, 1), computed + after))
    normalized = sass.sleigh.ordered.normalize(builder.block())
    assert isinstance(normalized.statements[1], ordered.Move)
    assert normalized.statements[2].location == ("R", 0, 1)
    state = evaluate.State({("R", 0): initial})
    assert ordered.Frame(normalized, 16).run(state) == ordered.Status.COMPLETE
    expected = (initial + 1) & 0xffffffff
    assert state.regs == {("R", 0): 0, ("R", 1): expected, ("R", 2): expected}


@pytest.mark.parametrize("spec,args", [
    (ordered.LANE, ()),
    (ordered.ADDRESS_SPACE, (bitvec.const(0x1000, 64),)),
])
def test_unused_environment_query_keeps_the_call_without_a_dead_assignment(spec, args):
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    builder.special(spec, *args)
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert spec.runtime + "(" in code
    assert "q0" not in code


def test_opaque_arguments_keep_their_producing_temporary():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    snapshot, = builder.special(ordered.CAPTURE, bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(23, 32))
    builder.special(ordered.RELEASE_SNAPSHOT, snapshot)
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert "local q0:8;" in code
    assert "q0 = sass_ordered_warp_capture(" in code
    assert "sass_ordered_snapshot_release(q0)" in code


def test_matching_statements_merge_values_at_their_position():
    bodies = []
    for scope in (0, 2):
        builder = ordered.Builder()
        old = builder.read_register("R", 0)
        builder.emit(ordered.WriteRegister(("R", 0, 1), old + 1))
        builder.special(ordered.FENCE, bitvec.const(0, 32), bitvec.const(scope, 32))
        builder.read_register("R", 0)
        bodies.append(builder.block())
    body = sass.sleigh.ordered.merge(list(zip(({"scope": 0}, {"scope": 2}), bodies)))
    assert isinstance(body, ordered.Block)
    assert [type(statement) for statement in body.statements] == [
        ordered.ReadRegister, ordered.WriteRegister, ordered.Special, ordered.ReadRegister]
    assert body.statements[2].arguments[1].op == "sel"
    ordered.validate(body)


def test_incompatible_branches_remain_decode_time_body_alternatives():
    bodies = []
    for space in ("ram", "shared"):
        builder = ordered.Builder()
        yes = ordered.Builder()
        yes.read_memory(space, bitvec.const(16, 64), 32)
        builder.emit(ordered.If(bitvec.const(0, 1), yes.block()))
        bodies.append(builder.block())
    choice = sass.sleigh.ordered.merge(list(zip(({"space": 0}, {"space": 1}), bodies)))
    assert isinstance(choice, sass.sleigh.ordered.Choice)
    for _, body in choice.cases:
        assert len(body.statements) == 1
        assert isinstance(body.statements[0], ordered.If)
        assert isinstance(body.statements[0].yes.statements[0], ordered.ReadMemory)


def test_compatible_decode_values_share_one_guarded_body():
    rows = []
    for selector in range(8):
        builder = ordered.Builder()
        value = builder.read_register("R", selector // 4)
        builder.emit(ordered.WriteRegister(("R", 2, 1), value + selector))
        rows.append(({"selector": selector}, builder.block()))
    choice = sass.sleigh.ordered.merge(rows)
    assert isinstance(choice, sass.sleigh.ordered.Choice)
    assert choice.keys == ("selector",) and len(choice.cases) == 2
    assert [selections[0] for selections, _ in choice.cases] == [frozenset(range(4)), frozenset(range(4, 8))]
    for selections, body in choice.cases:
        assert body.statements[0].location == ("R", min(selections[0]) // 4, 1)
        ordered.validate(body)


def test_different_calculations_using_temporaries_merge_at_their_position():
    bodies = []
    for operator in ("add", "xor"):
        builder = ordered.Builder()
        value = builder.read_register("R", 0)
        builder.emit(ordered.WriteRegister(("R", 1, 1), bitvec.op(operator, value, bitvec.const(1, 32), w=32)))
        bodies.append(builder.block())
    body = sass.sleigh.ordered.merge(list(zip(({"operator": 0}, {"operator": 1}), bodies)))
    assert isinstance(body, ordered.Block)
    assert body.statements[1].value.op == "sel"
    ordered.validate(body)


def test_common_prefix_can_share_completed_effects():
    bodies = []
    for space in ("ram", "shared"):
        body = ordered.Builder()
        before = body.read_register("R", 0)
        body.emit(ordered.WriteRegister(("R", 0, 1), before + 1))
        body.special(ordered.FENCE, bitvec.const(0, 32), bitvec.const(0, 32))
        after = body.read_memory(space, bitvec.const(16, 64), 32)
        body.emit(ordered.WriteRegister(("R", 1, 1), after))
        bodies.append(body.block())
    assert sass.sleigh.ordered.common_prefix(bodies) == 3
    for body in bodies:
        ordered.validate(ordered.Block(body.statements[:3]))
        ordered.validate(ordered.Block(body.statements[3:]))


@pytest.mark.parametrize("nested", [False, True])
def test_common_prefix_keeps_captures_needed_by_the_remaining_body(nested):
    bodies = []
    for space in ("ram", "shared"):
        body = ordered.Builder()
        predicate = body.read_register("P", 0)
        def access(arm):
            loaded = arm.read_memory(space, bitvec.const(16, 64), 32)
            arm.emit(ordered.WriteRegister(("R", 1, 1), loaded))
            return ()
        if nested:
            body.if_else(predicate, access, lambda arm: ())
        else:
            access(body)
            body.emit(ordered.WriteRegister(("P", 1, 1), predicate))
        bodies.append(body.block())
    assert sass.sleigh.ordered.common_prefix(bodies) == 0


def test_common_prefix_keeps_opaque_handles_needed_by_a_branch():
    bodies = []
    for source in (0, 1):
        body = ordered.Builder()
        snapshot, = body.special(ordered.CAPTURE, bitvec.const(0, 32), bitvec.const(3, 32), bitvec.const(23, 32))
        value, = body.special(ordered.READ_LANE, snapshot, bitvec.const(source, 32))
        body.emit(ordered.WriteRegister(("R", 0, 1), value))
        body.special(ordered.RELEASE_SNAPSHOT, snapshot)
        bodies.append(body.block())
    assert sass.sleigh.ordered.common_prefix(bodies) == 0


def test_shared_calculations_preserve_inline_decode_alternatives():
    from types import SimpleNamespace
    from sass.semantics.families import integer
    from sass.sleigh import gen
    rows = []
    for table in (0x96, 0xe8):
        builder = ordered.Builder()
        arguments = [builder.read_register("R", index) for index in range(3)]
        builder.emit(ordered.WriteRegister(("R", 3, 1), integer.logic3(*arguments, table)))
        rows.append(({"imm8": table}, builder.block()))
    choice = sass.sleigh.ordered.merge(rows)
    assert isinstance(choice, sass.sleigh.ordered.Choice)
    for (table,), body in choice.cases:
        code = sass.sleigh.ordered.emit(body, SimpleNamespace(g=gen.Gen("SM90"), refs=[], serial=0)).body()
        if table == 0x96:
            assert "q0" not in code and "PURE" not in code
            assert code.count("^") == 2
        else:
            assert "PURE" in code


def test_shared_calculations_capture_a_source_overwritten_before_the_call():
    from types import SimpleNamespace
    from sass.semantics.families import integer
    from sass.sleigh import gen
    generator = gen.Gen("SM90")
    builder = ordered.Builder()
    arguments = [builder.read_register("R", index) for index in range(3)]
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(10, 32)))
    builder.emit(ordered.WriteRegister(("R", 3, 1), integer.logic3(*arguments, 0xe8)))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=generator, refs=[], serial=0)).body()
    assert code.index("q0 = R0;") < code.index("R0 = 0xa:4;") < code.index("PURE")
    assert generator.scratch_calls and not generator.shared_calls


def test_read_retained_across_a_write_is_not_inlined():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    original = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(10, 32)))
    builder.emit(ordered.WriteRegister(("R", 1, 1), original))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code.index("q0 = R0;") < code.index("R0 = 0xa:4;") < code.index("R1 = q0;")


def test_forwarding_move_preserves_a_read_across_a_source_write():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    original = builder.move(builder.read_register("R", 0))
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(10, 32)))
    builder.emit(ordered.WriteRegister(("R", 1, 1), original))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code.index("q0 = R0;") < code.index("R0 = 0xa:4;") < code.index("R1 = q0;")


def test_forwarding_moves_allow_an_immediate_register_copy():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    value = builder.move(builder.move(builder.read_register("R", 0)))
    builder.emit(ordered.WriteRegister(("R", 1, 1), value))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code == "R1 = R0;"


def test_register_reads_inline_across_other_reads_and_pure_calculations():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    a, b = builder.read_register("R", 0), builder.read_register("R", 1)
    sum_ = builder.move(a + b)
    builder.emit(ordered.WriteRegister(("R", 2, 1), sum_))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert "q0" not in code and "q1" not in code and "R0 + R1" in code


def test_decode_choices_read_forwarded_inputs_without_scratch_copies():
    from sass.semantics import operands
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "fadd__RRR_RR")
    operands_ = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    builder = ordered.Builder()
    value = builder.read_register("R", ordered.DecodedRegister("Ra"))
    choice = lower.sel((("Ra", "negate"),), [((0,), value), ((1,), value ^ 0x80000000)], 32)
    builder.emit(ordered.WriteRegister(("R", ordered.DecodedRegister("Rd"), 1), choice))
    code = sass.sleigh.ordered.emit(builder.block(), operands_).body()
    assert "q0" not in code
    assert not any(symbol.startswith("ORDEREDARG") for symbol in generator.temporaries)


@pytest.mark.parametrize("cheap_arm", [False, True])
def test_large_decode_choices_keep_canonical_captured_arguments(cheap_arm):
    from sass.semantics import operands
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "fadd__RRR_RR")
    operands_ = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    builder = ordered.Builder()
    value = builder.read_register("R", ordered.DecodedRegister("Ra"))
    result = value
    for _ in range(40): result = (result >> 1) + value
    choice = lower.sel((("Ra", "negate"),), [((0,), result), ((1,), value if cheap_arm else result ^ value)], 32)
    builder.emit(ordered.WriteRegister(("R", ordered.DecodedRegister("Rd"), 1), choice))
    code = sass.sleigh.ordered.emit(builder.block(), operands_).body()
    assert ("ORDEREDARG4_0 =" in code) == (not cheap_arm)
    assert "q0" not in code


def test_forwarded_reads_keep_selected_register_width():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    value = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 1, 1), bitvec.zext(bitvec.extract(value, 15, 0), 32)))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert "R0:2" in code and "q0" not in code


def test_retained_half_read_is_captured_before_source_overwrite():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    value = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(10, 32)))
    builder.emit(ordered.WriteRegister(("R", 1, 1), bitvec.zext(bitvec.extract(value, 15, 0), 32)))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code.index("zext(R0:2)") < code.index("R0 = 0xa:4;") < code.index("R1 =")


def test_selected_widths_use_an_existing_capture_after_source_overwrite():
    from sass.semantics import operands
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "fadd__RRR_RR")
    operands_ = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    builder = ordered.Builder()
    value = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(10, 32)))
    selected = lower.sel((("Ra", "negate"),), [
        ((0,), bitvec.zext(bitvec.extract(value, 15, 0), 32)), ((1,), value)], 32)
    builder.emit(ordered.WriteRegister(("R", 1, 1), selected))
    code = sass.sleigh.ordered.emit(builder.block(), operands_).body()
    assert code.index("q0 =") < code.index("R0 =") < code.index("ORDEREDARG4_0 = q0;") < code.index("R1 =")


def test_capture_width_follows_decode_choice_and_all_consumers():
    from sass.sleigh import lower
    value = ordered.temp(0, 32)
    half = bitvec.zext(bitvec.extract(value, 15, 0), 32)
    selected = lower.sel(("width",), [((0,), half), ((1,), value)], 32)
    demands = sass.sleigh.ordered.register_demands([selected])
    assert [(values, bitvec.value(bits)) for values, bits in demands[0].args[1]] == [((0,), 16), ((1,), 32)]
    demands = sass.sleigh.ordered.register_demands([selected, value])
    assert bitvec.value(demands[0]) == 32


def test_capture_width_choices_share_repeated_widths_and_byte_rounding():
    from sass.sleigh import lower
    value = ordered.temp(0, 32)
    selected = lower.sel(("width",), [
        ((index,), bitvec.zext(bitvec.extract(value, 7 if index < 12 else 15, 0), 32))
        for index in range(16)], 32)
    demand = sass.sleigh.ordered.register_demands([selected])[0]
    prefix = sass.sleigh.ordered.selected_prefix(bitvec.reg("R", 0, 1, 32), demand)
    assert len(prefix.args[1]) == 5
    for index in range(16):
        assert bitvec.value(lower.specialize(demand, {"width": index})) == (8 if index < 12 else 16)
    same_byte = lower.sel(("width",), [((index,), bitvec.const(index + 9, 32)) for index in range(8)], 32)
    prefix = sass.sleigh.ordered.selected_prefix(bitvec.reg("R", 0, 1, 32), same_byte)
    assert prefix.op != "sel"


@pytest.mark.parametrize("default", [False, True])
def test_expression_choices_group_equal_arms_in_generated_constructors(default):
    from sass.semantics import operands
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "lop3_lut__RRR_RRR")
    builder = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    cases = [((index,), bitvec.const(index >= 12, 32)) for index in range(16)]
    expression = lower.sel((("bits", "imm8", 0, 4),), cases + [((None,), bitvec.const(0, 32))] if default else cases, 32)
    lower.Lower(builder, lower.Code(), lower.count_uses([expression])).value(expression)
    constructors = next(rows for (prefix, rows) in generator.tables if prefix == "sel")
    assert len(constructors) == 2
    assert {row[-1] for row in constructors} == {"export 0x0:4;", "export 0x1:4;"}


def test_discarded_predicate_outputs_do_not_capture_memory_inputs():
    from sass.semantics import family, operands
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "fchk__RCR_RC")
    builder = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    rows = lower.enumerate_rows(builder, family.family_for(klass), {("zero", "Pu"): True})
    assert rows and all(isinstance(body, ordered.Block) and not body.statements for _, body in rows)


def test_pure_calculations_share_values_within_a_block():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    a, b = builder.read_register("R", 0), builder.read_register("R", 1)
    builder.assign_from_inputs([(("R", 2, 1), a + b), (("R", 3, 1), a + b + 1)])
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code.count("R0 + R1") == 1


def test_captured_assignment_results_are_shared_by_later_right_hand_sides():
    builder = ordered.Builder()
    value = bitvec.reg("R", 0, 1, 32) + 1
    first, second = builder.capture_inputs((value, value != 0))
    assert builder.statements[-1].value.args[0] is first
    builder.emit(ordered.WriteRegister(("R", 0, 1), first))
    builder.emit(ordered.WriteRegister(("P", 0, 1), second))
    ordered.validate(builder.block())


def test_shared_value_tables_preserve_the_ordered_lowerer():
    from types import SimpleNamespace
    from sass.semantics.families import floating
    from sass.sleigh import gen, lower
    generator = gen.Gen("SM89")
    builder = SimpleNamespace(g=generator, refs=[], serial=0)
    value = floating.packed_ftz("add", (bitvec.reg("ORDEREDARG4_", 0, 1, 32),
                                      bitvec.reg("ORDEREDARG4_", 1, 1, 32)))
    code = lower.Code()
    sass.sleigh.ordered.Lower(builder, code, lower.count_uses([value]), {value}).value(value)
    assert not generator.shared_calls
    assert generator.scratch_calls


def test_assignment_copies_do_not_split_compatible_bodies():
    bodies = []
    for copies in (0, 3):
        builder = ordered.Builder()
        value = builder.read_register("R", 0)
        for _ in range(copies): value = builder.move(value + 0)
        builder.emit(ordered.WriteRegister(("R", 1, 1), value + 1))
        bodies.append(builder.block())
    body = sass.sleigh.ordered.merge(list(zip(({"copies": 0}, {"copies": 3}), bodies)))
    assert isinstance(body, ordered.Block)
    assert len(body.statements) == 2
    ordered.validate(body)


def test_normalization_rejects_invalid_unused_assignments():
    body = ordered.Block((ordered.Move(ordered.temp(0, 32), ordered.temp(1, 32)),))
    with pytest.raises(TypeError, match="undefined temporary"):
        sass.sleigh.ordered.normalize(body)


@pytest.mark.parametrize("condition", [0, 1])
def test_pure_branch_results_merge_without_moving_state_accesses(condition):
    from sass.semantics import evaluate
    builder = ordered.Builder()
    original = builder.read_register("R", 0)
    yes = ordered.Builder(builder.count)
    a = yes.move(original + 1)
    no = ordered.Builder(yes.count)
    b = no.move(original + 2)
    result = no.temp(32)
    builder.emit(ordered.If(bitvec.const(condition, 1), yes.block(a), no.block(b), (result,)))
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(100, 32)))
    builder.emit(ordered.WriteRegister(("R", 1, 1), result))
    body = sass.sleigh.ordered.normalize(builder.block())
    assert [type(statement) for statement in body.statements] == [
        ordered.ReadRegister, ordered.WriteRegister, ordered.WriteRegister]
    state = evaluate.State({("R", 0): 7})
    assert ordered.Frame(body, 16).run(state) is ordered.Status.COMPLETE
    assert state.read("R", 1, 1) == 9 - condition


@pytest.mark.parametrize("name", ["wide_register_body", "branch_body", "atomic_body"])
def test_normalization_preserves_state_access_and_faults(name):
    from sass.semantics import evaluate, ordered_runtime
    from tests.harness import ordered_gate
    match name:
        case "branch_body": original = ordered_gate.branch_body(True)
        case "atomic_body": original = ordered_gate.atomic_body(True)
        case _: original = getattr(ordered_gate, name)()
    for body in (original, sass.sleigh.ordered.normalize(original)):
        state = evaluate.State({("R", 0): 7, ("R", 1): 10, ("P", 0): 1})
        state.store("ram", 16, 32, 23)
        runtime = ordered_runtime.Runtime(handlers={"ordered_test_fail":
                                           lambda: (_ for _ in ()).throw(ValueError("gate failure"))})
        frame = ordered.Frame(body, 16)
        status = frame.run(state, runtime)
        match name:
            case "atomic_body":
                assert status is ordered.Status.FAULT and state.load("ram", 16, 32) == 24
                assert runtime.owner is None
            case "branch_body":
                assert status is ordered.Status.COMPLETE and state.read("R", 0, 1) == 23
            case _:
                assert status is ordered.Status.COMPLETE
                assert [state.read("R", index, 1) for index in range(2, 6)] == [4, 3, 7, 10]


def test_calculations_in_one_arm_are_not_reused_by_the_other():
    from types import SimpleNamespace
    from sass.sleigh import gen
    builder = ordered.Builder()
    a, b = builder.read_register("R", 0), builder.read_register("R", 1)
    yes = ordered.Block((ordered.WriteRegister(("R", 2, 1), a + b),))
    no = ordered.Block((ordered.WriteRegister(("R", 2, 1), a + b + 1),))
    builder.emit(ordered.If(bitvec.const(1, 1), yes, no))
    code = sass.sleigh.ordered.emit(builder.block(), SimpleNamespace(g=gen.Gen("SM89"), refs=[], serial=0)).body()
    assert code.count("q0 + q1") == 2


def test_stateful_decode_choices_select_a_body_before_capturing_inputs():
    from types import SimpleNamespace
    from sass.sleigh import lower
    instruction = object.__new__(lower.Symbolic)
    instruction.k = SimpleNamespace(mnemonic="MEMBAR")
    cases = [((0,), bitvec.const(7, 32)),
             ((1,), bitvec.load("ram", bitvec.const(16, 64), 32))]
    def unanswered(key, domain): raise lower.Branch(key, domain)
    instruction.answer = unanswered
    with pytest.raises(lower.Branch) as branch:
        instruction.decode_choice("selector", cases)
    assert branch.value.key == "selector" and branch.value.domain == [0, 1]
    instruction.answer = lambda key, domain: 0
    assert instruction.decode_choice("selector", cases) is cases[0][1]
    assert ordered.accesses_state(lower.sel(("selector",), cases, 32))


def test_numerical_decode_choices_share_compatible_register_captures():
    from types import SimpleNamespace
    from sass.sleigh import lower
    instruction = object.__new__(lower.Symbolic)
    instruction.k = SimpleNamespace(mnemonic="MEMBAR")
    register = bitvec.reg("R", 0, 1, 32)
    expression = instruction.decode_choice("selector", [((0,), register + 1), ((1,), register ^ 3)])
    builder = ordered.Builder()
    value = builder.capture(expression)
    builder.emit(ordered.WriteRegister(("R", 1, 1), value))
    ordered.validate(builder.block())
    assert [type(statement) for statement in builder.statements] == [ordered.ReadRegister, ordered.WriteRegister]
    assert not ordered.accesses_state(value)


@pytest.mark.parametrize("other", [bitvec.reg("R", 1, 1, 32), bitvec.reg("SR", 0, 1, 32)])
def test_decode_choices_with_different_or_fallible_reads_keep_separate_bodies(other):
    from types import SimpleNamespace
    from sass.sleigh import lower
    instruction = object.__new__(lower.Symbolic)
    instruction.k = SimpleNamespace(mnemonic="MEMBAR")
    def unanswered(key, domain): raise lower.Branch(key, domain)
    instruction.answer = unanswered
    with pytest.raises(lower.Branch):
        instruction.decode_choice("selector", [((0,), bitvec.reg("R", 0, 1, 32)), ((1,), other)])


def test_register_capture_does_not_hoist_reads_from_runtime_branches():
    expression = bitvec.ite(bitvec.reg("P", 0, 1, 1), bitvec.reg("R", 0, 1, 32), bitvec.reg("R", 1, 1, 32))
    assert ordered.register_inputs(expression) is None
    builder = ordered.Builder()
    result = builder.capture(expression)
    builder.emit(ordered.WriteRegister(("R", 2, 1), result))
    assert isinstance(builder.statements[1], ordered.If)
    assert builder.statements[1].yes.statements[0].location == ("R", 0, 1)
    assert builder.statements[1].no.statements[0].location == ("R", 1, 1)
    ordered.validate(builder.block())


def test_direct_transfers_preserve_the_target_subtable():
    from sass.semantics import operands
    from sass.sleigh import gen
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "bra_rel__U")
    builder = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    body = ordered.Block((ordered.Transfer("goto", bitvec.param("__target:sImm", 64)),))
    code = sass.sleigh.ordered.emit(body, builder).body()
    assert code.startswith("goto ") and "[" not in code and "local" not in code


def test_runtime_arguments_use_a_branch_targets_address():
    from sass.semantics import operands
    from sass.sleigh import gen
    generator = gen.Gen("SM90")
    klass = next(k for k in generator.arch.classes if k.name == "warpsync_collective__RIR")
    builder = operands.Builder(generator, klass, None, generator.span_variants(klass)[0][0])
    target = bitvec.param("__target:sImm", 64)
    body = ordered.Block((ordered.Special(ordered.WARP_ACKS["collective_all"], (target,)),))
    code = sass.sleigh.ordered.emit(body, builder).body()
    assert "sass_ordered_warp_collective_all_ack(&:8 b" in code
    assert "*[" not in code


def test_dead_calculations_preserve_branch_targets_and_stateful_operations():
    builder = ordered.Builder()
    unused = builder.move(builder.read_register("R", 0) + 1)
    yes = ordered.Builder(builder.count)
    yes.move(unused + 2)
    yes.special(ordered.FENCE, bitvec.const(0, 32), bitvec.const(0, 32))
    no = ordered.Builder(yes.count)
    no.read_memory("ram", bitvec.const(16, 64), 32)
    builder.emit(ordered.If(bitvec.const(1, 1), yes.block(), no.block()))
    statements = sass.sleigh.ordered.prune(ordered.linearize(builder.block()))
    assert [type(statement) for statement in statements] == [
        ordered.Branch, ordered.Special, ordered.Jump, ordered.ReadMemory]
    assert (statements[0].yes, statements[0].no, statements[2].target) == (1, 3, 4)


def test_dead_calculations_keep_opaque_inputs_and_special_register_reads():
    builder = ordered.Builder()
    builder.read_register("SR", 0)
    snapshot, = builder.special(ordered.CAPTURE, bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(3, 32))
    copy = builder.move(snapshot)
    builder.special(ordered.READ_LANE, copy, bitvec.const(0, 32))
    assert len(sass.sleigh.ordered.prune(ordered.linearize(builder.block()))) == 4
