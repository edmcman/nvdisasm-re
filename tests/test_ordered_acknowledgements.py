import pytest
from sass import ghidra_bridge
from sass.assemble import encoding
from sass.decode import ir
from sass.semantics import evaluate, lifter
from sass.semantics.runtime import Runtime
from tests.harness import select


def cases(arch):
    forms = (
        ("bar__ARV_RR_RR", dict(Rb=2, Rc=2), [37, 37], False),
        ("bar__SYNC_RR_RR", dict(Rb=2, Rc=2), [37, 37], False),
        ("bar__SYNC_dfrBlk_RR_RR", dict(Rb=2, Rc=2), [37, 37], False),
        ("bar__RED_RR_RR", dict(Rb=2, Rc=2, op="POPC", Pp=1), None, False),
        ("bar__RED_dfrBlk_RR_RR", dict(Rb=2, Rc=2, op="POPC", Pp=1), None, False),
        ("bar__SCAN_II_II", dict(Sb=0, Sc=32, Pp=1), [0, 32], True),
        ("bar__SYNCALL_noSrc_II", {}, [], False),
        ("bar__SYNCALL_dfrBlk_noSrc_II", {}, [], False),
    )
    md = ir.load(arch)
    classes = {klass.name: klass for klass in md.classes}
    for name, fields, inputs, guarded in forms:
        for enabled in (0, 1):
            arguments = inputs
            if arguments is None:
                op = md.enums[classes[name].operand_types["op"].type]["POPC"]
                arguments = [op, 37, 37, enabled]
            events = [] if guarded and not enabled else [dict(op="BAR", inputs=[1, *arguments])]
            word = encoding.encode(arch, name, **fields)
            yield dict(word=word, address=0x1000, registers=dict(R2=37, P1=enabled), observe=["R2", "P1"],
                       context=dict(synchronization={"bar": True})), events
    for enabled in (0, 1):
        registers, memory = dict(R2=37, P1=enabled), []
        if arch == "SM89":
            name, fields, inputs = "warpsync__RCR", dict(Pp=1, Sb_bank=0, Sb_addr=0x40), [0x12345678]
            if enabled: memory = [dict(space="cbank", address=0x40, hex="78563412")]
            variants = [(name, fields, inputs)]
        else:
            variants = [
                ("warpsync_collective__RIR", dict(sImm=32, Pp=1, all="ALL"), [0x1030]),
                ("warpsync_collective__RRR", dict(Ra=2, sImm=32, Pp=1), [37, 0x1030]),
            ]
        for name, fields, inputs in variants:
            events = [dict(op="WARPSYNC", inputs=[1, *inputs])] if enabled else []
            yield dict(word=encoding.encode(arch, name, **fields), address=0x1000, registers=registers,
                       memory=memory, observe=["R2", "P1"], context=dict(synchronization={"warpsync": True})), events
    for offset in (-32, 32):
        for enabled in (0, 1):
            events = [dict(op="BSSY", inputs=[1, 2, 0x1010 + offset])] if enabled else []
            yield dict(word=encoding.encode(arch, "bssy_", Sa=offset, barReg="B2", Pp=1), address=0x1000,
                       registers=dict(R2=37, P1=enabled), observe=["R2", "P1"],
                       context=dict(synchronization={"bssy": True})), events


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_standalone_acknowledgements_preserve_captured_event_inputs(arch):
    for request, events in cases(arch):
        state = evaluate.State({("R", 2): request["registers"]["R2"], ("P", 1): request["registers"]["P1"]})
        for memory in request.get("memory", ()):
            data = bytes.fromhex(memory["hex"])
            state.store(memory["space"], memory["address"], len(data) * 8, int.from_bytes(data, "little"))
        environment = Runtime(int(arch[2:]), request["context"])
        semantics = lifter.lift(arch, bytes.fromhex(request["word"]), request["address"])
        evaluate.apply(semantics, state, runtime=environment)
        assert state.pc == 0x1010 and environment.events == events
        assert state.regs == {("R", 2): 37, ("P", 1): request["registers"]["P1"]}


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_standalone_acknowledgements_in_both_ghidra_modes(arch, pytestconfig):
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    requests, expected = map(list, zip(*cases(arch)))
    results = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    for found in results.values():
        for request, events, result in zip(requests, expected, found):
            assert "error" not in result, result
            assert result["counter"] == 0x1010 and result.get("events", []) == events
            assert result["registers"] == request["registers"]
