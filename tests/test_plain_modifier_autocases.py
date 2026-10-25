"""New narrowing and load variants use the shared generated-case path."""
import json
import pytest
from sass.assemble import encoding
from sass.decode import ir, pydecode
from sass.sleigh import modifiers
from sass.sleigh.build import ARCHES
from tests.harness import autocases, lifted


@pytest.mark.parametrize("arch", ARCHES)
def test_normal_plans_represent_each_added_variant(arch):
    plan = json.loads((autocases.PLANS / f"{arch}.json").read_text())
    assert plan["verification"] == {"default_strength": 3, "opcode_strength": {}}
    md = ir.load(arch)
    classes = {k.name: k for k in md.classes}
    chosen = {}
    for entry in plan["forms"]:
        chosen.setdefault(entry["cls"], []).append(entry["settings"])
    for name, k in classes.items():
        if k.mnemonic == "I2I":
            assert {f["dstfmt"] for f in chosen.get(name, [])} == {"S8", "U8", "S16", "U16"}, name
    if arch == "SM75":
        assert {"U", "U.U8", "U.S8", "U.U16", "U.S16", "U.64", "U.128"} <= {
            f["sz"] for f in chosen["lds__sImmOffset"]}
        assert all(entry["native"] for entry in plan["forms"]
                   if entry["cls"] == "lds__sImmOffset" and entry["settings"]["sz"] in {"U.64", "U.128"})
    # Every supported prefetch label must be represented for every class that
    # implements it, independently of the planner's covering-array algorithm.
    # Python CI runs without generated language manifests. Assert the added
    # hint variants directly against the committed native case selection.
    native_prefetch_classes = {
        entry["cls"] for entry in plan["forms"]
        if entry["native"] and classes[entry["cls"]].mnemonic in ("LD", "LDG")
        and "sp2" in entry["settings"]}
    assert native_prefetch_classes
    for name in native_prefetch_classes:
        etype = classes[name].operand_types["sp2"].type
        # SM75 reserves the fourth SP2 encoding; newer architectures name it
        # LTC256B. Canonical md labels exclude that reserved value.
        expected = {modifiers.label(md, etype, value) for value in md.enums[etype].values()} & {
            "nosp2", "LTC64B", "LTC128B", "LTC256B"}
        assert expected <= {f["sp2"] for f in chosen[name]}, name


@pytest.mark.parametrize("arch", ["SM75", "SM80", "SM86", "SM89", "SM90",
                                  "SM100", "SM101", "SM103", "SM107", "SM120"])
@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_i2i_all_sources_and_destination_bounds(arch, fp):
    for k in ir.load(arch).classes:
        if k.mnemonic != "I2I": continue
        for fmt in ("S8", "U8", "S16", "U16"):
            cases = autocases.generate(arch, k.name, {"dstfmt": fmt}, fp, sample=100)
            assert len(cases) == 19
            width, signed = int(fmt[1:]), fmt.startswith("S")
            lo = -(1 << (width - 1)) if signed else 0
            hi = (1 << (width - int(signed))) - 1
            results = {v for c in cases for n, v in c.expected.items() if n.startswith("R")}
            assert {lo & 0xffffffff, hi} <= results
            assert any("R2" in c.expected for c in cases)
            for case in cases:
                status, problems = lifted.run(arch, fp, case)
                assert status == "native" and not problems, (case, problems)


@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("sz", ["U", "U.U8", "U.S8", "U.U16", "U.S16", "U.64", "U.128"])
def test_sm75_uniform_shared_sizes(sz, fp):
    cases = autocases.generate("SM75", "lds__sImmOffset", {"sz": sz, "stride": "X1"}, fp, sample=100)
    assert cases
    for case in cases:
        status, problems = lifted.run("SM75", fp, case)
        assert status == "native" and not problems, (case, problems)


@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("hint", ["nosp2", "LTC64B", "LTC128B", "LTC256B"])
@pytest.mark.parametrize("sz", ["U8", "S8", "U16", "S16", "32", "64", "128"])
def test_ldg_predicate_completion_alias_and_instruction_guard(sz, hint, fp):
    word = encoding.encode("SM89", "ldg__sImmOffset", Rd=8, Ra=2, e="E",
                           sz=sz, sp2=hint, Pu=0, Pnz=1)
    form = modifiers.settings("SM89", pydecode.decode("SM89", bytes.fromhex(word)))
    cases = autocases.generate("SM89", "ldg__sImmOffset",
                              form, fp, sample=100)
    assert any(c.expected.get("P0") == 0 and c.request["registers"].get("P0") == 1 for c in cases)
    assert any(c.expected.get("P1") == 0 and c.request["registers"].get("P1") == 1 for c in cases)
    assert any(c.expected.get("P0") == 1 and c.expected.get("__events") == 0 for c in cases)
    for case in cases:
        status, problems = lifted.run("SM89", fp, case)
        assert status == "native" and not problems, (case, problems)


@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_prefetch_profiles_issue_loads_for_each_hint(fp):
    classes = {k.name: k for k in ir.load("SM89").classes}
    plan = json.loads((autocases.PLANS / "SM89.json").read_text())["forms"]
    representatives = {}
    for entry in plan:
        opcode = classes[entry["cls"]].mnemonic
        if opcode in ("LD", "LDG") and entry["native"] and "sp2" in entry["settings"]:
            representatives.setdefault((opcode, entry["settings"]["sp2"]), entry)
    for opcode in ("LD", "LDG"):
        assert {hint for op, hint in representatives if op == opcode} == {
            "nosp2", "LTC64B", "LTC128B", "LTC256B"}
    for entry in representatives.values():
        cases = autocases.generate("SM89", entry["cls"], entry["settings"], fp,
                                   entry["profiles"], True, sample=100)
        assert any(v for c in cases for n, v in c.expected.items() if n.startswith("R"))
        for case in cases:
            status, problems = lifted.run("SM89", fp, case)
            assert status == "native" and not problems, (case, problems)
