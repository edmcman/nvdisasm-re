"""Reference evidence preserves case multiplicity and exact forms shard by shard."""
from sass.assemble import encoding
from sass.sleigh.modifiers import form
from tests import evidence
from tests.harness.case import Case


def test_reference_counts_cases_and_exact_forms(monkeypatch):
    cls = 'fadd__RRR_RR'
    plain = Case(cls, {'word': encoding.encode('SM89', cls)}, {})
    negated = Case(cls, {'word': encoding.encode('SM89', cls, **{'Ra@negate': 1})}, {})
    kernel = Case(cls, {'kernel': {}}, {})
    calls = []

    def stream(*job):
        calls.append(job)
        return iter([plain, negated, kernel, plain])

    monkeypatch.setattr(evidence, 'stream', stream)
    job = ('SM89', 'exact', 'fp32', 0, (0, 3))
    arch, found = evidence.reference(job)
    assert calls == [job] and arch == 'SM89'
    base = {'ftz': 'noftz', 'rnd': 'RN', 'sat': 'nosat',
            'Ra@negate': 'unset', 'Ra@absolute': 'unset',
            'Rc@negate': 'unset', 'Rc@absolute': 'unset'}
    neg = base | {'Ra@negate': 'set'}
    assert found == {cls: [4, {form(base): base, form(neg): neg}]}
    out = evidence.evidence({'ptxas': [(cls, base, 'probe.ptx')]}, found | {'mov__RI': [2, {}]})
    assert out['mov__RI'] == {'reference': 2}
    assert out[cls]['reference'] == 4 and out[cls]['ptxas'] == ['probe.ptx']
    names = out[cls]['modifiers']
    assert out[cls]['forms'] == {'ptxas': [[base[m] for m in names]], 'reference': [[s[m] for m in names] for _, s in sorted({form(base): base, form(neg): neg}.items())]}


def test_reference_caller_controls_sampling(monkeypatch):
    calls = []
    def run(fn, variants, families, sample):
        calls.append(sample)
        yield 'SM89', {'mov__RI': [1, {}]}
    monkeypatch.setattr(evidence.sharded, 'run', run)
    assert evidence.references(('SM89',))['SM89']['mov__RI'][0] == 1
    assert evidence.references(('SM89',), sample=25)['SM89']['mov__RI'][0] == 1
    assert calls == [0, 25]


def test_fixture_dataflow_uses_target_architecture():
    from sass.sleigh.gen import Gen
    from tests.harness import gpu
    from tests.cases import gpu_fixtures
    for arch in ('SM89', 'SM90'):
        fixture = gpu_fixtures(('int_alu',), arch=arch)[0][1]
        template = gpu.Template(fixture.image, fixture.kernel)
        sliced = evidence.on_data_path(Gen(arch), template.code)
        assert sliced
        assert any(template.decoded(offset).klass.mnemonic == 'IADD3' for offset in sliced)
