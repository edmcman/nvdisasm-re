import random
import pytest
from sass.semantics import bitvec, evaluate, family, lifter, ordered, ordered_runtime


from tests.harness.ordered_gate import PAIR, FAIL, register_body, branch_body, atomic_body, suspension_body, snapshot_body


@pytest.mark.parametrize("access", ["reads", "writes"])
def test_operation_rejects_a_bare_state_name(access):
    with pytest.raises(TypeError, match="state accesses"):
        ordered.operation("invalid_state_access", description="Invalid state access", **{access: "events"})
    assert "invalid_state_access" not in ordered.OPERATIONS


def test_family_rejects_an_expression_as_its_body(monkeypatch):
    monkeypatch.setattr(family, "FAMILIES", {})
    @family.family("TEST")
    def invalid(i): return bitvec.const(1, 32)
    with pytest.raises(TypeError, match="family did not return an ordered body"):
        invalid(None)


def execute(body, state=None, runtime=None):
    state = state or evaluate.State()
    frame = ordered.Frame(body, 16)
    assert frame.run(state, runtime) == ordered.Status.COMPLETE, frame.error
    return state


def test_read_after_write():
    state = execute(register_body(), evaluate.State({("R", 0): 7}))
    assert state.regs == {("R", 0): 8, ("R", 1): 8}
    assert state.pc == 16


@pytest.mark.parametrize("board", range(6))
def test_async_commit_dependency_slot_is_explicit(board):
    from sass.assemble import assemble_instruction
    from sass.semantics import runtime
    word = assemble_instruction("SM89", f"LDGDEPBAR @sched(dst_wr_sb={board})")
    semantics = lifter.lift("SM89", word)
    commits = [statement for statement in ordered.linearize(semantics.body)
               if isinstance(statement, ordered.Special) and statement.operation == ordered.ASYNC_COMMIT]
    assert len(commits) == 1
    assert commits[0].arguments == (bitvec.const(board, 32),)
    provider = runtime.Runtime(89, {"async_record": True})
    evaluate.apply(semantics, evaluate.State(), runtime=provider)
    assert provider.events == [dict(op="async_commit", args=[board])]


@pytest.mark.parametrize("arch", ("SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"))
@pytest.mark.parametrize("mask", (1, 8, 63))
def test_encoded_dependency_wait_precedes_predication_without_reads_in_the_untaken_body(arch, mask):
    from sass.assemble import assemble_instruction
    import sass.emulate.ordered
    from sass.semantics import runtime
    events = []

    class State(evaluate.State):
        def read(self, file, index, count):
            events.append((file, index, count))
            return super().read(file, index, count)

    class Dependencies:
        def wait_slots(self, supplied):
            assert supplied == mask
            events.append("wait")

    semantics = lifter.lift(arch, assemble_instruction(arch, f"@P0 MOV R1, R2 @sched(req_bit_set={mask})"))
    statements = semantics.body.statements
    assert isinstance(statements[0], ordered.Special) and statements[0].operation == ordered.DEPENDENCIES
    assert statements[0].arguments == (bitvec.const(mask, 6),)
    assert sass.emulate.ordered.requirements(semantics.body) == (False, False)
    initial = State({("P", 0): 0, ("R", 1): 7, ("R", 2): 9})
    evaluate.apply(semantics, initial, runtime=runtime.Runtime(int(arch[2:]), {"async": Dependencies()}))
    assert events == ["wait", ("P", 0, 1)]
    assert initial.regs["R", 1] == 7


def test_isolated_dependency_wait_has_an_empty_scoreboard_without_a_provider():
    from sass.assemble import assemble_instruction
    semantics = lifter.lift("SM89", assemble_instruction("SM89", "MOV R1, R2 @sched(req_bit_set=63)"))
    state = evaluate.State({("R", 2): 9})
    evaluate.apply(semantics, state)
    assert state.read("R", 1, 1) == 9


