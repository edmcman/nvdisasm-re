"""Execute the handwritten IR definition, independently of Python's evaluator."""
from dataclasses import replace
from pathlib import Path
import random
import pytest

from sass.assemble import encoding
from sass.formal import k
from sass.formal.k import tools
from sass.semantics import bitvec, evaluate, lifter
from sass.semantics.family import Flow, Store, Effect
from tests.harness import lifted, select

pytestmark = pytest.mark.k
BASE = 0x100000


@pytest.fixture(scope="module", autouse=True)
def require_k():
    if not tools.available(): pytest.fail("K is required: see docs/semantics.md, K backend")


def sem(**kwargs):
    return lifter.Semantics("SM89", "IR", "IR", BASE, "native",
                           flow=kwargs.pop("flow", Flow("return", bitvec.const(0, 64))), **kwargs)


def run_one(instruction, state=None, **kwargs):
    return tools.execute(k.input_term(k.export_instruction(instruction), state, **kwargs))


def run_expressions(expressions, params=None):
    """Compare actual numeric results, without testing equality inside the K model."""
    writes, observations = [], []
    for index, expr in enumerate(expressions):
        count = (expr.w + 31) // 32
        file, location = ("P", bitvec.reg("P", index, 1, element=1)) if expr.w == 1 else (
            "R", bitvec.reg("R", index * 8, count))
        value = expr if location.w == expr.w else bitvec.zext(expr, location.w)
        writes.append((location, value))
        expected = evaluate.evaluate(expr, evaluate.State(), params)
        observations.append((expr, file, location.args[1], count, expected))
    result = run_one(sem(writes=writes), params=params)
    assert result["status"] == "halted"
    for expr, file, index, count, expected in observations:
        element = 1 if file == "P" else 32
        actual = sum(result["regs"].get((file, index + j), 0) << (j * element) for j in range(count))
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
    instruction = sem(
        writes=[(r0, r1), (r1, r0), (r0, r0 + 1)],
        stores=[Store("ram", bitvec.const(0x80, 64), r0),
                Store("ram", bitvec.const(0x81, 64), bitvec.load("ram", bitvec.const(0x80, 64), 16))])
    expected = evaluate.execute(instruction, state)
    result = run_one(instruction, state)
    assert result["regs"] == expected.regs
    assert result["memory"] == {(space, address): value for space, memory in expected.memory.items()
                                for address, value in memory.items()}


def test_predicated_off_does_not_read_inputs():
    instruction = sem(guard=bitvec.const(0, 1),
                      writes=[(bitvec.reg("R", 0, 1), bitvec.param("missing", 32))],
                      effects=[Effect("sass_prim_bar", (bitvec.const(1, 1),))])
    result = run_one(instruction, evaluate.State(regs={("R", 0): 17}), limit=1)
    assert result["regs"] == {("R", 0): 17}
    assert result["events"] == []
    assert result["pc"] == BASE + 16
    assert result["status"] == "instruction limit"


def test_explicit_contracts_and_shared_primitive():
    value = bitvec.primitive("lane", 32)
    instruction = sem(writes=[(bitvec.reg("R", 0, 1), value), (bitvec.reg("R", 1, 1), value)],
                      effects=[Effect("sass_prim_bar", (bitvec.const(1, 1),))])
    instruction.status = "primitive"
    exported = k.export_instruction(instruction)
    sites = exported.instructions[0]["runtime_sites"]
    node = next(s for s in sites if s["kind"] == "value")
    responses = [
        dict(step=0, pc=BASE, id=-1, name="sass_prim_bar", arguments=[1], width=0, value=0,
             events='ListItem("BAR")', environment='("acknowledged" |-> 1)'),
        dict(step=0, pc=BASE, id=node["id"], name="sass_prim_lane", arguments=[], width=32, value=7,
             before='("acknowledged" |-> 1)',
             environment='("acknowledged" |-> 1)'),
    ]
    result = tools.execute(k.input_term(exported, responses=responses))
    assert result["regs"] == {("R", 0): 7, ("R", 1): 7}
    assert result["events"] == ["BAR"]  # the primitive is invoked only once
    assert result["environment"] == {"acknowledged": 1}
    missing = tools.execute(k.input_term(exported))
    assert missing["status"] == "runtime response missing"
    mismatch = tools.execute(k.input_term(exported, responses=[{**responses[0], "arguments": [0]}]))
    assert mismatch["status"] == "runtime response mismatch"


def test_program_branches_and_limits():
    from sass.assemble import encoding
    code = bytes.fromhex(
        encoding.encode("SM89", "bra_", sImm=16) +
        encoding.encode("SM89", "nop_") +
        encoding.encode("SM89", "exit_"))
    result = tools.execute(k.input_term(k.export_program("SM89", code)))
    assert result["status"] == "halted"
    assert result["step"] == 2
    loop = sem(flow=Flow("goto", bitvec.const(BASE, 64)))
    result = run_one(loop, limit=3)
    assert result["status"] == "instruction limit"
    assert result["step"] == 3
    opaque = replace(sem(), status="opaque", reason="unmodelled modifier")
    assert run_one(opaque)["status"] == "unmodelled modifier"
    assert run_one(sem(flow=None))["status"] == "PC outside program"


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
    input.write_text("run(exportedProgram(), .Map, .Map, .Map, .Map, .Map, 10)")
    result = tools.invoke("krun", input, "--definition", definition, "--output", "json")
    assert '\\"halted\\"' in result.stdout


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
                    if instruction.status != "native" or instruction.effects: continue
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
