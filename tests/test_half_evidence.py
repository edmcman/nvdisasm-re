import itertools
import random

import pytest

from sass.assemble import encoding
from sass.decode import ir, pydecode
from sass.semantics import bitvec, evaluate
from sass.semantics.families import floating
from sass.semantics.fp import fpformat, softfloat
from sass.sleigh.modifiers import settings
from sass.sleigh.build import ARCHES
from tests.harness import autocases, autocase_profiles, autocase_half_requests, device_cases, device_evidence, lifted
from tests.harness.case import source
from tests.refs import float
import tests.refs.fpformat


def form(cls, **fields):
    return settings('SM89', pydecode.decode('SM89', bytes.fromhex(encoding.encode('SM89', cls, **fields))))


@pytest.mark.parametrize('request_id', ('R1', 'R2', 'R3', 'R4', 'R6', 'R7', 'R8', 'R9', 'R10', 'R12'))
def test_requested_forms_match_recorded_gpu_observations(request_id):
    family = 'conversions' if request_id == 'R10' else 'fp16'
    cases = autocase_half_requests.cases('SM89', 'exact', family, request_id)
    scenarios = (device_cases.Scenario(family, c, *device_cases.classify(c, arch='SM89'), arch='SM89') for c in cases)
    summary = device_evidence.summary(scenarios, ids=False)
    assert summary['cases'] and summary['verdicts'].get('pass', 0) == summary['cases'], (request_id, summary)


def test_requested_profiles_match_reviewed_sizes():
    from collections import Counter
    counts = Counter()
    for entry in autocase_half_requests.entries('SM89'):
        counts[entry.request] += len(entry.inputs.values) or len(entry.inputs.vectors)
    assert counts == dict(R1=40, R2=30, R3=74, R4=114, R6=39, R7=14, R8=36, R9=15, R10=42, R12=144)


def test_routine_generation_does_not_expand_request_matrices(monkeypatch):
    def expanded(*args): pytest.fail('routine modifier generation expanded a request matrix')
    monkeypatch.setattr(autocase_profiles, 'half_request_vectors', expanded)
    monkeypatch.setattr(autocase_profiles, 'bf16_fma_vectors', expanded)
    for cls, fields in (('hadd2__RR', {}), ('hfma2__RRR', {}), ('hfma2__RRR', {'ofmt': 'BF16_V2'})):
        assert autocases.generate('SM89', cls, form(cls, **fields), sample=0)


def test_default_stream_retains_all_focused_inputs():
    cases = list(autocase_half_requests.cases('SM89', 'exact', 'fp16'))
    default = itertools.islice(autocases.stream('SM89', 'exact', 'fp16'), len(cases))
    assert {c.id for c in default} == {c.id for c in cases}


def test_minmax_plan_covers_decomposed_source_and_predicate_properties():
    md = ir.load('SM89')
    planned = autocases.saved_plan('SM89')['forms']
    for k in md.classes:
        if k.name not in ('hmnmx2__RRR', 'hmnmx2__RCR', 'hmnmx2__RUR', 'hmnmx2__RCxR', 'hmnmx2__RIR'):
            continue
        forms = [e['settings'] for e in planned if e['cls'] == k.name
                 and e['settings'].get('ofmt', 'F16_V2') == 'F16_V2'
                 and e['settings'].get('xorsign', 'noxorsign') == 'noxorsign']
        for name, swizzle in zip(('Ra', source(k, 'Sb', 'Rb', 'URb')), ('iswzA', 'iswzB')):
            if k.operand_types[name].type not in ('Register', 'UniformRegister', 'C', 'CX'):
                continue
            for absolute, negate in (('set', 'unset'), ('unset', 'set'), ('set', 'set')):
                assert any(f.get(name+'@absolute') == absolute and f.get(name+'@negate') == negate
                           for f in forms), (k.name, name, absolute, negate)
            for value in ('H1_H0', 'H0_H0', 'H1_H1'):
                assert any(f.get(swizzle) == value for f in forms), (k.name, swizzle, value)
        for inverted in ('unset', 'set'):
            assert any(f.get('Pp@not') == inverted for f in forms), (k.name, inverted)


@pytest.mark.parametrize('arch', ARCHES)
def test_requested_profiles_use_architecture_enums(arch):
    md = ir.load(arch)
    classes = {k.name: k for k in md.classes}
    for entry in autocase_half_requests.entries(arch):
        actual = autocases.encoded_form(arch, classes[entry.cls], entry.fields)
        assert autocases.generate(arch, entry.cls, actual, sample=0, input_profile=entry.inputs)


def test_v16_matrix_preserves_every_ordered_pair():
    values = autocase_profiles.V16
    assert len(set(values)) == 28
    pairs = set(itertools.product(values, repeat=2))
    vectors = autocase_profiles.half_request_vectors(2)
    assert len(vectors) == 784
    assert {tuple(v & 0xffff for v in raw) for raw in vectors} == pairs
    assert all(all(v & 0xffff != v >> 16 for v in raw) for raw in vectors)
    triples = autocase_profiles.half_request_vectors(3)
    for positions in itertools.combinations(range(3), 2):
        for anchor in (0, 0x3c00, 0x7c00, 0x7e00):
            remaining = next(n for n in range(3) if n not in positions)
            assert {tuple(raw[n] & 0xffff for n in positions) for raw in triples
                    if raw[remaining] & 0xffff == anchor} == pairs


