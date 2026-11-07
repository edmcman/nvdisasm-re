import json
import pytest
import sass.semantics.ordered
from sass.formal import k
from sass.formal.k import kast, ordered, tools
from tests.harness import ordered_gate
from sass.semantics import bitvec, evaluate


@pytest.mark.parametrize("body", [ordered_gate.register_body(), ordered_gate.branch_body(True),
                                 ordered_gate.atomic_body(), ordered_gate.suspension_body(), ordered_gate.snapshot_body(),
                                 ordered_gate.block_barrier_body(), ordered_gate.block_barrier_body(deferred=True),
                                 ordered_gate.block_barrier_deferred_wait_body(), ordered_gate.warp_sync_body()])
def test_ordered_export(body):
    exported = ordered.export(body)
    assert json.loads(json.dumps(exported.manifest()))["format"] == 2
    assert "orderedInstruction" in exported.program
    assert "orderedRead" in exported.program or "orderedLoad" in exported.program
    kast.encode(f"run({exported.program}, .Map, .Map, .Map, .Map, .Map, 1)")
    assert ordered.export(body) == exported


def test_temporary_ids_are_stable():
    encoder = ordered.Encoder()
    temporary = sass.semantics.ordered.temp(0, 32)
    first = encoder.expression(temporary)
    second = encoder.expression(temporary + 1)
    assert encoder.expression(temporary) == first
    assert second.count(first) == 1
    assert len({ident for ident, _ in encoder.pure.nodes.values()}) == len(encoder.pure.nodes)


def parameter_body():
    builder = sass.semantics.ordered.Builder()
    before = builder.read_register("R", 0)
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 0, 1), before + 1))
    condition = builder.read_register("P", 0)
    result, = builder.if_else(condition, lambda arm: (bitvec.param("input", 32),),
                              lambda arm: (bitvec.const(9, 32),))
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 1, 1), result))
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    return builder.block()


def test_ordered_parameters_export_and_reject_conflicting_widths():
    from sass.semantics import lifter
    body = parameter_body()
    semantics = lifter.Semantics("SM89", "parameters", "parameters", 0x100000, "native", body=body)
    assert k.export_instruction(semantics).program == ordered.export(body).program
    kast.encode(k.input_term(ordered.export(body), params={"input": 17}))
    builder = sass.semantics.ordered.Builder()
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 0, 1), bitvec.param("input", 32)))
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 1, 2), bitvec.param("input", 64)))
    with pytest.raises(k.ExportError, match="used at widths"):
        ordered.export(builder.block())


@pytest.mark.k
@pytest.mark.parametrize("enabled,parameters,status,result", [
    (0, {}, "halted", 9),
    (1, {}, "unbound parameter", None),
    (1, {"input": (1 << 40) | 17}, "halted", 17),
])
def test_ordered_parameter_execution_retains_earlier_writes(enabled, parameters, status, result):
    initial = evaluate.State({("R", 0): 7, ("P", 0): enabled})
    cells = tools.execute(k.input_term(ordered.export(parameter_body()), initial, params=parameters))
    assert cells["status"] == status and cells["regs"]["R", 0] == 8
    if result is None: assert ("R", 1) not in cells["regs"]
    else: assert cells["regs"]["R", 1] == result


def test_typed_runtime_transcript():
    exported = ordered.export(ordered_gate.pair_body())
    reply = dict(step=0, pc=0x100000, id=0, name=ordered_gate.PAIR.name, arguments=[5], values=[6, 1])
    term = k.input_term(exported, responses=[reply])
    assert "orderedResponse" in term
    kast.encode(term)
    with pytest.raises(k.ExportError, match="outside its declared types"):
        k.input_term(exported, responses=[dict(reply, values=[6, 2])])
    with pytest.raises(k.ExportError, match="outside its declared types"):
        k.input_term(exported, responses=[dict(reply, arguments=[1 << 32])])


