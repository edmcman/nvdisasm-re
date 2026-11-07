"""Explicit observations at the K/runtime boundary, not GPU verification evidence."""
import json
import pytest

from sass.formal import k
from sass.formal.k import tools
from sass.semantics import bitvec, evaluate, lifter, ordered, ordered_runtime, primitive, runtime

pytestmark = pytest.mark.k


def test_runtime_observation_contracts():
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")
    context = dict(
        warp=dict(lane=2, active=15, waiting=16, values=list(range(32)), predicates=[j % 2 for j in range(32)]),
        address_spaces={"global": [[100, 200]], "shared": [[300, 400]]},
        synchronization={"bar": True, "bmov": {"0": 17}},
    )
    builder, outputs = ordered.Builder(), []
    constant = lambda value: bitvec.const(value, 32)
    for contribution in (0, 1):
        builder.special(ordered.BARRIER_ACKS["packed_reduce"], constant(0), constant(0),
                        constant(32), bitvec.const(contribution, 1))
    for spec in (ordered.LANE, ordered.ACTIVE, ordered.WAITING):
        value, = builder.special(spec)
        outputs.append(value)
    snapshot, = builder.special(ordered.CAPTURE, constant(2), constant(15), constant(3))
    value, = builder.special(ordered.READ_LANE, snapshot, constant(1))
    outputs.append(value)
    predicates, = builder.special(ordered.CAPTURE_PREDICATE, constant(2), constant(15), bitvec.const(1, 1))
    value, = builder.special(ordered.BALLOT, predicates)
    outputs.append(value)
    wide, = builder.special(ordered.CAPTURE64, constant(2), constant(15), bitvec.const(7, 64))
    value, = builder.special(ordered.MATCH64, wide, constant(2))
    outputs.append(value)
    for spec in ordered.REDUCTIONS.values():
        value, = builder.special(spec, snapshot)
        outputs.append(value)
    value, = builder.special(ordered.ADDRESS_SPACE, bitvec.const(123, 64))
    outputs.append(value)
    outputs.extend((primitive.mufu("SIN", constant(0x3f800000)),
                    primitive.mufu("COS", constant(0x3e800000))))
    value, = builder.special(ordered.CONVERGENCE_READ, constant(0))
    outputs.append(value)
    for index, value in enumerate(outputs): builder.emit(ordered.WriteRegister(("R", index, 1), value))
    for spec, handle in ((ordered.RELEASE_SNAPSHOT, snapshot), (ordered.RELEASE_SNAPSHOT, predicates),
                         (ordered.RELEASE_SNAPSHOT64, wide)):
        builder.special(spec, handle)
    builder.emit(ordered.Transfer("return", bitvec.const(0, 64)))
    instruction = lifter.Semantics("SM89", "contracts", "runtime observations", 0x100000, "primitive",
                                   body=builder.block())
    exported = k.export_instruction(instruction)
    encoder = k.Encoder()
    encoder.instruction(instruction)
    environment = f'("runtime_context" |-> {k.string(json.dumps(context, sort_keys=True))})'
    provider = runtime.Runtime(89, context=context)
    frame, responses = ordered.Frame(builder.block(), 0), []

    class Transcript(ordered_runtime.Explicit):
        def call(self, name, arguments):
            start = len(self.provider.events)
            values = super().call(name, arguments)
            events = k.klist(k.string(json.dumps(event, sort_keys=True)) for event in self.provider.events[start:])
            responses.append(dict(step=0, pc=0x100000, id=frame.position, name=name,
                                  arguments=list(arguments), values=list(values), events=events))
            return values

    state, explicit = evaluate.State(), Transcript(provider)
    assert frame.run(state, explicit) == ordered.Status.COMPLETE
    assert not explicit.objects
    assert [state.regs["R", index] for index in range(6)] == [2, 15, 16, 1, 14, 4]
    assert state.regs["R", len(outputs) - 1] == 17
    for expression, (ident, _) in encoder.nodes.items():
        if expression.op != "mufu": continue
        name = bitvec.PRIMITIVE_OP + "mufu_" + expression.args[0]
        args = [bitvec.value(value) for value in expression.args[1:]]
        value = provider.call(name, args, 4)
        responses.append(dict(step=0, pc=0x100000, id=ident, name=name, arguments=args, width=32, value=value))
    assert len({response["id"] for response in responses}) == len(responses)
    result = tools.execute(k.input_term(exported, responses=responses, environment=environment))
    assert result["status"] == "halted"
    assert result["regs"] == state.regs
    assert [json.loads(event) for event in result["events"]] == provider.events
    assert result["environment"] == {"runtime_context": json.dumps(context, sort_keys=True)}
