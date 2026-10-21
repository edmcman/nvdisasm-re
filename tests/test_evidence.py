"""Reference evidence preserves case multiplicity while streaming families and lowerings."""
from sass.assemble import encoding
from tests import evidence
from tests.harness.case import Case


def test_reference_streams_sampled_cases_and_exact_forms(monkeypatch):
    cls = 'fadd__RRR_RR'
    plain = Case(cls, {'word': encoding.encode('SM89', cls)}, {})
    negated = Case(cls, {'word': encoding.encode('SM89', cls, **{'Ra@negate': 1})}, {})
    kernel = Case(cls, {'kernel': {}}, {})
    calls = []

    def cases(arch, fp, families, exhaustive):
        calls.append((arch, fp, families, exhaustive))
        return {'fp32': [plain, negated, kernel] if fp == 'exact' else [plain]}

    monkeypatch.setattr(evidence, 'FAMILIES', ('fp32',))
    monkeypatch.setattr(evidence, 'MODES', ('exact', 'readable'))
    monkeypatch.setattr(evidence, 'emulator_cases', cases)
    rows = evidence.reference('SM89')
    assert calls == []
    form = {'ftz': 'noftz', 'rnd': 'RN', 'sat': 'nosat',
            'Ra@negate': 'unset', 'Ra@absolute': 'unset',
            'Rc@negate': 'unset', 'Rc@absolute': 'unset'}
    assert next(rows) == (cls, form, None)
    assert calls == [('SM89', 'exact', ('fp32',), False)]
    assert list(rows) == [(cls, form | {'Ra@negate': 'set'}, None),
                          (cls, {}, None), (cls, form, None)]
    assert calls[-1] == ('SM89', 'readable', ('fp32',), False)
