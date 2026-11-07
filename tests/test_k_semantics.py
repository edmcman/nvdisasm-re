"""Execute the handwritten IR definition, independently of Python's evaluator."""
from dataclasses import replace
from pathlib import Path
import random
import pytest

from sass.assemble import encoding
from sass.formal import k
from sass.formal.k import tools
from sass.semantics import bitvec, evaluate, lifter, ordered
from tests.harness import lifted, select

pytestmark = pytest.mark.k
BASE = 0x100000


@pytest.fixture(scope="module", autouse=True)
def require_k():
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")


def sem(body=None, *, writes=(), transfer="return"):
    if body is None:
        builder = ordered.Builder()
        builder.assign_from_inputs((location.args, value) for location, value in writes)
        body = builder.block()
    if transfer is not None:
        body = ordered.Block((*body.statements, ordered.Transfer(transfer, bitvec.const(0, 64))))
    return lifter.Semantics("SM89", "IR", "IR", BASE, "native", body=body)


def run_one(instruction, state=None, **kwargs):
    return tools.execute(k.input_term(k.export_instruction(instruction), state, **kwargs))


def run_expressions(expressions, params=None):
    """Compare actual numeric results, without testing equality inside the K model."""
    builder, observations = ordered.Builder(), []
    for expr in expressions:
        result = builder.move(expr)
        expected = evaluate.evaluate(expr, evaluate.State(), params)
        observations.append((expr, ordered.temp_index(result), expected))
    result = run_one(sem(builder.block()), params=params)
    assert result["status"] == "halted"
    for expr, index, expected in observations:
        actual = result["temporaries"][index]
        assert actual == expected, f"{expr.op}, width {expr.w}: {actual:#x} != {expected:#x}"


def raw(op, width, *args):
    return bitvec.Expr(op, args, width)