@pytest.mark.parametrize("active", [False, True])
def test_deferred_wait_is_inside_predication_before_operand_reads(active):
    from sass.assemble import assemble_instruction
    from sass.semantics import runtime
    events = []

    class State(evaluate.State):
        def read(self, file, index, count):
            events.append(f"{file}{index}")
            return super().read(file, index, count)

        def load(self, space, address, width):
            events.append("load")
            return 42

    class Dependencies:
        def wait_slots(self, mask):
            assert mask == 8
            events.append("dependencies")

    class Coordinator:
        def pending(self): events.append("deferred")

    semantics = lifter.lift("SM89", assemble_instruction("SM89", "@P0 LDS R1, [R0] @sched(req_bit_set=8)"))
    state = State({("P", 0): int(active), ("R", 0): 16})
    provider = runtime.Runtime(89, {"async": Dependencies(), "coordination": Coordinator()})
    evaluate.apply(semantics, state, runtime=provider)
    assert events == (["dependencies", "P0", "deferred", "R0", "load"] if active else ["dependencies", "P0"])
    assert state.regs.get(("R", 1), 0) == (42 if active else 0)


def test_deferred_wait_retains_predication_and_reads_operands_after_resuming():
    from sass.assemble import assemble_instruction
    from sass.semantics import runtime

    class Ticket:
        complete = False
        def ready(self): return self.complete

    class Coordinator:
        def pending(self): return ticket
        def bind(self, supplied): assert supplied is ticket

    ticket = Ticket()
    semantics = lifter.lift("SM89", assemble_instruction("SM89", "@P0 LDS R1, [R0]"))
    frame = ordered.Frame(semantics.body, 16)
    environment = ordered_runtime.Runtime(provider=runtime.Runtime(89, {"coordination": Coordinator()}))
    state = evaluate.State({("P", 0): 1, ("R", 0): 16})
    state.store("shared", 16, 32, 7)
    state.store("shared", 32, 32, 9)
    assert frame.run(state, environment) == ordered.Status.SUSPENDED
    state.write("P", 0, 1, 0)
    state.write("R", 0, 1, 32)
    ticket.complete = True
    assert frame.run(state, environment) == ordered.Status.COMPLETE
    assert state.read("R", 1, 1) == 9


@pytest.mark.parametrize("text,arrival,completion", [
    ("BAR.SYNC 0", ordered.BLOCK_ARRIVE_IMPLICIT, ordered.BLOCK_WAIT_READY),
    ("BAR.SYNC 0, 32", ordered.BLOCK_ARRIVE_COUNTED, ordered.BLOCK_WAIT_READY),
    ("BAR.SYNC.DEFER_BLOCKING 0", ordered.BLOCK_ARRIVE_IMPLICIT, ordered.BLOCK_DEFER),
    ("BAR.RED.OR 0, !PT", ordered.BLOCK_ARRIVE_IMPLICIT, ordered.BLOCK_WAIT_READY),
    ("BAR.ARV 0, 32", ordered.BLOCK_ARRIVE_COUNTED, None),
])
def test_barrier_bodies_use_explicit_arrival_completion_and_release(text, arrival, completion):
    from sass.assemble import assemble_instruction
    semantics = lifter.lift("SM89", assemble_instruction("SM89", text))
    operations = [statement.operation for statement in ordered.linearize(semantics.body)
                  if isinstance(statement, ordered.Special)]
    assert operations == [ordered.BLOCK_WAIT_DEFERRED, arrival, *([completion] if completion else []),
                          ordered.BLOCK_RELEASE]
    ordered.prepare(semantics.body)


def test_wide_snapshot_handles_are_distinct_from_word_snapshots():
    builder = ordered.Builder()
    snapshot, = builder.special(ordered.CAPTURE64, bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(1 << 32, 64))
    builder.special(ordered.READ_LANE, snapshot, bitvec.const(0, 32))
    with pytest.raises(TypeError): ordered.validate(builder.block())


@pytest.mark.parametrize("width", (32, 64, 128))
def test_operand_group_snapshots_capture_all_words_and_release(width):
    from sass.semantics import primitive, runtime
    count = width // 32
    rows = [[(lane + 1) * 100 + word for word in range(count)] for lane in range(32)]
    provider = runtime.Runtime(89, {"warp": dict(lane=1, active=3, operand_groups={"Ra": rows})})
    environment = ordered_runtime.Explicit(provider)
    own = sum((1000 + word) << (32 * word) for word in range(count))
    snapshot, = environment.call(ordered.CAPTURE_WORDS[width].name,
                                 (primitive.OPERAND_GROUPS.index("Ra"), 1, 3, own))
    rows[0][0] = 0
    for lane, first in ((0, 100), (1, 1000)):
        assert [environment.call(ordered.READ_WORD.name, (snapshot, lane, word))[0]
                for word in range(count)] == list(range(first, first + count))
    with pytest.raises(runtime.UnsupportedSemantics, match="inactive"):
        environment.call(ordered.READ_WORD.name, (snapshot, 2, 0))
    with pytest.raises(runtime.UnsupportedSemantics, match="out of range"):
        environment.call(ordered.READ_WORD.name, (snapshot, 0, count))
    with pytest.raises(TypeError): environment.call(ordered.READ_LANE.name, (snapshot, 0))
    environment.call(ordered.RELEASE_WORDS.name, (snapshot,))
    assert not environment.objects


