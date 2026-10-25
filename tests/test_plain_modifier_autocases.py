"""New narrowing and load variants use the shared generated-case path."""
import json
import pytest
from sass.decode import ir
from tests.harness import autocases, lifted


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