def test_block_barrier_export_retains_distinct_count_contracts():
    implicit = ordered.export(ordered_gate.block_barrier_body())
    counted = ordered.export(ordered_gate.block_barrier_body(counted=32))
    assert implicit.operations[0] == sass.semantics.ordered.BLOCK_ARRIVE_IMPLICIT
    assert counted.operations[0] == sass.semantics.ordered.BLOCK_ARRIVE_COUNTED
    assert implicit.operations[0].results == (sass.semantics.ordered.BLOCK_TICKET,)
    assert "barrier_arrive" not in [operation.name for operation in implicit.operations]
    reply = dict(step=0, pc=0x100000, id=2, name=implicit.operations[0].name,
                 arguments=[0, 1, 3], values=[17])
    kast.encode(k.input_term(implicit, responses=[reply]))
    with pytest.raises(k.ExportError, match="outside its declared types"):
        k.input_term(implicit, responses=[dict(reply, values=[1 << 64])])


@pytest.mark.k
@pytest.mark.parametrize("protocol", ["implicit", "counted", "deferred"])
def test_block_barrier_k_contracts_use_the_explicit_environment(protocol):
    from sass.emulate import machine
    from sass.semantics import ordered_runtime, runtime

    builder = sass.semantics.ordered.Builder()
    before = builder.read_register("R", 0)
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 0, 1), before + 1))
    arguments = (bitvec.const(0, 32), bitvec.const(1, 1), bitvec.const(3, 32))
    counted = protocol == "counted"
    if counted: arguments = (arguments[0], bitvec.const(32, 32), *arguments[1:])
    ticket, = builder.special(sass.semantics.ordered.BLOCK_ARRIVE_COUNTED if counted else
                             sass.semantics.ordered.BLOCK_ARRIVE_IMPLICIT, *arguments)
    builder.special(sass.semantics.ordered.BLOCK_DEFER if protocol == "deferred" else
                    sass.semantics.ordered.BLOCK_WAIT_READY, ticket)
    builder.special(sass.semantics.ordered.BLOCK_RELEASE, ticket)
    if protocol == "deferred": builder.special(sass.semantics.ordered.BLOCK_WAIT_DEFERRED)
    result, = builder.special(sass.semantics.ordered.BLOCK_RESULT)
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 1, 1), result))
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    frame = sass.semantics.ordered.Frame(builder.block(), 0)
    replies = []

    class Transcript(ordered_runtime.Runtime):
        def call(self, name, arguments):
            values = super().call(name, arguments)
            if isinstance(values, sass.semantics.ordered.Pending): values = values.poll()
            assert values is not None
            replies.append(dict(step=0, pc=0x100000, id=frame.position, name=name,
                                arguments=list(arguments), values=list(values)))
            return values

    library = machine.Library('#include "sass/abi.hpp"\n', "coordination").lib
    count = 32 if counted else 1
    coordinator = library.sass_coordination_new(count)
    environment = Transcript(provider=runtime.Runtime(89, {
        "coordination": machine.Coordination(library, coordinator, 0)}))
    try:
        for index in range(1, count):
            machine.Coordination(library, coordinator, index).arrive(0, 1, 3, count)
        initial = evaluate.State({("R", 0): 7})
        state = initial.copy()
        assert frame.run(state, environment) == sass.semantics.ordered.Status.COMPLETE
        assert not environment.objects
        cells = tools.execute(k.input_term(ordered.export(builder.block()), initial, responses=replies))
        assert cells["status"] == "halted"
        assert cells["regs"] == state.regs == {("R", 0): 8, ("R", 1): count}
    finally:
        environment.close()
        library.sass_coordination_free(coordinator)


def test_mufu_exports_a_pure_numerical_contract():
    from sass.semantics import primitive
    builder = sass.semantics.ordered.Builder()
    builder.assign_from_inputs([(("R", 0, 1), primitive.mufu("RCP", bitvec.reg("R", 1, 1)))])
    exported = ordered.export(builder.block())
    assert exported.operations == ()
    assert exported.manifest()["contracts"] == [dict(name="sass_prim_mufu_rcp", arguments=(32,),
                                                     result=32, kind="pure", context="hardware approximation")]
    assert '"mufu"' in exported.program and "orderedSpecial" not in exported.program
    kast.encode(exported.program)