def test_discovery_keeps_nan_inputs_without_assigning_expectations():
    cls = 'hadd2_F32__RR'
    cases = autocases.generate('SM89', cls, form(cls, Ra='RZ', **{'Ra@negate': 1}, iswzB_as_C='H0_H0'),
                              discovery=True)
    assert all(not c.expected and c.request['observe'] and c.hardware['probe'] for c in cases)
    nans = {c.request['registers']['R4'] & 0xffff for c in cases
            if softfloat.unpack(c.request['registers']['R4'] & 0xffff, 16)[0] == 'nan'}
    assert {v for v in autocase_profiles.V16 if softfloat.unpack(v, 16)[0] == 'nan'} <= nans


def test_conversion_discovery_preserves_v16():
    cls = 'f2i__Rb_16b'
    actual = form(cls, srcfmt='F16', ftz='FTZ', rnd='TRUNC', ntz='NTZ', hsel='H0')
    cases = autocases.generate('SM89', cls, actual, discovery=True)
    assert set(autocase_profiles.V16) <= {c.request['registers']['R4'] & 0xffff for c in cases}
    assert all(not c.expected for c in cases)


@pytest.mark.parametrize('xs', [(0x3c01, 0x3bff, 9), (0x3c02, 0x3bff, 15)])
def test_half_midpoint_witnesses_separate_binary32_rounding(xs):
    a, b, c = [softfloat.unpack(v, 16)[2] for v in xs]
    exact = a * b + c
    intermediate = softfloat.unpack(softfloat.pack(exact, 32), 32)[2]
    assert float.rounded('fma', xs, 16) != softfloat.pack(intermediate, 16)


@pytest.mark.parametrize('operation,fn', [('add', floating.half_add), ('mul', floating.half_mul)])
def test_half_scalar_arithmetic_against_exact_reference(operation, fn):
    expression = fn(bitvec.param('a', 16), bitvec.param('b', 16))
    rng = random.Random(operation)
    vectors = list(itertools.product(autocase_profiles.V16, repeat=2))
    vectors += [(rng.getrandbits(16), rng.getrandbits(16)) for _ in range(2048)]
    for a, b in vectors:
        assert evaluate.evaluate(expression, None, dict(a=a, b=b)) == float.rounded(operation, [a, b], 16)


def test_half_fma_against_independent_rationals():
    expression = floating.half_fma(*(bitvec.param(n, 16) for n in ('a', 'b', 'c')))
    rng = random.Random('half-fma')
    vectors = list(itertools.product(autocase_profiles.V16, repeat=3))
    vectors += [tuple(rng.getrandbits(16) for _ in range(3)) for _ in range(4096)]
    for xs in vectors:
        assert evaluate.evaluate(expression, None, dict(zip(('a', 'b', 'c'), xs))) == float.rounded('fma', xs, 16), xs


@pytest.mark.parametrize('cls,fields', [
    ('hadd2__RR', {'ftz': 'FTZ'}),
    ('hfma2__RRR', {'fmz': 'FTZ'}),
    ('hfma2_mma__RRR_norelu', {'fmz': 'FTZ'}),
    ('hfma2__RRI', {'Sc': 0, 'Sc1': 0, 'Ra@negate': 1}),
    ('hfma2__RRR', {'ofmt': 'BF16_V2'}),
    ('hfma2_relu__RRR', {'ofmt': 'BF16_V2', 'satrelu': 'RELU'}),
])
@pytest.mark.parametrize('fp', ['exact', 'readable'])
def test_measured_half_forms_lift(cls, fields, fp):
    for case in autocases.generate('SM89', cls, form(cls, **fields), fp, sample=0):
        status, problems = lifted.run('SM89', fp, case)
        assert status == 'native' and not problems, (case, problems)


def test_bf16_integer_fma_against_independent_rationals():
    params = [bitvec.param(n, 16) for n in ('a', 'b', 'c')]
    expression = fpformat.bf16_fused(*params)
    rng = random.Random('bf16-fma')
    values = autocase_profiles.PROFILES['bf16_fma']['values']
    vectors = list(itertools.product(values, repeat=3))
    vectors += [tuple(rng.getrandbits(16) for _ in range(3)) for _ in range(4096)]
    for xs in vectors:
        assert evaluate.evaluate(expression, None, dict(zip(('a', 'b', 'c'), xs))) == tests.refs.fpformat.fused(*xs), xs


@pytest.mark.parametrize('fn,label,arity', [
    (floating.half_add, 'add.rn.f16', 2), (floating.half_mul, 'mul.rn.f16', 2),
    (floating.half_fma, 'fma.rn.f16', 3), (floating.widen_half, 'widen.f16.f32', 1),
])
def test_half_scalar_definition_interfaces(fn, label, arity):
    args = [bitvec.param(f'a{n}', 16) for n in range(arity)]
    expression = fn(*args)
    assert expression.op == label
    from sass.emulate import definitions
    assert 'sass::Bits<' in definitions.definition_function(label, (16,) * arity)


def test_preserved_definition_arguments_are_lowered():
    args = [bitvec.param(n, 32) for n in ('a', 'b', 'c')]
    expression = fpformat.bf16_packed(floating.flushed(args[0]), *args[1:])
    lowered = bitvec.lowered(expression, preserve={expression.op})
    assert lowered.op == 'fma.rn.bf16x2'
    assert lowered.args == (bitvec.lowered(expression.args[0]), *args[1:])
    assert lowered.args[0].op != expression.args[0].op
    assert bitvec.lowered(lowered) is bitvec.lowered(expression)
    values = dict(a=0x3f803f80, b=0x3f803f80, c=0)
    assert evaluate.evaluate(lowered, None, values) == 0x3f803f80
