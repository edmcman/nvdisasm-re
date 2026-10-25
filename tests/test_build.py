"""Coverage collection must not change generated code."""
import json
import pytest
from sass.sleigh import gen, modifiers


def instruction(coverage):
    g = gen.Gen("SM89", coverage=coverage)
    k = next(k for k in g.arch.classes if k.name == "prmt__RuIR_RIR")
    g.generate = lambda: [g.build(k, None, [], {})[0]]
    return g


def test_coverage_is_optional(tmp_path, monkeypatch):
    detailed = instruction(True)
    expected = detailed.slaspec()
    def forbidden(*args):
        pytest.fail("default generation enumerated coverage combinations")
    monkeypatch.setattr(modifiers, "combinations", forbidden)
    plain = instruction(False)
    assert plain.slaspec() == expected
    assert plain.coverage == {}
    plain.write_manifests(tmp_path)
    assert not list(tmp_path.glob("*.json"))
    detailed.write_manifests(tmp_path)
    assert json.loads((tmp_path / "sass_sm89_coverage.json").read_text())


@pytest.mark.parametrize("arch", ["SM75", "SM89", "SM90", "SM100"])
def test_wide_load_coverage_respects_span_domains(arch):
    """Wide sizes cannot inherit opaque rows from a narrower destination variant."""
    g = gen.Gen(arch, coverage=True)
    k = next(k for k in g.arch.classes if k.name == "ld__sImmOffset")
    g.semantics(k, None)
    combinations = g.coverage[k.name]["combinations"]
    decides = combinations["decides"]
    sz = decides.index("sz") if "sz" in decides else None
    assert all(row[-1] == "native" for row in combinations["table"]
               if sz is None or row[sz] != "U.128")
    assert g.coverage[k.name]["modifiers"]["sz"]["64"] == "native"
    assert g.coverage[k.name]["modifiers"]["sz"]["128"] == "native"


def test_span_domain_collection_preserves_generated_code():
    def load(coverage):
        g = gen.Gen("SM89", coverage=coverage)
        k = next(k for k in g.arch.classes if k.name == "ld__sImmOffset")
        g.generate = lambda: [g.build(k, None, [], {})[0]]
        return g.slaspec()
    assert load(False) == load(True)


def test_merge_retains_disjoint_domains_and_genuine_partial_status():
    narrow = dict(labels={"sz": ["32"]}, decides=[], table=[["native"]], free=1)
    wide = dict(labels={"sz": ["64"]}, decides=[], table=[["opaque"]], free=1)
    merged = modifiers.merge(narrow, wide)
    assert merged["decides"] == ["sz"]
    assert merged["table"] == [["32", "native"], ["64", "opaque"]]
    assert modifiers.merge(narrow, {**narrow, "table": [["opaque"]]})["table"] == [["partially native"]]
