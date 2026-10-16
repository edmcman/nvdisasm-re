"""Unconditional lifted control flow uses no condition; false and dynamic predicates survive."""
import pytest
from sass.decode import ir, pyencode
from sass.semantics import bitvec, evaluate, lifter
from tests.harness.select import arches


def pytest_generate_tests(metafunc):
    if "control_arch" in metafunc.fixturenames:
        metafunc.parametrize("control_arch", arches(metafunc.config))


@pytest.mark.parametrize("cls,fields,kind,target", [
    ("exit_", {}, "return", 0),
    ("bra_", dict(sImm=32), "goto", 0x1030),
    ("call_abs__RIR", dict(Sa=0x4560, depth="NOINC"), "call", 0x4560),
    ("ret__ABS", dict(Ra=2, Ra_offset=0, depth="NODEC"), "return", 0x4560),
])
@pytest.mark.parametrize("predicate,invert", [("PT", 0), ("PT", 1), (0, 0), (0, 1)])
def test_flow_conditions(control_arch, cls, fields, kind, target, predicate, invert):
    if cls == "bra_" and not any(k.name == cls for k in ir.load(control_arch).classes):
        cls, fields = "bra__U", {**fields, "cond": "U"}
    word = bytes.fromhex(pyencode.encode(control_arch, cls, **fields, Pp=predicate, **{"Pp@not": invert}))
    sem = lifter.lift(control_arch, word, 0x1000)
    assert sem.status == "native"
    assert sem.flow.kind == kind
    if predicate == "PT" and not invert:
        assert sem.flow.cond is None
    elif predicate == "PT":
        assert bitvec.is_const(sem.flow.cond, 0)
    else:
        assert sem.flow.cond is not None and not bitvec.is_const(sem.flow.cond)
    for value in (0, 1):
        state = evaluate.State()
        state.write("R", 2, 2, 0x4560)
        state.write("P", 0, 1, value)
        taken = (1 if predicate == "PT" else value) ^ invert
        assert evaluate.execute(sem, state).pc == (target if taken else 0x1010)
