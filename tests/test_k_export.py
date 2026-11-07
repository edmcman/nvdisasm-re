"""The K export's executable vocabulary, provenance, and installable package data."""
import json
import pytest

from sass.assemble import encoding
from sass.formal import k
from sass.semantics import bitvec, evaluate, lifter, ordered
from tests.harness.select import arches


def pytest_generate_tests(metafunc):
    if "export_arch" in metafunc.fixturenames:
        metafunc.parametrize("export_arch", arches(metafunc.config))


def semantics(body=None, writes=()):
    if body is None:
        builder = ordered.Builder()
        builder.assign_from_inputs((location.args, value) for location, value in writes)
        body = builder.block()
    return lifter.Semantics("SM89", "ir", "IR example", 0x100000, "native", body=body)


def test_vocabulary_and_package_data():
    assert k.OPERATORS == set(bitvec.OPS) | {"const", "param", "reg", "ite"}
    assert k.DEFINITION.is_file()
    for source in k.SOURCES:
        assert source.is_file()


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
    assert any(name.startswith("mul.wide") for name in exported.instructions[0]["definitions"])
    encoder = k.Encoder()
    encoder.instruction(sem)
    assert sum(node.op == "mul" for node in encoder.nodes) == 1
    assert exported.program.count('orderedWrite("R"') == 2


def test_repeated_defined_expression_preserves_unique_node_ids():
    from sass.semantics.families.floating import float_test
    builder = ordered.Builder()
    a, b = builder.read_register("R", 0, 2), builder.read_register("R", 2, 2)
    comparison = float_test("LT", a, b)
    for index, value in enumerate((~comparison, comparison)):
        captured = builder.move(value)
        builder.emit(ordered.WriteRegister(("P", index, 1), captured))
    encoder = k.Encoder()
    encoder.instruction(semantics(builder.block()))
    ids = [ident for ident, _ in encoder.nodes.values()]
    assert len(set(ids)) == len(ids)
    assert sum(node.op == "flt" for node in encoder.nodes) == 1


def test_contracts_and_effects():
    builder = ordered.Builder()
    builder.special(ordered.BARRIER_ACKS["all"])
    source = builder.read_register("R", 1)
    snapshot, = builder.special(ordered.CAPTURE, bitvec.const(0, 32), bitvec.const(15, 32), source)
    value, = builder.special(ordered.READ_LANE, snapshot, bitvec.const(3, 32))
    builder.special(ordered.RELEASE_SNAPSHOT, snapshot)
    builder.emit(ordered.WriteRegister(("R", 2, 1), value))
    sem = semantics(builder.block())
    sem.status = "primitive"
    exported = k.export_instruction(sem)
    manifest = exported.manifest()
    assert manifest["contracts"] == []
    assert {c["name"] for c in manifest["operations"]} == {
        ordered.BARRIER_ACKS["all"].name, ordered.CAPTURE.name,
        ordered.READ_LANE.name, ordered.RELEASE_SNAPSHOT.name}
    sites = manifest["instructions"][0]["runtime_sites"]
    assert [site["id"] for site in sites] == [0, 2, 3, 4]
    assert [site["results"] for site in sites] == [[], ["warp_snapshot"], [32], []]
    assert manifest["instructions"][0]["status"] == "primitive"
    assert json.loads(json.dumps(manifest))["format"] == 2


def test_machine_transition_export():
    builder = ordered.Builder()
    guard = builder.read_register("P", 0)
    arm = ordered.Builder(builder.count)
    value = arm.read_register("R", 2)
    arm.emit(ordered.WriteRegister(("R", 1, 1), value))
    arm.emit(ordered.WriteMemory("ram", bitvec.const(0x100, 64), bitvec.const(0x1234, 16)))
    arm.emit(ordered.Transfer("goto", bitvec.const(0, 64)))
    builder.emit(ordered.If(guard, arm.block()))
    sem = semantics(builder.block())
    exported = k.export_instruction(sem)
    assert 'orderedStore("ram"' in exported.program
    assert 'orderedControl("goto"' in exported.program
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
    sem = semantics(ordered.Block((ordered.WriteRegister(("R", 0, 1), bitvec.Expr(operator, (), 32)),)))
    with pytest.raises(k.ExportError):
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
    bitvec.Expr("lzcount", (), 32),
    bitvec.Expr("lzcount", (1,), 32),
    bitvec.Expr("lzcount", (bitvec.const(1, 32), bitvec.const(0, 32)), 32),
])
def test_malformed_ir_rejected(expr):
    with pytest.raises(k.ExportError):
        k.export_instruction(semantics(ordered.Block((ordered.WriteRegister(("R", 0, 1), expr),))))


def test_leading_zero_count_export():
    value = bitvec.Expr("lzcount", (bitvec.reg("R", 1, 1),), 32)
    exported = k.export_instruction(semantics(writes=[(bitvec.reg("R", 0, 1), value)]))
    assert '"lzcount"' in exported.program
    assert exported.contracts == ()


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