@pytest.mark.parametrize("width", (32, 64, 128))
def test_cooperative_operand_groups_keep_distinct_words_and_generations(width):
    environment = ordered_runtime.Cooperative()
    frames = [ordered.Frame(ordered.Block(), 16) for _ in range(2)]
    count = width // 32
    retained = []
    for generation in range(2):
        pending = []
        for lane in (0, 1):
            environment.bind(frames[lane], lane, site=16)
            value = sum((generation * 1000 + lane * 100 + word) << (32 * word) for word in range(count))
            pending.append(environment.call(ordered.CAPTURE_WORDS[width].name, (0, lane, 3, value)))
            if lane == 0: assert pending[-1].poll() is None
        retained.append(pending[0].poll()[0])
        other, = pending[1].poll()
        environment.call(ordered.RELEASE_WORDS.name, (other,))
    for generation, snapshot in enumerate(retained):
        for lane in range(2):
            assert [environment.call(ordered.READ_WORD.name, (snapshot, lane, word))[0] for word in range(count)] == [
                generation * 1000 + lane * 100 + word for word in range(count)]
        environment.call(ordered.RELEASE_WORDS.name, (snapshot,))
    assert not environment.objects and not environment.captures


def test_wide_snapshot_keeps_high_words_after_later_captures_and_releases():
    runtime = ordered_runtime.Cooperative()
    old = None
    for high in (1, 2):
        frame = ordered.Frame(ordered.Block(), 16)
        runtime.bind(frame, 0, site=16)
        pending = runtime.call(ordered.CAPTURE64.name, (0, 1, high << 32 | 7))
        handle, = pending.poll()
        assert runtime.call(ordered.READ_LANE64.name, (handle, 0)) == (high << 32 | 7,)
        if old is None: old = handle
        else:
            runtime.call(ordered.RELEASE_SNAPSHOT64.name, (handle,))
            assert runtime.call(ordered.READ_LANE64.name, (old, 0)) == (1 << 32 | 7,)
    runtime.call(ordered.RELEASE_SNAPSHOT64.name, (old,))
    assert not runtime.objects


def test_mufu_uses_captured_values_without_an_environment():
    from sass.semantics import primitive
    builder = ordered.Builder()
    captured = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(0, 32)))
    builder.assign_from_inputs([(("R", 1, 1), primitive.mufu("RCP", captured))])
    assert not any(isinstance(statement, ordered.Special) for statement in builder.statements)
    state = execute(builder.block(), evaluate.State({("R", 0): 0x3f800000}))
    assert state.regs == {("R", 0): 0, ("R", 1): 0x3f800000}


def test_unselected_mufu_approximation_does_not_fail():
    from sass.semantics import primitive
    expression = bitvec.ite(bitvec.reg("P", 0, 1, 1),
                            primitive.mufu("EX2", bitvec.const(0x3e800000, 32)),
                            bitvec.const(42, 32))
    builder = ordered.Builder()
    builder.assign_from_inputs([(("R", 0, 1), expression)])
    assert execute(builder.block()).regs["R", 0] == 42


def test_left_funnel_low_word_does_not_capture_unused_constant_memory():
    semantics = lifter.lift("SM89", bytes.fromhex("19a624662a03aba60563084e9ecef1b4"))
    class NoLoads(evaluate.State):
        def load(self, *args): raise AssertionError("unused constant-memory operand")
    state = NoLoads({("R", 102): 7, ("R", 5): 13})
    evaluate.apply(semantics, state)
    assert state.regs["R", 36] == 7 << 13
    assert not any(isinstance(statement, ordered.ReadMemory) for statement in ordered.linearize(semantics.body))


