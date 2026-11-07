import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.semantics import lifter, ordered
from sass.sleigh.build import ARCHES
from sass.sleigh.gen import Gen
from sass.sleigh.registers import REG_FILES
from tests.harness import dataflow, lifted
from tests.harness.case import Case, Cases
from tests.refs.float import hsetp2


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("table", [0, 0xff])
@pytest.mark.parametrize("operation", ["POR", "PAND"])
@pytest.mark.parametrize("destination", [2, 7])
def test_constant_lop3_captures_only_live_predicate_inputs(arch, fp, table, operation, destination):
    word = encoding.encode(arch, "lop3_lut__RRR_RRR", Rd=8, Pu=destination,
                           Ra=2, Rb=4, Rc=6, imm8=table, Pp=2, pop=operation)
    semantics = lifter.lift(arch, bytes.fromhex(word), fp=fp)
    predicate_reads = [statement.location for statement in ordered.linearize(semantics.body)
                       if isinstance(statement, ordered.ReadRegister) and statement.location[0] == "P"]
    constant = bool(table)
    outcomes = (lambda p: constant or p) if operation == "POR" else (lambda p: constant and p)
    depends = destination != 7 and outcomes(False) != outcomes(True)
    assert (("P", 2, 1) in predicate_reads) == depends
    for predicate in (0, 1):
        expected = {"R8": 0xffffffff if constant else 0,
                    "P2": int(outcomes(bool(predicate))) if destination != 7 else predicate}
        case = Case("lop3_lut__RRR_RRR", Cases.request(word, {"P2": predicate}, expected), expected)
        assert lifted.run(arch, fp, case) == ("native", [])


@pytest.mark.parametrize("comparison", ["F", "T"])
@pytest.mark.parametrize("operation", ["AND", "OR", "XOR"])
@pytest.mark.parametrize("combined", [False, True])
def test_constant_hsetp2_dependency_matches_both_live_outputs(comparison, operation, combined):
    word = encoding.encode("SM75", "hsetp2__RR", Pu=2, Pv=3, Ra=4, Rc=6, Pp=1,
                           cmp=comparison, bop=operation, h_and="H_AND" if combined else "noh_and")
    decoded = pydecode.decode("SM75", bytes.fromhex(word))
    reads, _ = dataflow.expected(Gen("SM75", "exact"), decoded, optimized=True)
    outcomes = [hsetp2(0, 0, comparison, operation, p=predicate, h_and=combined)
                for predicate in (False, True)]
    base, stride, *_ = REG_FILES["P"]
    assert (base + stride in reads) == (outcomes[0] != outcomes[1])


@pytest.mark.parametrize("arch,cls,fmt", [
    ("SM89", "hfma2_relu__RRR", "F16_V2"), ("SM89", "hfma2_relu__RRC", "F16_V2"),
    ("SM90", "hfma2_mma_relu__RRR", "F16_V2"), ("SM90", "hfma2_mma_relu__RRC", "F16_V2"),
    ("SM89", "hfma2_relu__RRR", "BF16_V2"), ("SM89", "hfma2_relu__RRC", "BF16_V2"),
])
@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("predicate", [0, 1])
@pytest.mark.parametrize("destination", [2, 255])
def test_half_fma_relu_captures_arithmetic_once_and_preserves_discarded_outputs(
        arch, cls, fmt, fp, predicate, destination):
    memory_operand = cls.endswith("RRC")
    inputs = {"Rb": 4, "Sc_bank": 0, "Sc_addr": 32} if memory_operand else {"Rb": 4, "Rc": 6}
    word = encoding.encode(arch, cls, Rd=destination, Ra=2, Pp=1, satrelu="RELU",
                           ofmt=fmt, **inputs)
    semantics = lifter.lift(arch, bytes.fromhex(word), fp=fp)
    statements = ordered.linearize(semantics.body)
    loads = [statement for statement in statements if isinstance(statement, ordered.ReadMemory)]
    assert len(loads) == int(memory_operand and destination != 255)
    assert not any(isinstance(statement, ordered.If) for statement in semantics.body.statements)
    result = 0 if predicate else 0xc000c000
    minus_one = {"F16_V2": 0xbc00bc00, "BF16_V2": 0xbf80bf80}[fmt]
    expected = {"R2": minus_one if destination == 255 else result}
    extras = {"memory": [dict(space="cbank", address=32, hex="00000000")]} if memory_operand else {}
    case = Case(cls, Cases.request(word, {"R2": minus_one, "R4": 0x40004000,
                                         "R6": 0, "P1": predicate}, expected, extras=extras), expected)
    assert lifted.run(arch, fp, case) == ("native", [])