@pytest.mark.k
def test_pure_and_ordered_responses_have_distinct_ids():
    from sass.semantics import lifter, primitive
    builder = sass.semantics.ordered.Builder()
    operation = sass.semantics.ordered.BARRIER_ACKS["all"]
    builder.special(operation)
    builder.assign_from_inputs([(("R", 0, 1), primitive.mufu("RCP", bitvec.const(0x3f800000, 32)))])
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    semantics = lifter.Semantics("SM89", "responses", "responses", 0x100000, "primitive", body=builder.block())
    exported = k.export_instruction(semantics)
    sites = exported.instructions[0]["runtime_sites"]
    pure = next(site for site in sites if site["kind"] == "pure")
    assert sites[0]["id"] == 0 and pure["id"] < 0
    environment = '("acknowledged" |-> 1)'
    replies = [
        dict(step=0, pc=0x100000, id=0, name=operation.name, arguments=[], values=[],
             events='ListItem("BAR")', environment=environment),
        dict(step=0, pc=0x100000, id=pure["id"], name=pure["name"], arguments=[0x3f800000],
             width=32, value=0x3f800000, before=environment, environment=environment),
    ]
    cells = tools.execute(k.input_term(exported, responses=replies))
    assert cells["status"] == "halted" and cells["regs"]["R", 0] == 0x3f800000
    assert cells["events"] == ["BAR"] and cells["environment"] == {"acknowledged": 1}


@pytest.mark.k
@pytest.mark.parametrize("text,location,expected", [
    ("VOTE.ANY R1, P1, P1", ("R", 1), 3),
    ("MATCH.ANY.U64 R1, R0", ("R", 1), 3),
    ("REDUX.SUM UR1, R1", ("UR", 1), 8),
    ("DEPBAR.LE SB0, 0x1, {1}", ("R", 1), 1),
    ("DEPBAR {1,4}", ("R", 1), 1),
    ("@!PT MOV R1, R2 @sched(req_bit_set=8)", ("R", 1), 1),
    ("BAR.SYNC 0", ("R", 1), 1),
    ("BAR.SYNC.DEFER_BLOCKING 0", ("R", 1), 1),
    ("BAR.RED.OR 0, !PT", ("R", 1), 1),
    ("B2R.RESULT R1, P1", ("R", 1), 37),
    ("B2R.BAR R1, 3", ("R", 1), 0xa0001001),
    ("WARPSYNC 0xffffffff", ("R", 1), 1),
    ("WARPSYNC R1", ("R", 1), 1),
    ("WARPSYNC UR1", ("R", 1), 1),
    ("WARPSYNC.EXCLUSIVE !PT, R1", ("R", 1), 1),
    ("BSSY B0, 0x100100", ("R", 1), 1),
    ("BSSY B0, 0x100.REL", ("R", 1), 1),
    ("BSYNC B0", ("R", 1), 1),
    ("BSYNC !PT, B0", ("R", 1), 1),
    ("BMOV.32 R1, B0", ("R", 1), 37),
    ("BMOV.32.CLEAR R1, B0", ("R", 1), 37),
    ("BMOV.32.CLEAR B2, B3", ("R", 1), 1),
    ("BMOV.32 B2, MEXITED", ("R", 1), 1),
    ("BMOV.64 ATEXIT_PC, R0", ("R", 1), 1),
    ("BMOV.32.PQUAD MACTIVE, R1", ("R", 1), 1),
    ("BMOV.32 B0, B3", ("R", 1), 1),
    ("BREAK B0", ("R", 1), 1),
    ("BREAK !PT, B0", ("R", 1), 1),
])
def test_production_runtime_k_contracts_use_captured_inputs(text, location, expected):
    from sass.assemble import assemble_instruction
    from sass.semantics import lifter, ordered_runtime, runtime
    semantics = lifter.lift("SM89", assemble_instruction("SM89", text), 0x100000)
    body = sass.semantics.ordered.Block((*semantics.body.statements,
                                        sass.semantics.ordered.Transfer("return", bitvec.const(0, 64))))
    initial = evaluate.State({("R", 0): 7, ("R", 1): 1, ("P", 1): 1})
    context = dict(async_record=True, synchronization={"bar": True, "warpsync": True, "bssy": True, "bsync": True,
                                                       "break": True, "bmov": {"0": 37}}, barrier_result=37,
                   barrier_states=[0, 0, 0, 0xa0001001] + [0] * 12,
                   warp=dict(lane=0, active=3, values=[7, (1 << 32) | 7] + [0] * 30, predicates=[0, 1] + [0] * 30))
    frame = sass.semantics.ordered.Frame(body, 0)
    replies = []

    class Transcript(ordered_runtime.Explicit):
        def call(self, name, arguments):
            before = len(self.provider.events)
            values = super().call(name, arguments)
            events = [json.dumps(event, sort_keys=True) for event in self.provider.events[before:]]
            replies.append(dict(step=0, pc=0x100000, id=frame.position, name=name,
                                arguments=list(arguments), values=list(values),
                                events=k.klist(k.string(event) for event in events)))
            return values

    environment = Transcript(runtime.Runtime(89, context))
    state = initial.copy()
    assert frame.run(state, environment) == sass.semantics.ordered.Status.COMPLETE
    assert state.regs[location] == expected and not environment.objects
    cells = tools.execute(k.input_term(ordered.export(body), initial, responses=replies))
    assert cells["status"] == "halted" and cells["regs"][location] == expected
    assert cells["events"] == [json.dumps(event, sort_keys=True) for event in environment.provider.events]