def test_assign_from_inputs_captures_at_its_position_and_preserves_alias_order():
    b = ordered.Builder()
    b.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(5, 32)))
    b.assign_from_inputs([(("R", 0, 1), bitvec.reg("R", 1, 1)),
                          (("R", 1, 1), bitvec.reg("R", 0, 1)),
                          (("R", 0, 1), bitvec.reg("R", 0, 1) + 1)])
    state = execute(b.block(), evaluate.State({("R", 0): 10, ("R", 1): 20}))
    assert state.regs == {("R", 0): 6, ("R", 1): 5}


def test_assign_from_inputs_keeps_memory_in_its_selected_branch():
    b = ordered.Builder()
    condition = bitvec.reg("P", 0, 1, 1)
    rhs = bitvec.ite(condition, bitvec.load("ram", bitvec.const(0, 64), 32), bitvec.const(42, 32))
    b.assign_from_inputs([(("R", 0, 1), rhs)])

    class NoLoads(evaluate.State):
        def load(self, *args): raise AssertionError("untaken memory access")
    assert execute(b.block(), NoLoads()).regs["R", 0] == 42


@pytest.mark.parametrize("enabled", [0, 1])
@pytest.mark.parametrize("alias", [False, True])
def test_complementary_outputs_keep_selected_reads_and_destination_order(enabled, alias):
    b = ordered.Builder()
    selected = bitvec.ite(bitvec.reg("P", 0, 1, 1),
                         bitvec.load("ram", bitvec.const(16, 64), 32), bitvec.const(42, 32))
    condition = selected == 7
    b.assign_from_inputs([(("P", 2, 1), ~condition), (("P", 2 if alias else 3, 1), condition)])

    class CountLoads(evaluate.State):
        loads = 0
        def load(self, *args):
            self.loads += 1
            return super().load(*args)

    state = CountLoads({("P", 0): enabled})
    state.store("ram", 16, 32, 7)
    execute(b.block(), state)
    assert state.loads == enabled
    assert state.regs["P", 2] == (enabled if alias else 1 - enabled)
    if not alias: assert state.regs["P", 3] == enabled


def test_complementary_output_read_fault_preserves_earlier_writes():
    b = ordered.Builder()
    b.emit(ordered.WriteRegister(("R", 8, 1), bitvec.const(9, 32)))
    condition = bitvec.load("ram", bitvec.const(16, 64), 32) == 7
    b.assign_from_inputs([(("P", 2, 1), ~condition), (("P", 3, 1), condition)])

    class Fault(evaluate.State):
        def load(self, *args): raise LookupError("unmapped")

    state = Fault({("P", 2): 1, ("P", 3): 1})
    frame = ordered.Frame(b.block(), 16)
    assert frame.run(state) == ordered.Status.FAULT
    assert isinstance(frame.error, LookupError)
    assert state.regs == {("R", 8): 9, ("P", 2): 1, ("P", 3): 1}


def test_complementary_inputs_preserve_an_explicit_cached_result():
    b = ordered.Builder()
    cached = b.move(bitvec.const(0, 1))
    condition = bitvec.reg("P", 0, 1, 1)
    negative, positive = b.capture_inputs((~condition, condition), {~condition: cached})
    b.emit(ordered.WriteRegister(("P", 2, 1), negative))
    b.emit(ordered.WriteRegister(("P", 3, 1), positive))
    state = execute(b.block(), evaluate.State({("P", 0): 0}))
    assert state.regs["P", 2] == state.regs["P", 3] == 0


@family.definition("ordered.test.load")
def defined_load(address):
    """Load a word through an argument-only address."""
    return bitvec.load("ram", address, 32)


@family.definition("ordered.test.choice")
def defined_choice(code, value):
    """Select a numerical formula using a decode-time constant code."""
    return bitvec.chosen(code, lambda number: value + number)


@family.definition("ordered.test.optional")
def defined_optional(code, value):
    """Use the input only for the selected numerical formula."""
    return bitvec.chosen(code, lambda number: value if number else bitvec.const(42, 32))


@family.definition("ordered.test.first")
def defined_first(a, b):
    """Return the first input independently of the second."""
    return a


def test_unused_input_is_distinguished_from_other_numerical_inputs():
    builder = ordered.Builder()
    builder.assign_from_inputs([(("R", 1, 1), defined_first(
        bitvec.reg("R", 0, 1), bitvec.load("ram", bitvec.const(0, 64), 32)))])
    assert sum(isinstance(statement, ordered.ReadRegister) for statement in builder.statements) == 1
    assert not any(isinstance(statement, ordered.ReadMemory) for statement in builder.statements)
    assert execute(builder.block(), evaluate.State({("R", 0): 17})).regs["R", 1] == 17


