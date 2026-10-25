"""Interaction selection and accounting checked against independent exhaustive tuples."""
import itertools
import pytest
from sass.sleigh import verification, modifiers


def row(dimensions=4):
    return dict(cls="example", opcode="EXAMPLE", combinations=dict(
        labels={chr(65 + i): ["0", "1"] for i in range(dimensions)},
        decides=[], table=[["native"]], free=2 ** dimensions))


def tuples(settings, strength):
    return {tuple((n, settings[n]) for n in names)
            for names in itertools.combinations(sorted(settings), min(strength, len(settings)))}


def select(requirements):
    cover = verification.Cover(requirements)
    selected = []
    while candidate := cover.next():
        form, _, mask = candidate
        selected.append(form)
        cover.accept(mask)
    return selected, cover.uncovered


def test_three_way_covers_independently_enumerated_requirements():
    r = row()
    selected, missing = select(verification.Requirements(r))
    target = set().union(*(tuples(s, 3) for s, _ in verification.forms(r)))
    assert set().union(*(tuples(s, 3) for s in selected)) == target
    assert not missing and len(selected) < 16
    assert selected == select(verification.Requirements(r))[0]


def test_constraints_require_complete_legal_extension():
    r = row()
    r["combinations"].update(decides=["A", "B"], free=4,
                             table=[["0", "0", "native"], ["1", "1", "native"]])
    selected, missing = select(verification.Requirements(r))
    assert not missing
    assert all(s["A"] == s["B"] for s in selected)
    target = set().union(*(tuples(s, 3) for s, _ in verification.forms(r)))
    assert set().union(*(tuples(s, 3) for s in selected)) == target
    reordered = {**r, "combinations": {**r["combinations"], "table": list(reversed(r["combinations"]["table"])),
                                      "labels": dict(reversed(list(r["combinations"]["labels"].items())))}}
    assert selected == select(verification.Requirements(reordered))[0]


@pytest.mark.parametrize("strength", [1, 2, 3, 4])
def test_mixed_domains_and_projected_constraints_match_exhaustive_oracle(strength):
    r = dict(cls="mixed", opcode="MIXED", combinations=dict(
        labels={"A": ["a", "b", "c"], "B": ["x", "y"], "C": ["0", "1"],
                "D": ["p", "q", "r"], "fixed": ["z"]},
        decides=["A", "C"], free=6,
        table=[["a", "0", "native"], ["a", "1", "opaque"],
               ["b", "0", "partially native"], ["c", "1", "primitive"]]))
    req = verification.Requirements(r, verification.VerificationPolicy(default_strength=strength))
    legal = [(s, status) for s, status in verification.forms(r) if status != "opaque"]
    target = set().union(*(tuples({n: s[n] for n in req.domains}, strength) for s, _ in legal))
    assert req.required.bit_count() == len(target)
    selected, missing = select(req)
    plannable = set().union(*(tuples({n: s[n] for n in req.domains}, strength)
                              for s, status in legal if status in verification.SUPPORTED))
    assert set().union(*(tuples({n: s[n] for n in req.domains}, strength) for s in selected)) == plannable
    assert missing.bit_count() == len(target - plannable)


@pytest.mark.parametrize("strength", [1, 2, 3, 14, "exhaustive"])
def test_strength_override_and_smaller_classes(strength):
    r = row(2)
    req = verification.Requirements(r, verification.VerificationPolicy(opcode_strength={"EXAMPLE": strength}))
    selected, missing = select(req)
    assert not missing
    if strength != 1:
        assert len(selected) == 4 and req.strength == 2


def test_constant_and_empty_classes_have_anchor():
    for labels in ({}, {"fixed": ["X"]}):
        r = row(0)
        r["combinations"]["labels"] = labels
        req = verification.Requirements(r)
        assert req.required == 1 and req.strength == 0
        assert len(select(req)[0]) == 1
    r["combinations"]["table"] = [["opaque"]]
    assert not verification.Requirements(r).required


