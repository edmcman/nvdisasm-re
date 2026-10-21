"""Caller-controlled sampling preserves coverage, regressions and backend eligibility."""
import argparse
import json
import random
import subprocess
import sys
import pytest
from tests.cases import emulator_cases
from tests.harness import autocases, device_cases, lifted, sampling
from tests.harness.case import Cases
from tests.harness.manual import retained


def corpus(n=80):
    c = Cases('SM89')
    for negate in (0, 1):
        for i in range(n):
            c.add('fadd__RRR_RR', dict(Rd=8, Ra=2, Rc=4, **{'Ra@negate': negate}),
                  {'R2': i, 'R4': 0x3f800000}, {'R8': 0})
    return c


def identities(cases): return {case.id for case in cases}


def forms(cases, arch='SM89'): return {sampling.form(arch, case.request['word']) for case in cases}


@pytest.mark.parametrize('bad', [-1, 101, True, False, 0.5, '1.0', 'nan', None])
def test_invalid_percentages(bad):
    with pytest.raises(ValueError, match='integer from 0 to 100'): sampling.percentage(bad)


def test_endpoints_nested_and_order_independent():
    c = corpus()
    samples = [sampling.select(c, 'SM89', p) for p in (0, 1, 25, 50, 99, 100)]
    assert len(samples[0]) == 2
    assert samples[-1] is c
    assert all(forms(s) == forms(c) for s in samples)
    assert all(identities(a) <= identities(b) for a, b in zip(samples, samples[1:]))
    assert 2 < len(samples[2]) < len(c)
    shuffled = list(c); random.Random(42).shuffle(shuffled)
    for p, selected in zip((0, 1, 25, 50, 99, 100), samples):
        assert identities(sampling.select(shuffled, 'SM89', p)) == identities(selected)


def test_backend_floor_and_unconditional_cases():
    c = corpus(6)
    base = c[0]
    # A context is GPU-only through an explicit warp adapter, and cannot be lifted.
    gpu = base._replace(request={**base.request, 'context': {'warp': True}}, hardware={'adapter': 'warp'})
    emulated = [case._replace(hardware={'skip': 'synthetic'}) for case in c]
    full = [*emulated, gpu]
    selected = sampling.select(full, 'SM89', 0)
    assert gpu in selected
    assert any(lifted.applies(case) for case in selected)
    assert any(device_cases.classify(case)[0] != 'emulator' for case in selected)
    kernel = base._replace(request={'kernel': {}}, expected={'error': 'stopped'})
    error = base._replace(expected={'error': 'expected error'})
    pinned = c[-1]
    selected = sampling.select([*c, kernel, error], 'SM89', 0, retained={pinned.id})
    assert all(case in selected for case in (kernel, error, pinned))


def test_retained_regressions_and_sweeps(monkeypatch):
    from tests import cases
    @retained('specific regression corpus')
    def regressions(c):
        for i in c.sweep(32):
            c.add('mov__RI', dict(Rd=8, Sb=i, PixMaskU04=15), {}, {'R8': i})
    class Family:
        @staticmethod
        def emulator_cases(c):
            regressions(c)
            for i in c.sweep(128):
                c.add('mov__RR', dict(Rd=8, Ra=2, PixMaskU04=15), {'R2': i}, {'R8': i})
    monkeypatch.setattr(cases, 'module', lambda _: Family)
    full = emulator_cases('SM89', 'exact', ('synthetic',), 100, generated=False)['synthetic']
    minimal = emulator_cases('SM89', 'exact', ('synthetic',), 0, generated=False)['synthetic']
    assert len(full) == 160
    assert len(minimal) == 33
    assert set(full.manual_reasons) <= identities(minimal)
    assert forms(full) == forms(minimal)


def test_generated_filtering_and_shard_independence(monkeypatch):
    entries = [dict(family='fp32', cls=cls, settings=settings, profiles=['floating_forms'], native=True)
               for cls, settings in [('fmul__RRR_RR', {'rnd':'RN','fmz':'FTZ','sat':'SAT','scale':'noscale'}),
                                     ('ffma__RRR_RRR', {'rnd':'RN','fmz':'FTZ','sat':'nosat'}),
                                     ('fset__RRR_RRR', {'fcomp':'EQ','bop':'AND','ftz':'noftz'})]]
    from sass.assemble import encoding
    for entry in entries:
        entry['settings'] = dict(sampling.form('SM89', encoding.encode('SM89', entry['cls'], **entry['settings']))[1])
    monkeypatch.setattr(autocases, 'saved_plan', lambda arch: {'forms': entries})
    for fp in ('exact', 'readable'):
        for p in (0, 25, 100):
            whole = list(autocases.stream('SM89', fp, 'fp32', sample=p))
            split = [case for i in range(2) for case in autocases.stream('SM89', fp, 'fp32', i, 2, sample=p)]
            assert identities(whole) == identities(split)
            complete = list(autocases.stream('SM89', fp, 'fp32', sample=100))
            assert forms(whole) == forms(complete)


def test_cli_alias_and_conflicts():
    p = argparse.ArgumentParser(); sampling.add_arguments(p)
    for args, expected in [([], 0), (['--sample','25'],25), (['--exhaustive'],100)]:
        a = p.parse_args(args)
        assert sampling.resolve(a.sample, a.exhaustive) == expected
    for args in (['--sample','101'], ['--sample','0','--exhaustive']):
        with pytest.raises(SystemExit): p.parse_args(args)
    with pytest.raises(ValueError, match='mutually exclusive'): sampling.resolve(0, True)


def test_selection_independent_of_python_hash_seed():
    script = "from tests.test_sampling import corpus; from tests.harness import sampling; import json; print(json.dumps([c.id for c in sampling.select(corpus(), 'SM89', 25)]))"
    import os
    outputs = [subprocess.check_output([sys.executable, '-c', script], env={**os.environ, 'PYTHONHASHSEED': seed}, text=True)
               for seed in ('1', '123')]
    assert json.loads(outputs[0]) == json.loads(outputs[1])


def test_same_scenario_retains_all_assertions():
    c = corpus()
    representative = sampling.select(c, 'SM89', 0)[0]
    extra_check = representative._replace(expected={**representative.expected, '__max_ops': 2})
    assert extra_check.id == representative.id
    selected = sampling.select([*c, extra_check], 'SM89', 0)
    assert representative in selected and extra_check in selected


def test_minimal_generation_encodes_only_selected_inputs(monkeypatch):
    from sass.assemble import encoding
    cls = 'fset__RRR_RRR'
    form = dict(sampling.form('SM89', encoding.encode('SM89', cls, fcomp='EQ', bop='AND'))[1])
    calls = []
    original = autocases.checked_word
    def checked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(autocases, 'checked_word', checked)
    full = autocases.generate('SM89', cls, form, sample=100)
    assert len(calls) == len(full) and len(full) > 1
    samples = []
    for percent in (0, 25, 100):
        calls.clear()
        selected = autocases.generate('SM89', cls, form, sample=percent)
        assert len(calls) == len(selected)
        assert not selected.deferred and not any(isinstance(x, autocases.Pending) for x in selected)
        assert identities(selected) == identities(sampling.select(full, 'SM89', percent))
        assert forms(selected) == forms(full)
        samples.append(selected)
    assert len(samples[0]) == 1
    assert identities(samples[0]) <= identities(samples[1]) <= identities(samples[2])