@pytest.mark.k
@pytest.mark.parametrize("table", [0x96, 0xe8, 0x17, 0xca])
def test_production_lop3_definition_preserves_aliased_inputs(table):
    from sass.assemble import assemble_instruction
    from sass.semantics import lifter
    from tests.refs import integer
    values = (0xf0f0f0f0, 0xcccccccc, 0xaaaaaaaa)
    semantics = lifter.lift("SM89", assemble_instruction(
        "SM89", f"LOP3.LUT P2, R2, R2, R4, R6, {table}, PT"), 0x100000)
    body = sass.semantics.ordered.Block((*semantics.body.statements,
                                        sass.semantics.ordered.Transfer("return", bitvec.const(0, 64))))
    initial = evaluate.State(dict(zip((("R", 2), ("R", 4), ("R", 6)), values)))
    exported = ordered.export(body)
    assert not exported.operations
    cells = tools.execute(k.input_term(exported, initial))
    expected, predicate = integer.lop3(*values, table, 1, "POR")
    assert cells["status"] == "halted"
    assert cells["regs"][("R", 2)] == expected and cells["regs"][("P", 2)] == predicate


@pytest.mark.k
@pytest.mark.parametrize("width", (32, 64, 128))
def test_operand_group_k_contracts_keep_all_words(width):
    from sass.semantics import ordered_runtime, runtime
    builder = sass.semantics.ordered.Builder()
    count = width // 32
    source = bitvec.const(sum((10 + word) << (32 * word) for word in range(count)), width)
    snapshot, = builder.special(sass.semantics.ordered.CAPTURE_WORDS[width],
                                bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(3, 32), source)
    for lane in range(2):
        value, = builder.special(sass.semantics.ordered.READ_WORD, snapshot,
                                 bitvec.const(lane, 32), bitvec.const(count - 1, 32))
        builder.emit(sass.semantics.ordered.WriteRegister(("R", lane, 1), value))
    builder.special(sass.semantics.ordered.RELEASE_WORDS, snapshot)
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    frame = sass.semantics.ordered.Frame(builder.block(), 0)
    replies = []

    class Transcript(ordered_runtime.Explicit):
        def call(self, name, arguments):
            values = super().call(name, arguments)
            replies.append(dict(step=0, pc=0x100000, id=frame.position, name=name,
                                arguments=list(arguments), values=list(values)))
            return values

    provider = runtime.Runtime(89, {"warp": dict(lane=1, active=3,
                                                operand_groups={"Ra": [list(range(1, count + 1)) for _ in range(32)]})})
    environment = Transcript(provider)
    state = evaluate.State()
    assert frame.run(state, environment) == sass.semantics.ordered.Status.COMPLETE
    assert not environment.objects
    cells = tools.execute(k.input_term(ordered.export(builder.block()), responses=replies))
    assert cells["status"] == "halted"
    assert [cells["regs"]["R", lane] for lane in range(2)] == [count, count + 9]