@pytest.mark.parametrize("code", [0, 1])
def test_decode_time_unused_definition_inputs_are_not_read(code):
    builder = ordered.Builder()
    value = defined_optional(bitvec.const(code, 1), bitvec.load("ram", bitvec.const(0, 64), 32))
    builder.assign_from_inputs([(("R", 0, 1), value)])
    assert sum(isinstance(statement, ordered.ReadMemory) for statement in builder.statements) == code
    state = evaluate.State()
    state.store("ram", 0, 32, 17)
    assert execute(builder.block(), state).regs["R", 0] == (17 if code else 42)


def test_purity_checks_preserve_decode_time_constant_arguments():
    code = bitvec.Expr("sel", (("code",), (((0,), bitvec.const(1, 8)), ((1,), bitvec.const(2, 8)))), 8)
    builder = ordered.Builder()
    value = bitvec.reg("R", 0, 1)
    builder.assign_from_inputs([(("R", 1, 1), defined_choice(code, value)),
                                (("R", 2, 1), defined_choice(code, value + 1))])
    ordered.validate(builder.block())
    assert sum(isinstance(statement, ordered.ReadRegister) for statement in builder.statements) == 1


def test_capture_keeps_hidden_state_reads_in_the_selected_branch():
    b = ordered.Builder()
    rhs = bitvec.ite(bitvec.reg("P", 0, 1, 1), defined_load(bitvec.const(0, 64)), bitvec.const(42, 32))
    b.assign_from_inputs([(("R", 0, 1), rhs)])

    class NoLoads(evaluate.State):
        def load(self, *args): raise AssertionError("untaken defined load")
    assert execute(b.block(), NoLoads()).regs["R", 0] == 42
    state = evaluate.State({("P", 0): 1})
    state.store("ram", 0, 32, 17)
    assert execute(b.block(), state).regs["R", 0] == 17


@pytest.mark.parametrize("taken", [0, 1])
def test_lifted_conditional_transfer_captures_its_predicate_before_writes(taken):
    body = ordered.Builder()
    predicate = body.read_register("P", 0)
    target, = body.if_else(predicate, lambda arm: (arm.read_memory("ram", bitvec.const(0x600, 64), 64),),
                          lambda arm: (bitvec.const(0x110, 64),))
    body.emit(ordered.WriteRegister(("P", 0, 1), bitvec.const(0, 1)))
    body.emit(ordered.If(predicate, ordered.Block((ordered.Transfer("call", target),))))
    semantics = lifter.Semantics("SM89", "test", "test", 0x100, "native", body=body.block())

    class SelectedLoads(evaluate.State):
        def load(self, *args):
            assert taken, "untaken call read its target"
            return super().load(*args)
    state = SelectedLoads({("P", 0): taken}, pc=0x100)
    state.store("ram", 0x600, 64, 0x400)
    evaluate.apply(semantics, state)
    assert state.read("P", 0, 1) == 0
    assert state.pc == (0x400 if taken else 0x110)


def test_branch_temporary_cannot_escape_without_a_result():
    b = ordered.Builder()
    x = b.read_register("R", 0)
    body = ordered.Block((ordered.If(bitvec.const(1, 1), b.block()),
                          ordered.WriteRegister(("R", 1, 1), x)))
    with pytest.raises(TypeError, match="undefined temporary"): ordered.validate(body)


def test_validation_reuse_does_not_cross_branch_scopes():
    builder = ordered.Builder()
    value = builder.read_register("R", 0) + 1
    builder.emit(ordered.WriteRegister(("R", 1, 1), value))
    other = ordered.Block((ordered.WriteRegister(("R", 2, 1), value),))
    body = ordered.Block((ordered.If(bitvec.const(1, 1), builder.block(), other),))
    with pytest.raises(TypeError, match="undefined temporary"): ordered.validate(body)


def test_validation_reuse_checks_each_expected_type():
    builder = ordered.Builder()
    value = builder.read_register("R", 0)
    builder.emit(ordered.WriteRegister(("R", 1, 1), value))
    builder.emit(ordered.WriteRegister(("P", 0, 1), value))
    with pytest.raises(TypeError, match="type mismatch"): ordered.validate(builder.block())