def test_partial_requirements_are_not_credited_without_a_case():
    r = row(2)
    r["combinations"].update(decides=["A"], free=2,
                             table=[["0", "native"], ["1", "partially native"]])
    req = verification.Requirements(r)
    cover = verification.Cover(req)
    candidate = cover.next()
    assert candidate and cover.uncovered == req.required
    # Reject that candidate: another legal extension remains available.
    candidate = cover.next()
    cover.accept(candidate[2])
    assert cover.uncovered & ~req.plannable


def test_evidence_uses_positive_forms_and_known_settings():
    req = verification.Requirements(row())
    entry = dict(modifiers=["A", "B", "C", "D"],
                 forms=dict(reference=[["0"] * 4], gpu=[["0", "0", "0", None]]),
                 verification_forms=dict(reference=[]))
    e = req.evidence(entry)
    assert e["covered"]["reference"] == 0
    assert e["covered"]["gpu"] == 1
    entry["verification_forms"]["reference"] = [["0"] * 4]
    assert req.evidence(entry)["covered"]["reference"] == 4
    entry["forms"]["gpu"] = [["INVALID"] * 4]
    assert req.evidence(entry)["covered"]["gpu"] == 0


def test_positive_forms_compact_round_trip():
    entry = dict(modifiers=["A"], forms=dict(reference=[["0"], ["1"]]),
                 verification_forms=dict(reference=[["1"]]))
    assert modifiers.expand(modifiers.compact(entry)) == entry
    entry["verification_forms"]["reference"] = []
    assert modifiers.expand(modifiers.compact(entry)) == entry


@pytest.mark.parametrize("strength", [0, -1, True, "pairwise", 2.5])
def test_invalid_policy(strength):
    with pytest.raises(ValueError):
        verification.VerificationPolicy(default_strength=strength)


def test_interaction_completion_does_not_manufacture_exact_provenance():
    from sass import provenance
    req = verification.Requirements(row())
    selected, _ = select(req)
    values = [[s[n] for n in req.domains] for s in selected]
    entry = dict(modifiers=list(req.domains), forms=dict(reference=values),
                 verification_forms=dict(reference=values))
    assert req.evidence(entry)["covered"]["reference"] == req.required.bit_count()
    unrecorded = next(s for s, _ in verification.forms(row()) if s not in selected)
    assert not provenance.ran(modifiers.compact(entry), "reference", unrecorded)


def test_roles_are_family_local_and_do_not_change_support():
    from sass.assemble import encoding
    from sass.semantics import family
    memory = family.family_for(encoding.klass("SM89", "ld__sImmOffset"))
    assert memory.modifier_roles["sp2"] == "hint"
    assert memory.modifier_roles["sem"] == "effect"
    arithmetic = family.family_for(encoding.klass("SM89", "fadd__RRR_RR"))
    assert not arithmetic.modifier_roles
    with pytest.raises(ValueError):
        family.family("INVALID_TEST_ROLE", modifier_roles={"mode": "scheduling"})


def test_summary_keeps_exhaustive_support_and_independent_sources():
    from sass.sleigh import coverage
    r = row(2)
    r.update(status="native", modifiers={n: {v: "native" for v in ls} for n, ls in r["combinations"]["labels"].items()})
    entry = dict(modifiers=["A", "B"], forms=dict(reference=[["0", "0"]], gpu=[["0", "0"]]),
                 verification_forms=dict(reference=[["0", "0"]]))
    interactions = {r["cls"]: verification.Requirements(r).evidence(entry)}
    summary = coverage.summarized([r], {r["cls"]: entry}, [coverage.combinations(r, entry)], interactions)
    assert summary["combination_status"] == {"EXAMPLE": {"native": 4}}
    assert summary["interaction_evidence"]["EXAMPLE"] == dict(strength=3, total=4, covered=dict(reference=1, gpu=1, ptxas=0))
