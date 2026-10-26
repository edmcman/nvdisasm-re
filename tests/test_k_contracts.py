"""Explicit observations at the K/runtime boundary, not GPU verification evidence."""
import json
import pytest

from sass.formal import k
from sass.formal.k import tools
from sass.semantics import bitvec, evaluate, lifter, primitive, runtime
from sass.semantics.family import Effect, Flow

pytestmark = pytest.mark.k


def test_runtime_observation_contracts():
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")
    context = dict(
        warp=dict(lane=2, active=15, waiting=16, values=list(range(32)), predicates=[j % 2 for j in range(32)]),
        address_spaces={"global": [[100, 200]], "shared": [[300, 400]]},
        synchronization={"bar": True, "bmov": {"0": 17}},
    )
    provider = runtime.Runtime(89, context=context)
    expressions = [
        primitive.lane(), primitive.active(), primitive.waiting(),
        primitive.read_lane(bitvec.const(3, 32), bitvec.const(1, 32)),
        primitive.ballot(bitvec.const(1, 1)), primitive.match(bitvec.const(7, 64)),
        *[primitive.redux(op, bitvec.const(3, 32)) for op in primitive.REDUCTIONS],
        primitive.address_space(bitvec.const(123, 64)),
        primitive.mufu("SIN", bitvec.const(0x3f800000, 32)),
        primitive.mufu("COS", bitvec.const(0x3e800000, 32)),
        primitive.bmov(bitvec.const(0, 32)),
    ]
    instruction = lifter.Semantics(
        "SM89", "contracts", "runtime observations", 0x100000, "primitive",
        writes=[(bitvec.reg("R", j, 1), e) for j, e in enumerate(expressions)],
        effects=[Effect("sass_prim_bar", (bitvec.const(0, 1),)),
                 Effect("sass_prim_bar", (bitvec.const(1, 1),))],
        flow=Flow("return", bitvec.const(0, 64)),
    )
    exported = k.export_instruction(instruction)
    encoder = k.Encoder()
    encoder.instruction(instruction)
    environment = f'("runtime_context" |-> {k.string(json.dumps(context, sort_keys=True))})'
    responses = []

    def observed(ident, name, args, width):
        start = len(provider.events)
        value = provider.call(name, args, (width + 7) // 8)
        events = k.klist(k.string(json.dumps(event, sort_keys=True)) for event in provider.events[start:])
        responses.append(dict(step=0, pc=0x100000, id=ident, name=name, arguments=args,
                              width=width, value=value, events=events))
        return value

    for index, effect in enumerate(instruction.effects):
        observed(-index - 1, effect.name,
                 [evaluate.evaluate(a, evaluate.State()) for a in effect.args], 0)
    expected = []
    for expr in expressions:
        ident = encoder.nodes[expr][0]
        expected.append(observed(ident, bitvec.PRIMITIVE_OP + expr.args[0],
                                 [evaluate.evaluate(a, evaluate.State()) for a in expr.args[1:]], expr.w))
    result = tools.execute(k.input_term(exported, responses=responses, environment=environment))
    assert result["status"] == "halted"
    assert result["regs"] == {("R", j): value for j, value in enumerate(expected)}
    assert [json.loads(event) for event in result["events"]] == provider.events
    assert result["environment"] == {"runtime_context": json.dumps(context, sort_keys=True)}