@pytest.mark.parametrize("taken", [0, 1])
@pytest.mark.parametrize("empty_yes", [False, True])
def test_conditional_effect_with_one_empty_arm(taken, empty_yes):
    effect = ordered.Block((ordered.WriteRegister(("R", 0, 1), bitvec.const(23, 32)),))
    body = ordered.Block((ordered.If(bitvec.const(taken, 1),
                                    ordered.Block() if empty_yes else effect,
                                    effect if empty_yes else ordered.Block()),))
    code = ordered.linearize(body)
    assert len(code) == 2 and isinstance(code[0], ordered.Branch)
    assert execute(body).read("R", 0, 1) == (23 if bool(taken) != empty_yes else 0)


def test_branch_is_lazy_and_has_results():
    state = evaluate.State({("P", 0): 1})
    state.store("ram", 16, 32, 23)
    assert execute(branch_body(True), state).regs["R", 0] == 23
    b = ordered.Builder()
    yes = ordered.Builder()
    yes.read_memory("ram", bitvec.const(16, 64), 32)
    b.emit(ordered.If(bitvec.const(0, 1), yes.block()))

    class NoLoads(evaluate.State):
        def load(self, *args): raise AssertionError("untaken branch loaded memory")
    execute(b.block(), NoLoads())


def test_multiple_results():
    b = ordered.Builder()
    x, p = b.special(PAIR, bitvec.const(5, 32))
    b.emit(ordered.WriteRegister(("R", 0, 1), x))
    b.emit(ordered.WriteRegister(("P", 0, 1), p))
    runtime = ordered_runtime.Runtime({PAIR.name: lambda x: (x + 1, 1)})
    assert execute(b.block(), runtime=runtime).regs == {("R", 0): 6, ("P", 0): 1}


@pytest.mark.parametrize("fail", [False, True])
def test_atomic_fault_preserves_writes_and_releases(fail):
    state = evaluate.State()
    runtime = ordered_runtime.Runtime({FAIL.name: lambda: (_ for _ in ()).throw(ValueError("fault"))})
    frame = ordered.Frame(atomic_body(fail), 16)
    status = frame.run(state, runtime)
    assert status == (ordered.Status.FAULT if fail else ordered.Status.COMPLETE)
    assert state.load("ram", 16, 32) == 1
    assert runtime.owner is None
    assert state.pc == (None if fail else 16)
    assert frame.run(state, runtime) == status


def test_suspension_does_not_repeat_effects_or_reads():
    state = evaluate.State({("R", 0): 7}, pc=0)
    ready, calls = [], []
    runtime = ordered_runtime.Runtime()

    def arrive(*args):
        calls.append(args)
        return runtime.retain(ordered.TICKET, args),

    def wait(ticket):
        calls.append(ticket)
        return ordered.Pending(lambda: (3,) if ready else None)

    runtime.handlers = {ordered.ARRIVE.name: arrive, ordered.WAIT.name: wait}
    frame = ordered.Frame(suspension_body(), 16)
    assert frame.run(state, runtime) == ordered.Status.SUSPENDED
    position = frame.position
    assert state.pc == 0 and state.regs["R", 0] == 8
    assert frame.run(state, runtime) == ordered.Status.SUSPENDED
    assert frame.position == position and len(calls) == 2
    state.regs["R", 0] = 50
    ready.append(True)
    assert frame.run(state, runtime) == ordered.Status.COMPLETE
    assert state.regs == {("R", 0): 50, ("R", 1): 10}
    assert state.pc == 16 and len(calls) == 2


def test_retained_snapshot_survives_destination_writes_and_suspension():
    states = [evaluate.State({("R", 0): i, ("R", 1): 10 + i}, pc=0) for i in range(2)]
    frames = [ordered.Frame(snapshot_body(), 16) for _ in states]
    runtime = ordered_runtime.Cooperative()
    runtime.bind(frames[0], 0)
    assert frames[0].run(states[0], runtime) == ordered.Status.SUSPENDED
    assert states[0].read("R", 1, 1) == 10
    runtime.bind(frames[1], 1)
    assert frames[1].run(states[1], runtime) == ordered.Status.SUSPENDED
    assert states[1].read("R", 1, 1) == 99
    for index in (0, 1):
        runtime.bind(frames[index], index)
        assert frames[index].run(states[index], runtime) == ordered.Status.COMPLETE
    assert [s.read("R", 2, 1) for s in states] == [11, 10]
    runtime.close()
    assert not runtime.objects


