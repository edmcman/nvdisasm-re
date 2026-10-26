"""The K export's executable vocabulary, provenance, and installable package data."""
import json
import pytest

from sass.assemble import encoding
from sass.formal import k
from sass.semantics import bitvec, evaluate, lifter
from sass.semantics.family import Flow, Store, Effect
from tests.harness.select import arches


def pytest_generate_tests(metafunc):
    if "export_arch" in metafunc.fixturenames:
        metafunc.parametrize("export_arch", arches(metafunc.config))


def semantics(**kwargs):
    return lifter.Semantics("SM89", "ir", "IR example", 0x100000, "native", **kwargs)


def test_vocabulary_and_package_data():
    assert k.OPERATORS == set(bitvec.OPS) | {"const", "param", "reg", "load", "ite", "primitive"}
    assert k.DEFINITION.is_file()
    for name in ("bits.k", "float.k"):
        assert k.DEFINITION.with_name(name).is_file()


def test_named_expansion_and_shared_nodes():
    from sass.semantics.families.integer import wide_product
    a, b = bitvec.reg("R", 1, 1), bitvec.reg("R", 2, 1)
    value = wide_product(a, b, False)
    sem = semantics(writes=[(bitvec.reg("R", 4, 2), value),
                            (bitvec.reg("R", 6, 2), value)])
    exported = k.export_instruction(sem)
    assert "mul.wide" not in exported.program
    assert '"mul"' in exported.program
    assert k.export_instruction(sem) == exported
    assert exported.contracts == ()
    encoder = k.Encoder()
    encoder.instruction(sem)
    lowered = bitvec.lowered(value)
    assert exported.program.count(encoder.nodes[lowered][1]) == 2


def test_contracts_and_effects():
    value = bitvec.primitive("read_lane", 32, bitvec.reg("R", 1, 1), bitvec.const(3, 32))
    sem = semantics(writes=[(bitvec.reg("R", 2, 1), value)],
                    effects=[Effect("sass_prim_bar", (bitvec.const(1, 1),))])
    sem.status = "primitive"
    exported = k.export_instruction(sem)
    manifest = exported.manifest()
    assert {c["name"] for c in manifest["contracts"]} == {"sass_prim_read_lane", "sass_prim_bar"}
    assert 'effect(-1, "sass_prim_bar"' in exported.program
    assert manifest["instructions"][0]["status"] == "primitive"
    assert json.loads(json.dumps(manifest))["format"] == 1


def test_machine_transition_export():
    sem = semantics(guard=bitvec.reg("P", 0, 1, element=1),
                    writes=[(bitvec.reg("R", 1, 1), bitvec.reg("R", 2, 1))],
                    stores=[Store("ram", bitvec.const(0x100, 64), bitvec.const(0x1234, 16))],
                    flow=Flow("goto", bitvec.const(0, 64)))
    exported = k.export_instruction(sem)
    assert 'store("ram"' in exported.program
    assert 'flow("goto"' in exported.program
    state = evaluate.State(regs={("R", 2): 9}, memory={"ram": {256: 12}})
    term = k.input_term(exported, state, params={"x": 3}, limit=7)
    assert 'location("R", 2) |-> 9' in term
    assert 'byte("ram", 256) |-> 12' in term
    assert '("x" |-> 3)' in term
    assert term.endswith(", 7)")


def test_opaque_is_explicit():
    sem = semantics()
    sem.status, sem.reason = "opaque", 'unknown "variant"'
    exported = k.export_instruction(sem)
    assert 'unsupported(1048576, absent, "unknown \\"variant\\"")' in exported.program
    assert exported.instructions[0]["reason"] == sem.reason


@pytest.mark.parametrize("operator", ["sel", "tuple", "future_operator"])
def test_unexecutable_nodes_rejected(operator):
    sem = semantics(writes=[(bitvec.reg("R", 0, 1), bitvec.Expr(operator, (), 32))])
    with pytest.raises(k.ExportError, match="unexecutable"):
        k.export_instruction(sem)


@pytest.mark.parametrize("code,base", [(b"", 16), (b"x", 16), (bytes(16), 0),
                                      (bytes(16), 17), (bytes(16), 1 << 64)])
def test_bad_program_rejected(code, base):
    with pytest.raises(k.ExportError):
        k.export_program("SM89", code, base)


def test_zero_address_is_not_silently_skipped():
    instruction = semantics()
    instruction.addr = 0
    with pytest.raises(k.ExportError, match="PC zero"):
        k.export_instruction(instruction)


@pytest.mark.parametrize("expr", [
    bitvec.Expr("add", (bitvec.const(1, 32),), 32),
    bitvec.Expr("ite", (bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(2, 32)), 32),
    bitvec.Expr("fadd", (bitvec.const(1, 32), bitvec.const(2, 32), "invalid"), 32),
    bitvec.Expr("extract", (bitvec.const(1, 32), 40, 9), 32),
])
def test_malformed_ir_rejected(expr):
    with pytest.raises(k.ExportError):
        k.export_instruction(semantics(writes=[(bitvec.reg("R", 0, 1), expr)]))


def test_instruction_sequence(export_arch):
    code = bytes.fromhex(encoding.encode(export_arch, "nop_") +
                         encoding.encode(export_arch, "exit_"))
    exported = k.export_program(export_arch, code)
    assert len(exported.instructions) == 2
    assert [m["address"] for m in exported.instructions] == [0x100000, 0x100010]
    assert exported.contracts == ()


def test_cli(tmp_path, export_arch):
    from sass.formal.k.__main__ import main
    source, output = tmp_path / "code.bin", tmp_path / "program.k"
    source.write_bytes(bytes.fromhex(encoding.encode(export_arch, "exit_")))
    main([str(source), "--arch", export_arch, "-o", str(output)])
    assert "module SASS-EXPORTED" in output.read_text()
    assert json.loads(output.with_suffix(".json").read_text())["k_version"] == k.VERSION


def test_cli_manifest_cannot_overwrite_source(tmp_path):
    from sass.formal.k.__main__ import main
    with pytest.raises(SystemExit):
        main([str(tmp_path / "unused.bin"), "--arch", "SM89", "-o", str(tmp_path / "source.json")])
    assert not (tmp_path / "source.json").exists()


def test_bad_encoding_has_address_context():
    with pytest.raises(k.ExportError, match="SM89 at 0x100000"):
        k.export_program("SM89", bytes(16))


def test_response_validation():
    exported = k.export_instruction(semantics())
    response = dict(step=0, pc=0x100000, id=0, name="sass_prim_lane",
                    arguments=[], width=32, value=0)
    with pytest.raises(k.ExportError, match="duplicate"):
        k.input_term(exported, responses=[response, response])
    with pytest.raises(k.ExportError, match="width"):
        k.input_term(exported, responses=[{**response, "value": -1}])
    with pytest.raises(k.ExportError, match="width"):
        k.input_term(exported, responses=[{**response, "value": 1 << 32}])
    with pytest.raises(k.ExportError, match="limit"):
        k.input_term(exported, limit=0)