def test_integer_vocabulary():
    expressions = []
    for width in (1, 3, 8, 16, 32, 64, 65, 128):
        m = bitvec.mask(width)
        values = (0, 1, m // 2, m)
        for a, b in zip(values, reversed(values)):
            a, b = bitvec.const(a, width), bitvec.const(b, width)
            for op in ("add", "sub", "mul", "and", "or", "xor"):
                expressions.append(raw(op, width, a, b))
            for op in ("eq", "ne", "ult", "ule", "slt", "sle"):
                expressions.append(raw(op, 1, a, b))
            for op in ("not", "neg"): expressions.append(raw(op, width, a))
            for count in (0, width - 1, width, width + 7):
                for op in ("shl", "shr", "sar"):
                    expressions.append(raw(op, width, a, bitvec.const(count, 16)))
            expressions += [raw("zext", width + 32, a), raw("sext", width + 32, a)]
            expressions.append(raw("extract", 1, a, width - 1, width - 1))
            expressions.append(raw("concat", width * 2, a, b))
    run_expressions(expressions)


def test_leading_zero_count():
    expressions = [
        raw("lzcount", result_width, bitvec.const(value, width))
        for width in (1, 3, 8, 16, 32, 64, 65, 128)
        for value in (0, *(1 << bit for bit in range(width)), bitvec.mask(width))
        for result_width in (1, 8, width)
    ]
    run_expressions(expressions)


def float_values(width):
    e, m = {16: (5, 10), 32: (8, 23), 64: (11, 52)}[width]
    sign, infinity = 1 << (width - 1), ((1 << e) - 1) << m
    one = ((1 << (e - 1)) - 1) << m
    return (0, sign, 1, sign | 1, (1 << m) - 1, 1 << m, one, sign | one,
            one + 1, infinity - 1, sign | (infinity - 1), infinity,
            sign | infinity, infinity | 1, sign | infinity | (1 << (m - 1)))


def test_exact_floating_vocabulary():
    expressions = []
    rng = random.Random(20261026)
    for width in (16, 32, 64):
        values = float_values(width)
        pairs = [(v, values[(j * 7 + 3) % len(values)]) for j, v in enumerate(values)]
        pairs += [(rng.getrandbits(width), rng.getrandbits(width)) for _ in range(12)]
        pairs += [(0, 0), (1 << (width - 1), 1 << (width - 1)),
                  (values[6], values[7]), (values[9], values[9])]
        for a, b in pairs:
            a, b = bitvec.const(a, width), bitvec.const(b, width)
            for mode in ("RN", "RZ", "RM", "RP"):
                expressions += [raw("fadd", width, a, b, mode),
                                raw("fmul", width, a, b, mode, -3),
                                raw("fmul", width, a, b, mode, 2),
                                raw("ffma", width, a, b, bitvec.const(values[7], width), mode)]
            for op in ("flt", "fle", "feq"): expressions.append(raw(op, 1, a, b))
            expressions += [raw("fnan", 1, a),
                            raw("nanresult", width, a, bitvec.const(7, width))]
        for destination in (16, 32, 64):
            for value in values:
                for mode in ("RN", "RZ", "RM", "RP"):
                    expressions.append(raw("fconv", destination, bitvec.const(value, width), mode))
    run_expressions(expressions)


def test_lazy_and_unbound_parameters():
    bad = bitvec.param("missing", 32)
    expr = raw("ite", 32, bitvec.const(0, 1), bad, bitvec.param("good", 32))
    run_expressions([expr], params={"good": 1 << 40 | 17})
    result = run_one(sem(writes=[(bitvec.reg("R", 0, 1), bad)]))
    assert result["status"] == "unbound parameter"
    assert result["regs"] == {}


def test_aliasing_and_memory_snapshot():
    r0, r1 = bitvec.reg("R", 0, 1), bitvec.reg("R", 1, 1)
    state = evaluate.State(regs={("R", 0): 0x1234, ("R", 1): 0x5678})
    state.store("ram", 0x80, 32, 0xaabbccdd)
    builder = ordered.Builder()
    before = builder.read_register("R", 0)
    memory = builder.read_memory("ram", bitvec.const(0x80, 64), 16)
    builder.assign_from_inputs([(r0.args, r1), (r1.args, r0), (r0.args, r0 + 1)])
    builder.emit(ordered.WriteMemory("ram", bitvec.const(0x80, 64), before))
    builder.emit(ordered.WriteMemory("ram", bitvec.const(0x81, 64), memory))
    instruction = sem(builder.block())
    expected = evaluate.execute(instruction, state)
    result = run_one(instruction, state)
    assert result["regs"] == expected.regs
    assert result["regs"] == {("R", 0): 0x1235, ("R", 1): 0x1234}
    assert result["memory"] == {(space, address): value for space, memory in expected.memory.items()
                                for address, value in memory.items()}
    assert [result["memory"]["ram", address] for address in range(0x80, 0x84)] == [0x34, 0xdd, 0xcc, 0]


def test_predicated_off_does_not_read_inputs():
    arm = ordered.Builder()
    arm.emit(ordered.WriteRegister(("R", 0, 1), bitvec.param("missing", 32)))
    arm.special(ordered.BARRIER_ACKS["all"])
    arm.emit(ordered.Transfer("return", bitvec.const(0, 64)))
    instruction = sem(ordered.Block((ordered.If(bitvec.const(0, 1), arm.block()),)), transfer=None)
    result = run_one(instruction, evaluate.State(regs={("R", 0): 17}), limit=1)
    assert result["regs"] == {("R", 0): 17}
    assert result["events"] == []
    assert result["pc"] == BASE + 16
    assert result["status"] == "instruction limit"


def test_explicit_contracts_and_shared_primitive():
    builder = ordered.Builder()
    operation = ordered.BARRIER_ACKS["packed_wait"]
    builder.special(operation, bitvec.const(0, 32), bitvec.const(32, 32))
    value, = builder.special(ordered.LANE)
    for index in (0, 1): builder.emit(ordered.WriteRegister(("R", index, 1), value))
    instruction = sem(builder.block())
    instruction.status = "primitive"
    exported = k.export_instruction(instruction)
    sites = exported.instructions[0]["runtime_sites"]
    assert [site["name"] for site in sites] == [operation.name, ordered.LANE.name]
    responses = [
        dict(step=0, pc=BASE, id=0, name=operation.name, arguments=[0, 32], values=[],
             events='ListItem("BAR")', environment='("acknowledged" |-> 1)'),
        dict(step=0, pc=BASE, id=1, name=ordered.LANE.name, arguments=[], values=[7],
             before='("acknowledged" |-> 1)',
             environment='("acknowledged" |-> 1)'),
    ]
    result = tools.execute(k.input_term(exported, responses=responses))
    assert result["regs"] == {("R", 0): 7, ("R", 1): 7}
    assert result["events"] == ["BAR"]  # the primitive is invoked only once
    assert result["environment"] == {"acknowledged": 1}
    missing = tools.execute(k.input_term(exported))
    assert missing["status"] == "runtime response missing"
    mismatch = tools.execute(k.input_term(exported, responses=[{**responses[0], "arguments": [1, 32]}]))
    assert mismatch["status"] == "runtime response mismatch"


def test_program_branches_and_limits():
    from sass.assemble import encoding
    code = bytes.fromhex(
        encoding.encode("SM89", "bra_", sImm=16) +
        encoding.encode("SM89", "nop_") +
        encoding.encode("SM89", "exit_"))
    exported = k.export_program("SM89", code)
    responses = [dict(step=step, pc=BASE + offset, id=0,
                      name=ordered.BLOCK_WAIT_DEFERRED.name, arguments=[], values=[])
                 for step, offset in ((0, 0), (1, 32))]
    result = tools.execute(k.input_term(exported, responses=responses))
    assert result["status"] == "halted"
    assert result["step"] == 2
    loop = sem(ordered.Block((ordered.Transfer("goto", bitvec.const(BASE, 64)),)), transfer=None)
    result = run_one(loop, limit=3)
    assert result["status"] == "instruction limit"
    assert result["step"] == 3
    opaque = replace(sem(), status="opaque", reason="unmodelled modifier")
    assert run_one(opaque)["status"] == "unmodelled modifier"
    assert run_one(sem(transfer=None))["status"] == "PC outside program"


def test_haskell_execution():
    instruction = sem(writes=[(bitvec.reg("R", 0, 1), bitvec.const(123, 32))])
    result = tools.execute(k.input_term(k.export_instruction(instruction)), backend="haskell")
    assert result["status"] == "halted"
    assert result["regs"] == {("R", 0): 123}


def test_generated_module(tmp_path):
    exported = k.export_program("SM89", bytes.fromhex(encoding.encode("SM89", "exit_")))
    source = tmp_path / "program.k"
    source.write_text(exported.source)
    definition = tmp_path / "compiled"
    tools.invoke("kompile", source, "--main-module", "SASS-EXPORTED", "--syntax-module", "SASS-EXPORTED",
                 "--backend", "haskell", "--output-definition", definition, "-I", k.DEFINITION.parent,
                 timeout=180)
    input = tmp_path / "input.sass"
    response = dict(step=0, pc=BASE, id=0, name=ordered.BLOCK_WAIT_DEFERRED.name,
                    arguments=[], values=[])
    input.write_text(k.input_term(exported, responses=[response], limit=10).replace(
        exported.program, "exportedProgram()", 1))
    result = tools.invoke("krun", input, "--definition", definition, "--output", "json")
    assert '\\"halted\\"' in result.stdout


def test_document_example(tmp_path):
    from sass.formal.k import example, execution
    worked = example.make()
    for predicate, value, steps in ((0, 2, 4), (1, 0, 3)):
        state, trace, responses = worked.run(predicate)
        assert state.pc == 0 and state.regs == {("R", 0): value, ("P", 0): predicate}
        assert len(trace) == steps
        result = tools.execute(k.input_term(worked.exported, worked.initial(predicate),
                                           responses=responses, limit=example.LIMIT))
        assert result["status"] == "halted" and result["pc"] == 0
        assert result["step"] == steps and result["regs"] == state.regs
        assert result["memory"] == {} and result["events"] == []
    proof = execution.publication(worked, tmp_path, example.LIMIT, prove=True)
    assert proof["status"] == "proved" and proof["claim_count"] == 11
    assert "#Top" in proof["output"]
    assert (tmp_path / "proof.log").read_text().startswith(proof["output"])
    # A wrong intermediate result must fail even when the other claims are valid.
    source = execution.specification(worked, example.LIMIT)
    assert source == (tmp_path / "claims.k").read_text()
    quoted = execution.complete_claims(source)
    assert quoted.count("  claim ") == 2
    for label in ("p0-complete", "p1-complete"):
        assert f"[label({label})]" in quoted
    unchecked = worked.publication(tmp_path / "unchecked")
    assert quoted in unchecked
    assert "K was not run for this publication." in unchecked
    assert "C_j" not in unchecked and r"\frac{" not in unchecked
    target = '=> (location("R", 0) |-> 0) (location("P", 0) |-> 0) </regs>'
    assert target in source
    wrong = source.replace(target, target.replace("|-> 0)", "|-> 1)", 1), 1)
    path = tmp_path / "wrong.k"
    path.write_text(wrong)
    result = tools.prove(path, "SASS-EXECUTION-PROOF", check=False)
    assert result.returncode == 1 and "#Top" not in result.stdout
    assert "[Error] Prover:" in result.stderr


def test_symbolic_claims():
    root = Path(__file__).resolve().parents[1] / "examples/formal"
    result = tools.prove(root / "claims.k", "SASS-CLAIMS", timeout=180)
    assert "#Top" in result.stdout
    false = tools.prove(root / "false-claim.k", "SASS-FALSE-CLAIM", check=False)
    assert false.returncode == 1
    assert "[Error] Inner Parser:" not in false.stderr and "[Error] Outer Parser:" not in false.stderr
    assert "[Error] Compiler:" not in false.stderr
    assert "[Error] Prover:" in false.stderr and "#Top" not in false.stdout


def pytest_generate_tests(metafunc):
    if "shared_arch" in metafunc.fixturenames:
        metafunc.parametrize("shared_arch", select.arches(metafunc.config))


def test_shared_native_cases(shared_arch, request):
    """Retain one native case per selected family/mode as an integration sample."""
    from tests.cases import emulator_cases
    checked = 0
    for mode in select.modes(request.config):
        for _, cases in emulator_cases(shared_arch, mode, select.families(request.config), generated=False).items():
            for case in cases:
                if "word" not in case.request or not lifted.applies(case): continue
                try:
                    instruction = lifter.lift(shared_arch, bytes.fromhex(case.request["word"]), BASE, fp=mode)
                    if instruction.status != "native" or \
                            any(isinstance(statement, ordered.Special) for statement in ordered.linearize(instruction.body)):
                        continue
                    state = lifted.state(shared_arch, case.request)
                    expected = evaluate.execute(instruction, state)
                except (ValueError, evaluate.Unknown): continue
                result = run_one(instruction, state, limit=1)
                assert result["regs"] == expected.regs, case.id
                assert result["pc"] == expected.pc, case.id
                assert result["memory"] == {(s, a): v for s, memory in expected.memory.items()
                                            for a, v in memory.items()}, case.id
                checked += 1
                break
    assert checked, "no applicable shared cases were selected"