def test_barrier_reuse_retains_tickets_for_the_completed_generation():
    runtime = ordered_runtime.Cooperative()
    runtime.lane = 0
    first, = runtime.call(ordered.ARRIVE.name, (0, 2, 1))
    pending = runtime.call(ordered.WAIT.name, (first,))
    assert pending.poll() is None
    with pytest.raises(ValueError, match="repeats"): runtime.call(ordered.ARRIVE.name, (0, 2, 1))
    runtime.lane = 1
    runtime.call(ordered.ARRIVE.name, (0, 2, 1))
    assert pending.poll() == (2,)
    runtime.lane = 0
    second, = runtime.call(ordered.ARRIVE.name, (0, 2, 0))
    assert second != first
    assert runtime.call(ordered.WAIT.name, (second,)).poll() is None
    assert runtime.call(ordered.WAIT.name, (first,)).poll() == (2,)


def test_releasing_tickets_preserves_arrivals_and_existing_waits():
    runtime = ordered_runtime.Cooperative()
    runtime.lane = 0
    first, = runtime.call(ordered.ARRIVE.name, (0, 2, 1))
    pending = runtime.call(ordered.WAIT.name, (first,))
    runtime.call(ordered.RELEASE_TICKET.name, (first,))
    assert pending.poll() is None
    for spec in (ordered.WAIT, ordered.RELEASE_TICKET):
        with pytest.raises(TypeError, match="invalid opaque handle"): runtime.call(spec.name, (first,))
    snapshot = runtime.retain(ordered.SNAPSHOT, {0: 7})
    with pytest.raises(TypeError, match="invalid opaque handle"): runtime.call(ordered.RELEASE_TICKET.name, (snapshot,))
    assert runtime.object(snapshot, ordered.SNAPSHOT) == {0: 7}
    runtime.lane = 1
    last, = runtime.call(ordered.ARRIVE.name, (0, 2, 0))
    runtime.call(ordered.RELEASE_TICKET.name, (last,))
    assert pending.poll() == (1,)
    assert not runtime.barriers
    runtime.call(ordered.RELEASE_SNAPSHOT.name, (snapshot,))
    assert not runtime.objects


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_cooperative_snapshot_schedules(schedule):
    runtime = ordered_runtime.Cooperative()
    states = [evaluate.State({("R", 0): lane, ("R", 1): 10 + lane}) for lane in range(2)]
    frames = [ordered.Frame(snapshot_body(), 16) for _ in states]
    rng = random.Random(0x5a55)
    for _ in range(4):
        order = [0, 1]
        match schedule:
            case "reverse": order.reverse()
            case "seeded": rng.shuffle(order)
        for lane in order:
            runtime.bind(frames[lane], lane)
            assert frames[lane].run(states[lane], runtime) != ordered.Status.FAULT
    assert all(frame.status == ordered.Status.COMPLETE for frame in frames)
    assert [state.read("R", 2, 1) for state in states] == [11, 10]


def test_capture_reuse_does_not_replace_a_retained_snapshot():
    runtime = ordered_runtime.Cooperative()
    runtime.site = 0
    runtime.lane = 0
    first = runtime.capture(0, 3, 10)
    runtime.lane = 1
    first_other = runtime.capture(1, 3, 11)
    first_handle, = first_other.poll()
    runtime.lane = 0
    second = runtime.capture(0, 3, 20)
    assert second.poll() is None
    runtime.lane = 1
    second_other = runtime.capture(1, 3, 21)
    second_handle, = second_other.poll()
    assert second_handle != first_handle
    own_handle, = first.poll()
    assert own_handle != first_handle
    assert runtime.object(own_handle, ordered.SNAPSHOT) is runtime.object(first_handle, ordered.SNAPSHOT)
    assert runtime.read_lane(first_handle, 1) == (11,)
    assert runtime.read_lane(second_handle, 1) == (21,)