@pytest.mark.k
def test_ticket_release_k_contract_preserves_earlier_writes_and_wait_results():
    from sass.semantics import ordered_runtime
    builder = sass.semantics.ordered.Builder()
    before = builder.read_register("R", 0)
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 0, 1), before + 1))
    ticket, = builder.special(sass.semantics.ordered.ARRIVE, bitvec.const(0, 32),
                              bitvec.const(1, 32), bitvec.const(1, 1))
    result, = builder.special(sass.semantics.ordered.WAIT, ticket)
    builder.special(sass.semantics.ordered.RELEASE_TICKET, ticket)
    builder.emit(sass.semantics.ordered.WriteRegister(("R", 1, 1), before + result))
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    frame = sass.semantics.ordered.Frame(builder.block(), 0)
    replies = []

    class Transcript(ordered_runtime.Cooperative):
        def call(self, name, arguments):
            values = super().call(name, arguments)
            if isinstance(values, sass.semantics.ordered.Pending): values = values.poll()
            assert values is not None
            replies.append(dict(step=0, pc=0x100000, id=frame.position, name=name,
                                arguments=list(arguments), values=list(values)))
            return values

    environment = Transcript()
    environment.bind(frame, 0)
    initial = evaluate.State({("R", 0): 7})
    state = initial.copy()
    assert frame.run(state, environment) == sass.semantics.ordered.Status.COMPLETE
    assert not environment.objects and not environment.barriers
    cells = tools.execute(k.input_term(ordered.export(builder.block()), initial, responses=replies))
    assert cells["status"] == "halted"
    assert cells["regs"] == state.regs == {("R", 0): 8, ("R", 1): 8}


@pytest.mark.k
@pytest.mark.parametrize("events,after,status", [
    (".List", ".Map", "halted"),
    ('ListItem("unexpected")', ".Map", "pure response mismatch"),
    (".List", '("unexpected" |-> 1)', "pure response mismatch"),
])
def test_mufu_contract_cannot_change_the_environment(events, after, status):
    from sass.semantics import primitive
    builder = sass.semantics.ordered.Builder()
    builder.assign_from_inputs([(("R", 0, 1), primitive.mufu("RCP", bitvec.reg("R", 1, 1)))])
    builder.emit(sass.semantics.ordered.Transfer("return", bitvec.const(0, 64)))
    exported = ordered.export(builder.block())
    encoder = ordered.Encoder()
    encoder.instruction(builder.block(), 0x100000)
    ident = next(ident for node, (ident, _) in encoder.pure.nodes.items() if node.op == "mufu")
    reply = dict(step=0, pc=0x100000, id=ident, name="sass_prim_mufu_rcp", arguments=[0x3f800000],
                 width=32, value=0x3f800000, events=events, environment=after)
    cells = tools.execute(k.input_term(exported, evaluate.State({("R", 1): 0x3f800000}),
                                       responses=[reply]))
    assert cells["status"] == status


@pytest.mark.k
def test_ordered_k_multiple_results_and_fault_after_writes():
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")
    body = ordered_gate.pair_body()
    body = sass.semantics.ordered.Block((*body.statements, sass.semantics.ordered.Transfer("return", bitvec.const(0, 64))))
    exported = ordered.export(body)
    reply = dict(step=0, pc=0x100000, id=0, name=ordered_gate.PAIR.name, arguments=[5], values=[6, 1])
    cells = tools.execute(k.input_term(exported, responses=[reply]))
    assert cells["status"] == "halted" and cells["regs"]["R", 0] == 6 and cells["regs"]["P", 0] == 1
    body = ordered_gate.atomic_body(True)
    state = evaluate.State()
    state.store("ram", 16, 32, 23)
    cells = tools.execute(k.input_term(ordered.export(body), state))
    assert cells["status"] == "runtime response missing"
    assert cells["memory"]["ram", 16] == 24


@pytest.mark.k
@pytest.mark.parametrize("body,initial,registers,memory", [
    (ordered_gate.register_body(), {("R", 0): 7}, {("R", 0): 8, ("R", 1): 8}, 23),
    (ordered_gate.branch_body(True), {("P", 0): 1}, {("R", 0): 23}, 23),
    (ordered_gate.atomic_body(), {}, {}, 24),
])
def test_ordered_k_execution(body, initial, registers, memory):
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")
    body = sass.semantics.ordered.Block((*body.statements, sass.semantics.ordered.Transfer("return", bitvec.const(0, 64))))
    state = evaluate.State(initial)
    state.store("ram", 16, 32, 23)
    term = k.input_term(ordered.export(body), state, limit=1)
    cells = tools.execute(term)
    assert cells["status"] == "halted"
    for location, value in registers.items(): assert cells["regs"][location] == value
    actual = sum(cells["memory"].get(("ram", 16 + byte), 0) << (8 * byte) for byte in range(4))
    assert actual == memory
