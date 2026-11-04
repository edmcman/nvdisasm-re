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


@pytest.mark.parametrize("arch", ["SM75", "SM89", "SM100"])
def test_matrix_load_span_enumeration(arch):
    from sass.sleigh import verification
    g = gen.Gen(arch, coverage=True)
    k = next(k for k in g.arch.classes if k.name == "ldsm__sImmOffset")
    g.semantics(k, None)
    forms = [(form, status) for form, status in verification.forms(g.coverage[k.name])
             if form["sz"] == "16" and form["mode"] in ("M88", "MT88")]
    assert {form["num"] for form, _ in forms} == {"1", "2", "4"}
    assert all(status == "primitive" for _, status in forms)


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_span_conditions_reject_jointly_impossible_formats(arch, monkeypatch):
    from sass.semantics import family
    import sass.semantics.families
    monkeypatch.setitem(family.FAMILIES, "HMMA", family.FAMILIES["LDSM"])
    g = gen.Gen(arch)
    k = next(k for k in g.arch.classes if k.name == "hmma_x8_")
    domain = {g.source(k, name)[1].ranges: values for name, values in
              (("dstfmt", {0}), ("srcfmt", {2, 3}), ("size", {0, 1, 2, 3}))}
    assert g.conditions(k, domain)[0] is None


def test_constant_atomic_predicate_does_not_duplicate_body():
    g = gen.Gen("SM90")
    k = next(k for k in g.arch.classes if k.name == "atomg_int__RaNonRZ")
    g.semantics(k, None)
    assert not any(name.startswith("live") for name in g.tables.values())
def test_zero_argument_effect_is_retained():
    g = gen.Gen("SM89")
    k = next(k for k in g.arch.classes if k.name == "ldgdepbar_")
    g.build(k)
    assert "sass_async_commit" in g.pcodeops
    assert any("sass_async_commit();" in row for row in g.table_lines)


def test_pure_forwarding_reuses_tables_across_constructors():
    from types import SimpleNamespace
    from sass.semantics.bitvec import const
    from sass.semantics.families import matrix
    from sass.sleigh import lower
    g = gen.Gen("SM89")
    b = SimpleNamespace(g=g, values={}, refs=[], serial=0)
    first, second = lower.Code(), lower.Code()
    for code, values in ((first, (1, 2)), (second, (3, 4))):
        emitter = lower.Lower(b, code, {})
        for value in values:
            emitter.scratch_call(matrix.half_accumulator(const(value, 16)))
    assert len(g.scratch_forwards) == 2
    assert [line for line in first.lines if line.startswith("build ")] == [
        line for line in second.lines if line.startswith("build ")]
    assert len({line for line in first.lines if line.startswith("build ")}) == 2


def test_class_filter_preserves_opaque_input_spans():
    from sass.sleigh.gen import Gen
    g = Gen("SM90")
    sparse = next(k for k in g.arch.classes if k.name == "hmma_sparse_")
    direct = next(k for k in g.arch.classes if k.name == "hmma_x8_")
    assert g.dynamic_inputs(sparse) == {"Ra", "Rb", "Rc"}
    assert len(g.span_variants(sparse)) == 2
    assert not g.dynamic_inputs(direct)


@pytest.mark.parametrize("arch", ["SM89", "SM100"])
def test_span_domain_narrowing_is_encoded(arch):
    g = gen.Gen(arch)
    k = next(k for k in g.arch.classes if k.name == "ld__sImmOffset")
    field = g.source(k, "sz")[1]
    clauses, _ = g.conditions(k, {field.ranges: set(range(8))})
    assert all(clause in clauses for clause in gen.member(g, field, set(range(7))))


@pytest.mark.parametrize("name", ["iadd3_noimm__RRR_RRR", "uiadd3__URURUR_URURUR", "lop3_lut__RRR_RRR"])
def test_compact_integer_forms_prune_discarded_predicates(name):
    g = gen.Gen("SM100")
    k = next(k for k in g.arch.classes if k.name == name)
    g.semantics(k, None)
    assert any(table.startswith("live") for table in g.tables.values())