def test_ghidra_capture_sites_use_distinct_pcode_positions():
    from types import SimpleNamespace
    from sass.ghidra_bridge.ordered import Execution
    runtime = ordered_runtime.Cooperative()

    def capture(lane, position, value):
        thread = SimpleNamespace(getFrame=lambda: SimpleNamespace(index=lambda: position))
        execution = Execution(thread, runtime)
        runtime.bind(execution, lane, site=16)
        return runtime.call(ordered.CAPTURE.name, (lane, 3, value))

    first = capture(0, 3, 10)
    second = capture(1, 7, 21)
    assert first.poll() is None and second.poll() is None
    first_other = capture(1, 3, 11)
    second_other = capture(0, 7, 20)
    for pending, values in ((first, (10, 11)), (first_other, (10, 11)),
                            (second, (20, 21)), (second_other, (20, 21))):
        handle, = pending.poll()
        assert tuple(runtime.read_lane(handle, lane)[0] for lane in range(2)) == values
        runtime.call(ordered.RELEASE_SNAPSHOT.name, (handle,))
    assert not runtime.objects and not runtime.captures


def test_atomic_owner_blocks_other_frames():
    runtime = ordered_runtime.Cooperative()
    first, second = ordered.Frame(ordered.Block(), 16), ordered.Frame(ordered.Block(), 16)
    runtime.bind(first, 0)
    runtime.acquire(first)
    with pytest.raises(RuntimeError, match="owned by another"): runtime.bind(second, 1)
    runtime.release(first)
    runtime.bind(second, 1)


def test_ghidra_pending_fault_is_terminal():
    from sass.ghidra_bridge.ordered import Execution
    runtime = ordered_runtime.Runtime()
    execution = Execution(None, runtime)
    error = ValueError("pending operation failed")

    def poll(): raise error
    execution.pending = ordered.Pending(poll), "sass_ordered_barrier_wait"
    assert execution.step() == ordered.Status.FAULT
    assert execution.error is error and execution.pending is None
    assert execution.step() == ordered.Status.FAULT


@pytest.mark.parametrize("expression", [
    bitvec.Expr("add", (bitvec.const(0, 32),), 32),
    bitvec.Expr("add", (bitvec.const(0, 32), bitvec.const(0, 1)), 32),
    bitvec.Expr("eq", (bitvec.const(0, 32), bitvec.const(0, 32)), 32),
    bitvec.Expr("add", (bitvec.const(0, 32), bitvec.const(0, 32)), 64),
])
def test_invalid_pure_expression_types(expression):
    with pytest.raises(TypeError):
        ordered.validate(ordered.Block((ordered.Move(ordered.temp(0, expression.w), expression),)))


@pytest.mark.parametrize("body,reason", [
    (ordered.Block((ordered.Move(ordered.temp(0, 32), bitvec.reg("R", 0, 1)),)), "state access"),
    (ordered.Block((ordered.Move(ordered.temp(1, 32), ordered.temp(0, 32)),)), "undefined temporary"),
    (ordered.Block((ordered.Move(ordered.temp(0, 32), bitvec.const(0, 32)),
                    ordered.Move(ordered.temp(0, 32), bitvec.const(0, 32)))), "defined twice"),
    (ordered.Block((ordered.Atomic(ordered.Block((ordered.Atomic(ordered.Block()),))),)), "nested atomic"),
    (ordered.Block((ordered.Atomic(ordered.Block((ordered.Special(ordered.WAIT_READY,
                    (ordered.Handle(0, ordered.TICKET),)),))),)), "suspension"),
    (ordered.Block((ordered.Atomic(ordered.Block((ordered.Transfer("goto", bitvec.const(0, 64)),))),)), "control transfer"),
    (ordered.Block((ordered.Transfer("goto", bitvec.const(0, 64)), ordered.Atomic(ordered.Block()))), "after terminal"),
    (ordered.Block((ordered.WriteRegister(("R", 0, 1), bitvec.const(0, 1)),)), "type mismatch"),
    (ordered.Block((ordered.WriteMemory("ram", bitvec.const(0, 32), bitvec.const(0, 32)),)), "type mismatch"),
    (ordered.Block((ordered.ReadMemory(ordered.temp(0, 0), "ram", bitvec.const(0, 64)),)), "positive multiple"),
    (ordered.Block((ordered.Special(ordered.WAIT, (ordered.temp(0, 64),), (ordered.temp(1, 32),)),)), "undefined temporary"),
])
def test_validation(body, reason):
    with pytest.raises(TypeError, match=reason): ordered.validate(body)
